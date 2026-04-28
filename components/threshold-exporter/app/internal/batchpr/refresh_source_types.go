package batchpr

// PR-4 — Refresh after source-rule data-layer hot-fix.
//
// The data-layer hot-fix scenario PR-4 unblocks
// ----------------------------------------------
// PR-3 closes "Base PR merged → tenant branches need rebase".
// PR-4 closes the orthogonal scenario: "Parser shipped a bug fix
// that affects N specific source rules; only their derived
// _defaults.yaml + per-tenant override files need to change". The
// canonical example from planning §C-10:
//
//   parser tools/v2.8.0 → tools/v2.8.1 fixes a histogram bucket
//   off-by-one bug. The fix changes how 200 source rules out of
//   the customer's 10 000-rule corpus translate to threshold-
//   exporter conf.d shape. Without PR-4 the fix means re-running
//   the FULL apply pipeline (10 000 rules → 50 tenant chunks → 50
//   PRs again), even though only the 200-rule subset changed. PR-4
//   ships the surgical "patch only what changed" path.
//
// Shape of work the orchestration does
// ------------------------------------
// PR-4's contract is deliberately narrow: the caller has already
// done the heavy lifting (re-run parser, re-cluster, re-emit) and
// hands batchpr a typed `RefreshSourceTarget` per affected tenant
// PR with the NEW file contents. PR-4 walks the targets and:
//
//   1. GetPR(num) — verify the tenant PR is still open. Closed/
//      merged → record SkippedClosed (the data-layer hot-fix is
//      moot for an already-merged tenant; the merged commit is
//      what the customer wants in main).
//   2. (DryRun) — record DryRun, skip writes.
//   3. CheckoutBranch(branch) — switch to the existing tenant
//      branch (NOT reset, NOT a new branch off main).
//   4. WriteFiles(branch, target.Files) — replace the affected
//      files with the new content.
//   5. Commit(branch, message, author) — single commit referencing
//      the source rule IDs that drove the change ("data-layer
//      hot-fix: re-emitted prom-rules.yaml#groups[3].rules[12], …").
//   6. Push(branch) — REGULAR push (not force-push). PR-4 adds a
//      new commit on top of the existing tenant branch; rewriting
//      history is unnecessary and would break ongoing reviewer
//      conversations.
//   7. CommentPR(num, body) — reviewer note: "data-layer hot-fix
//      applied for source rules X, Y, Z; please re-review the
//      latest commit".
//
// Why caller writes the new files instead of batchpr
// --------------------------------------------------
// Cross-referencing source_rule_id → which proposal contains it →
// which tenant PR was the proposal grouped into requires:
//
//   - The original ApplyResult (or equivalent) capturing the
//     proposal-index ↔ tenant-PR mapping.
//   - The C-9 emit pipeline (BuildProposals → EmitProposals).
//   - The new parser ParsedRule corpus (with the bug fix).
//
// All of those live above batchpr in the toolchain stack (PR-5's
// CLI binds them together). Putting the cross-ref + re-emit logic
// inside batchpr would pull C-8 parser + C-9 profile/emit as deps,
// violating the layering. The CLI / UI does the heavy lifting and
// hands batchpr a clean per-target "here's the new file content;
// please apply it" instruction list.
//
// Difference from PR-2 Apply()
// ----------------------------
// PR-2 Apply() opens NEW tenant PRs from a Plan. PR-4 RefreshSource
// updates EXISTING tenant PRs in place. Apply uses CreateBranch
// (creates from base), RefreshSource uses CheckoutBranch (switches
// to existing). Apply uses non-force Push, RefreshSource also uses
// non-force Push (history is preserved across the patch commit).

// PatchStatus discriminates the per-tenant-PR outcome of one
// RefreshSource run.
type PatchStatus string

const (
	// PatchUpdated — checkout + write + commit + push + comment all
	// succeeded. The tenant branch now carries one new commit
	// containing the data-layer hot-fix.
	PatchUpdated PatchStatus = "updated"

	// PatchSkippedClosed — the tenant PR is closed/merged on
	// GitHub; we don't apply the hot-fix (a merged PR's content is
	// already in main; closed PRs are abandoned). PRState carries
	// "closed" or "merged" for the report.
	PatchSkippedClosed PatchStatus = "skipped_closed"

	// PatchSkippedNoChange — Files map was empty for this target
	// (caller's diff produced zero changes for this tenant). Recorded
	// rather than failed so the report shows that the tenant was
	// considered but didn't need an update.
	PatchSkippedNoChange PatchStatus = "skipped_no_change"

	// PatchDryRun — DryRun=true; the writes were not executed.
	// PRState reflects whatever GetPR returned (we still query
	// state in DryRun so reviewers see which tenant PRs the patch
	// would touch).
	PatchDryRun PatchStatus = "dry_run"

	// PatchFailed — something went wrong. Step records which
	// orchestration step failed: "get_pr" / "checkout" / "write" /
	// "commit" / "push" / "comment". Subsequent targets are still
	// attempted (one bad PR doesn't sink the batch).
	PatchFailed PatchStatus = "failed"
)

// RefreshSourceTarget identifies one tenant PR to patch and
// carries the new file content the caller already produced
// (typically from re-running parser + emit on the affected source
// rule subset).
type RefreshSourceTarget struct {
	// PRNumber is the GitHub PR number of the tenant chunk PR
	// being patched.
	PRNumber int `json:"pr_number"`

	// BranchName is the head branch of the tenant PR. The
	// orchestration checks out THIS branch (not main / master);
	// caller is responsible for keeping the value in sync with
	// what GitHub reports.
	BranchName string `json:"branch_name"`

	// Files is the path → new content map for paths that need to
	// change on this branch. POSIX-style paths relative to the
	// repo root (same shape as ApplyInput.ItemFiles).
	//
	// Empty map → orchestration records PatchSkippedNoChange and
	// moves on. Useful when the caller's per-tenant diff produced
	// nothing for this PR (e.g. the source rule fix applied to
	// a different tenant subset).
	Files map[string][]byte `json:"-"`

	// SourceRuleIDs lists the source rule identifiers (from C-8
	// `provenance.source_rule_id`) that drove this patch. Embedded
	// in the commit message + reviewer comment so the audit trail
	// links commits → source rule fixes. Sorted by the caller for
	// deterministic message output; orchestration does not re-sort.
	SourceRuleIDs []string `json:"source_rule_ids,omitempty"`
}

// RefreshSourceInput is the contract from the caller (CLI wrapper).
type RefreshSourceInput struct {
	// Repo identifies the target repo (same shape as ApplyInput.Repo).
	// Required.
	Repo Repo `json:"repo"`

	// Targets is the list of tenant PRs to patch. Caller's
	// responsibility to populate (typically by stashing
	// ApplyResult and the parser+emit re-run output). Empty = no-op
	// (return empty RefreshSourceResult); not an error so a hot-fix
	// run that finds nothing-affected can no-op safely.
	Targets []RefreshSourceTarget `json:"targets"`

	// CommitAuthor is the `Name <email>` string used for the patch
	// commit. Empty falls back to git's configured user.
	CommitAuthor string `json:"commit_author,omitempty"`

	// CommitMessageOverride, when non-empty, replaces the default
	// commit message ("Data-layer hot-fix: re-emitted source rules
	// X, Y, Z"). The default already includes the SourceRuleIDs
	// list; an override is for customers with stricter commit-msg
	// conventions. The literal token `<source-rule-ids>` is
	// substituted with the comma-joined SourceRuleIDs list.
	CommitMessageOverride string `json:"commit_message_override,omitempty"`

	// CommentBody, when non-empty, replaces the default reviewer
	// note. The literal token `<source-rule-ids>` is substituted
	// with the comma-joined SourceRuleIDs list. Empty → default
	// "Data-layer hot-fix applied for source rules X, Y, Z; please
	// re-review the latest commit."
	CommentBody string `json:"comment_body,omitempty"`

	// PostCommentOnSkipped controls whether the orchestration
	// posts a "skipped (PR closed)" comment on closed/merged
	// tenant PRs. Default false — closed PRs don't need new noise.
	PostCommentOnSkipped bool `json:"post_comment_on_skipped,omitempty"`

	// DryRun runs the orchestration without executing the
	// checkout / write / commit / push / comment. Useful for
	// pre-flight review of "what would refresh-source do".
	DryRun bool `json:"dry_run,omitempty"`

	// InterCallDelayMillis softens GitHub secondary rate limits
	// for big batches (same pattern as ApplyInput.InterCallDelayMillis).
	InterCallDelayMillis int `json:"inter_call_delay_ms,omitempty"`
}

// RefreshSourceItemResult is the per-target outcome.
type RefreshSourceItemResult struct {
	// PRNumber + BranchName mirror the input target for easy
	// correlation in the report.
	PRNumber   int    `json:"pr_number"`
	BranchName string `json:"branch_name"`

	// PRState is what GetPR returned (or PRStateUnknown if the
	// pre-flight GetPR call failed before we could decide).
	PRState PRState `json:"pr_state"`

	// Status — see PatchStatus consts.
	Status PatchStatus `json:"status"`

	// FilesUpdated is the count of files written for this target
	// (== len(target.Files) on a successful update; 0 on skip /
	// failed-before-write paths). Surfaced in the report so
	// reviewers see "tenant X got 3 files; tenant Y got 1".
	FilesUpdated int `json:"files_updated"`

	// SourceRuleIDs echoes the input SourceRuleIDs for report
	// rendering — the report groups by source rule + lists
	// affected tenant PRs.
	SourceRuleIDs []string `json:"source_rule_ids,omitempty"`

	// Step records which orchestration step recorded the failure
	// (only populated when Status == PatchFailed): one of "get_pr"
	// / "checkout" / "write" / "commit" / "push" / "comment".
	Step string `json:"step,omitempty"`

	// ErrorMessage is the verbatim error from the underlying call,
	// prefixed with the step name. Populated only on PatchFailed.
	ErrorMessage string `json:"error_message,omitempty"`
}

// RefreshSourceResult is the top-level RefreshSource return.
type RefreshSourceResult struct {
	Items []RefreshSourceItemResult `json:"items"`

	// Summary roll-up for human / log consumption.
	Summary RefreshSourceSummary `json:"summary"`

	// ReportMarkdown is the rendered `patch-plan.md` content.
	// Caller writes this to disk (or surfaces in a workflow
	// summary). Always populated, even for empty Targets — the
	// header explains "no PRs needed patching".
	ReportMarkdown string `json:"report_markdown"`

	// Warnings collects non-fatal issues that don't fit one of the
	// per-item statuses (e.g. PostCommentOnSkipped=true but a
	// CommentPR call failed).
	Warnings []string `json:"warnings,omitempty"`
}

// RefreshSourceSummary is the cheap roll-up.
type RefreshSourceSummary struct {
	TotalTargets    int `json:"total_targets"`
	UpdatedCount    int `json:"updated_count"`
	SkippedCount    int `json:"skipped_count"`
	NoChangeCount   int `json:"no_change_count"`
	DryRunCount     int `json:"dry_run_count"`
	FailedCount     int `json:"failed_count"`
	TotalFilesPatch int `json:"total_files_patched"`
}
