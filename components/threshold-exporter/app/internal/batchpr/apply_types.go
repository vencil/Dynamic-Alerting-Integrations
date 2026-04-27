package batchpr

// PR-2 — Apply mode (push branches + open PRs via GitHub).
//
// PR-1 ships the planner: Plan describes what to do; this file
// describes the contract for actually doing it. The orchestration
// itself lives in apply.go; the per-op interfaces live in
// interfaces.go.
//
// Design principles (carried over from C-9 / C-12 PR family):
//
//   - IO at the edges. The Apply() function is pure orchestration;
//     git operations + GitHub API calls happen behind GitClient /
//     PRClient interfaces. Tests use in-memory stub impls; production
//     wires shell-out impls (`git` + `gh` CLIs).
//
//   - Per-item dispatch. Each Plan.Items[i] is one PR's worth of
//     work; success/failure is recorded per-item so a partial run
//     can be resumed (a future `apply --resume` would consume the
//     ApplyResult to skip already-opened items).
//
//   - Idempotency on branch level. Re-running Apply() against the
//     same Plan + remote should NOT create duplicate PRs. Branch
//     names are deterministic from the Plan; a branch that already
//     exists on the remote AND has an open PR is "skipped existing"
//     rather than re-pushed (see branch.go for naming + idempotency
//     check semantics).
//
//   - <base> placeholder substitution. Tenant PRs carry
//     `Blocked by: #<base>` placeholder text that PR-1 cannot fill
//     (the base PR number is only known after Apply() opens it).
//     The orchestration patches every tenant PR's description after
//     the base PR opens, via PRClient.UpdatePRDescription.
//
//   - DryRun honours the same return shape. Callers can sanity-
//     check what would happen without touching the remote; useful
//     in CI dry-run flows + customer pre-flight reviews.

// Repo identifies the GitHub repo the apply targets.
//
// Owner + Name combine into the `<owner>/<repo>` slug GitHub uses
// in its REST and CLI surfaces. BaseBranch is the branch that new
// PR branches are created off of (typically `main`).
type Repo struct {
	Owner      string `json:"owner"`
	Name       string `json:"name"`
	BaseBranch string `json:"base_branch"`
}

// FullName returns the conventional `<owner>/<name>` string.
func (r Repo) FullName() string {
	if r.Owner == "" || r.Name == "" {
		return ""
	}
	return r.Owner + "/" + r.Name
}

// ApplyInput is the contract the caller (CLI or UI) hands to the
// apply layer.
type ApplyInput struct {
	// Plan is the BuildPlan() output. Required.
	Plan *Plan `json:"plan"`

	// ItemFiles maps Plan.Items[i] → file content for that PR.
	// Each value is the same `path → bytes` map shape C-9 PR-3
	// EmitProposals returns. Files outside of any PlanItem's bucket
	// produce a Warning rather than a hard error (see
	// AllocateFiles for the canonical grouping helper).
	//
	// Required: every Plan.Items[i] that should produce a real PR
	// must have an entry. An empty file map under an index is
	// treated as "skip this item" with a warning — useful when a
	// proposal turned out to need no actual file changes.
	ItemFiles map[int]map[string][]byte `json:"-"`

	// Repo identifies the target. Required.
	Repo Repo `json:"repo"`

	// BranchPrefix is prepended to each computed branch name.
	// Empty defaults to `da-tools/c10/`. Trailing slashes
	// normalised. Lets multiple concurrent imports coexist by
	// scoping branches under different prefixes.
	BranchPrefix string `json:"branch_prefix,omitempty"`

	// CommitAuthor is the `Name <email>` string used as the git
	// committer for branch commits. Empty falls back to whatever
	// `git config user.{name,email}` resolves to in the executing
	// environment.
	CommitAuthor string `json:"commit_author,omitempty"`

	// DryRun runs the orchestration without performing any git
	// operations or GitHub API calls. ApplyResult.Items[i].Status
	// is ApplyStatusDryRun for every successful item; intended
	// for sanity-check / pre-flight workflows.
	DryRun bool `json:"dry_run,omitempty"`

	// InterCallDelayMillis is a per-item delay (in ms) inserted
	// between OpenPR calls to avoid GitHub secondary rate limits
	// when a Plan has many tenant PRs. 0 disables the sleep.
	// Caller should pick something modest (e.g. 500ms) for
	// 50+ tenant PRs.
	InterCallDelayMillis int `json:"inter_call_delay_ms,omitempty"`
}

// ApplyStatus discriminates the outcome of one PlanItem's apply.
type ApplyStatus string

const (
	// ApplyStatusCreated — branch + commit + push + PR all
	// succeeded. PRNumber + PRURL populated.
	ApplyStatusCreated ApplyStatus = "created"

	// ApplyStatusSkippedExisting — a PR with the same head branch
	// already exists on the remote; we leave it alone.
	// PRNumber + PRURL reflect the existing PR (read via
	// PRClient.FindPRByBranch). Re-runs of Apply() against the
	// same Plan are safe.
	ApplyStatusSkippedExisting ApplyStatus = "skipped_existing"

	// ApplyStatusDryRun — DryRun=true was set; no git or API calls
	// happened. BranchName is the name we WOULD have used; PR
	// fields are zero.
	ApplyStatusDryRun ApplyStatus = "dry_run"

	// ApplyStatusEmptyFiles — caller's ItemFiles[i] was nil/empty;
	// we skipped the item without committing. Useful when a
	// proposal turned out to have no actionable file changes.
	ApplyStatusEmptyFiles ApplyStatus = "empty_files"

	// ApplyStatusFailed — something went wrong. ErrorMessage is
	// populated; subsequent items are still attempted (so a
	// single rate-limited tenant doesn't block the whole batch).
	ApplyStatusFailed ApplyStatus = "failed"
)

// ApplyItemResult records the outcome of one PlanItem.
type ApplyItemResult struct {
	// PlanItemIndex points back to ApplyInput.Plan.Items[i]. Lets
	// callers correlate results with their source PlanItem (e.g.
	// to print "tenant chunk 3 failed: ...").
	PlanItemIndex int `json:"plan_item_index"`

	// Kind copies PlanItem.Kind for convenience.
	Kind PlanItemKind `json:"kind"`

	// BranchName is the local + remote branch name the apply used.
	// Always populated, even for dry-run / skipped / failed
	// outcomes.
	BranchName string `json:"branch_name"`

	// Status — see ApplyStatus consts.
	Status ApplyStatus `json:"status"`

	// PRNumber is the GitHub PR number, or 0 when no PR was
	// opened (dry-run / failed / empty-files).
	PRNumber int `json:"pr_number,omitempty"`

	// PRURL is the GitHub HTML URL of the PR. Populated alongside
	// PRNumber.
	PRURL string `json:"pr_url,omitempty"`

	// ErrorMessage captures the proximate failure reason. Non-empty
	// only when Status == ApplyStatusFailed. Verbatim error string
	// from the underlying GitClient / PRClient call, prefixed with
	// the orchestration step name ("git push", "open PR", etc.).
	ErrorMessage string `json:"error_message,omitempty"`
}

// ApplyResult is the top-level return from Apply().
//
// A run with N PlanItems always produces N ApplyItemResult entries
// (one per item, in the same order). Apply() never returns a
// hard error after starting per-item processing — failures are
// surfaced via ApplyItemResult.Status so callers can decide whether
// to re-run, fix, and retry.
type ApplyResult struct {
	Items []ApplyItemResult `json:"items"`

	// BasePRNumber is populated when the Plan had a Base PR and it
	// opened successfully (or already existed). Tenant PR
	// descriptions get this number substituted in for the `<base>`
	// placeholder PR-1 wrote.
	BasePRNumber int `json:"base_pr_number,omitempty"`

	// Summary roll-up for human / log consumption.
	Summary ApplySummary `json:"summary"`

	// Warnings collects non-fatal issues that don't fit one of the
	// per-item Statuses (e.g. AllocateFiles produced a warning;
	// CommitAuthor not set; <base> placeholder substitution
	// skipped because base PR didn't open). Surface on the CLI
	// alongside the per-item table.
	Warnings []string `json:"warnings,omitempty"`
}

// ApplySummary is the cheap roll-up view.
type ApplySummary struct {
	TotalItems              int `json:"total_items"`
	CreatedCount            int `json:"created_count"`
	SkippedExistingCount    int `json:"skipped_existing_count"`
	DryRunCount             int `json:"dry_run_count"`
	EmptyFilesCount         int `json:"empty_files_count"`
	FailedCount             int `json:"failed_count"`
	BasePlaceholderRewrites int `json:"base_placeholder_rewrites"`
}
