package batchpr

// PR-3 — Refresh after Base PR merges (Rebase Hell solver).
//
// The Rebase Hell scenario PR-3 unblocks
// --------------------------------------
// PR-2's Apply() opens a Base Infrastructure PR (carrying
// `_defaults.yaml` changes for N profiles) plus M tenant chunk PRs
// (each declaring "Blocked by Base"). After human review, the Base
// PR merges into main. The tenant PRs now have a problem:
//
//   - Their branches were created off main BEFORE the Base PR's
//     `_defaults.yaml` content existed there. Once the Base PR
//     merges, GitHub shows each tenant PR's diff as if it adds the
//     `_defaults.yaml` content (already in main!) on top of the
//     tenant override. Reviewers can't tell what's new vs. what's
//     already-merged.
//   - Tenant PRs need `git rebase --onto <merged-base-sha>` to
//     re-anchor onto the new main. With M tenant PRs, doing this
//     by hand is the "Rebase Hell" step in customer-anon onboarding.
//
// PR-3 ships the orchestration that walks the M tenant PRs in
// batch:
//
//   1. For each tenant PR, fetch its current state (open / closed /
//      merged). Closed/merged → skip silently.
//   2. Rebase the tenant branch onto the merged main HEAD.
//   3. If clean → force-push-with-lease and post a "rebased onto
//      merged main" comment on the PR.
//   4. If conflicts → abort the rebase, leave the branch untouched,
//      collect conflict-file list for the report.
//   5. Generate `refresh-report.md` summarising per-PR outcomes +
//      conflict details.
//
// PR-4 (next) will add `refresh --source-rule-ids` for data-layer
// hot-fixes (re-emit a specific subset of rules without re-running
// the whole import). PR-3 is the simpler, more common case: Base
// merged, get tenant PRs caught up.
//
// Why caller passes the tenant PR list explicitly
// -----------------------------------------------
// We don't auto-discover tenant PRs from GitHub by scanning
// "Blocked by" text. Two reasons:
//
//   - Brittle: GitHub PR descriptions get edited (reviewers add
//     comments-as-notes, automation rewrites). Scanning for a
//     specific marker text drifts.
//   - Fragile: a PR description scan can't tell "was blocked by
//     this base" from "mentions this base PR number for unrelated
//     reasons". False positives sneak in over time.
//
// The CLI / workflow wrapper (PR-5 territory) is responsible for
// recording the Base ↔ tenant PR linkage at apply time (one row
// per ApplyResult.Items[i] is enough) and feeding it back to
// Refresh() as a typed RefreshTarget list. This keeps Refresh
// pure-Go orchestration with no string-scrape heuristics.

// RebaseStatus discriminates the per-tenant-PR outcome of
// `git rebase --onto`. Three values keep the code simple while
// covering the meaningful states reviewers + automation care
// about.
type RebaseStatus string

const (
	// RebaseClean — the rebase applied without conflicts and the
	// rebased branch was force-pushed (or would be in DryRun).
	RebaseClean RebaseStatus = "clean"

	// RebaseConflicts — `git rebase --onto` reported merge
	// conflicts. The orchestration aborts the in-flight rebase
	// (`git rebase --abort`) so the working tree is left in a
	// clean state; the tenant branch on disk + remote is
	// unchanged. ConflictedFiles lists the files git named.
	RebaseConflicts RebaseStatus = "conflicts"

	// RebaseSkippedClosed — the tenant PR is closed/merged on
	// GitHub; we skip the rebase entirely (no point updating a
	// non-open PR). PRState carries "closed" or "merged" for the
	// report.
	RebaseSkippedClosed RebaseStatus = "skipped_closed"

	// RebaseDryRun — DryRun=true; the rebase + push were not
	// executed. ConflictedFiles is empty; PRState reflects whatever
	// GetPR returned (we still query state in DryRun so reviewers
	// see which tenant PRs the apply would touch).
	RebaseDryRun RebaseStatus = "dry_run"

	// RebaseFailed — something went wrong before / after the
	// rebase itself: GetPR network error, ForcePushWithLease
	// failed, CommentPR failed. ErrorMessage explains. The branch
	// state may or may not have been updated; the orchestration
	// notes which step in `Step` so operators can recover.
	RebaseFailed RebaseStatus = "failed"
)

// RefreshTarget identifies one tenant PR the Refresh orchestration
// should rebase. Typically constructed from PR-2's ApplyResult by
// the CLI wrapper.
type RefreshTarget struct {
	// PRNumber is the GitHub PR number of the tenant chunk PR
	// (ApplyResult.Items[i].PRNumber).
	PRNumber int `json:"pr_number"`

	// BranchName is the head branch of the tenant PR
	// (ApplyResult.Items[i].BranchName). Refresh orchestration
	// won't try to derive this from PRNumber via GetPR — too easy
	// to get wrong if the head branch differs from what we
	// recorded; pass it in.
	BranchName string `json:"branch_name"`

	// OldBase is the branch / SHA that was the original base when
	// the tenant PR was opened. Typically Repo.BaseBranch (i.e.
	// "main"). For `git rebase --onto NEW OLD branch`, this is
	// OLD — usually the same as Repo.BaseBranch since tenant PRs
	// were opened against main; the Rebase Hell exists because
	// commits between Apply and Base merge made main move.
	//
	// Empty falls back to Repo.BaseBranch in the orchestration
	// layer.
	OldBase string `json:"old_base,omitempty"`
}

// RefreshInput is the contract from the caller (CLI wrapper).
type RefreshInput struct {
	// Repo identifies the target repo (same shape as ApplyInput.Repo).
	// Required.
	Repo Repo `json:"repo"`

	// BaseMergedSHA is the merge commit SHA the Base Infrastructure
	// PR landed on main. The orchestration rebases each tenant
	// branch onto this SHA (so tenant diffs no longer carry the
	// Base PR's `_defaults.yaml` content). Required.
	BaseMergedSHA string `json:"base_merged_sha"`

	// BaseMergedPRNumber is the PR number of the merged Base PR
	// (used when posting "rebased onto #X" comments on tenant PRs).
	// Optional but recommended — falls back to "the merged base"
	// in comments when zero.
	BaseMergedPRNumber int `json:"base_merged_pr_number,omitempty"`

	// Targets is the list of tenant PRs to refresh. Caller's
	// responsibility to populate (typically by stashing
	// ApplyResult and feeding back the per-tenant rows). Empty =
	// no-op (return empty RefreshResult); not an error so a
	// scheduled refresh job that finds nothing-to-do can no-op
	// safely.
	Targets []RefreshTarget `json:"targets"`

	// CommentBody, when non-empty, replaces the orchestration's
	// default rebase-success comment. Useful for customer-specific
	// boilerplate ("re-review at your convenience" etc.). The
	// orchestration substitutes the literal token `<base-sha>` if
	// present.
	CommentBody string `json:"comment_body,omitempty"`

	// PostCommentOnSkipped controls whether the orchestration
	// posts a "skipped (PR closed)" comment on closed/merged
	// tenant PRs. Default false — closed PRs don't need new noise.
	PostCommentOnSkipped bool `json:"post_comment_on_skipped,omitempty"`

	// DryRun runs the orchestration without executing the rebase
	// or push or comment. RefreshItemResult.Status is RebaseDryRun
	// for tenant PRs that would have been processed; intended for
	// pre-flight review of "what would refresh do".
	DryRun bool `json:"dry_run,omitempty"`

	// InterCallDelayMillis softens GitHub secondary rate limits
	// for big batches (same pattern as ApplyInput.InterCallDelayMillis).
	InterCallDelayMillis int `json:"inter_call_delay_ms,omitempty"`
}

// PRState describes whether a PR is open / closed / merged at the
// moment Refresh observed it. Returned by PRClient.GetPR; used by
// the orchestration to decide whether to skip + reported in
// RefreshItemResult for the report.
type PRState string

const (
	PRStateOpen    PRState = "open"
	PRStateClosed  PRState = "closed"
	PRStateMerged  PRState = "merged"
	PRStateUnknown PRState = "unknown"
)

// RefreshItemResult is the per-target outcome of a Refresh run.
type RefreshItemResult struct {
	// PRNumber + BranchName mirror the input target for easy
	// correlation in the report.
	PRNumber   int    `json:"pr_number"`
	BranchName string `json:"branch_name"`

	// PRState is what GetPR returned (or PRStateUnknown if the
	// pre-flight GetPR call failed before we could decide).
	PRState PRState `json:"pr_state"`

	// Status — see RebaseStatus consts.
	Status RebaseStatus `json:"status"`

	// ConflictedFiles is populated only when Status == RebaseConflicts.
	// Empty otherwise. Sorted for stable report output.
	ConflictedFiles []string `json:"conflicted_files,omitempty"`

	// Step records which orchestration step recorded the failure
	// (only populated when Status == RebaseFailed): one of
	// "get_pr" / "rebase" / "push" / "comment". Lets operators
	// pinpoint the recovery action.
	Step string `json:"step,omitempty"`

	// ErrorMessage is the verbatim error from the underlying call,
	// prefixed with the step name. Populated only on RebaseFailed.
	ErrorMessage string `json:"error_message,omitempty"`
}

// RefreshResult is the top-level Refresh return.
type RefreshResult struct {
	Items []RefreshItemResult `json:"items"`

	// Summary roll-up for human / log consumption.
	Summary RefreshSummary `json:"summary"`

	// ReportMarkdown is the rendered `refresh-report.md` content.
	// Caller writes this to disk (or surfaces in a workflow
	// summary). Always populated, even for empty Targets — the
	// header explains "no PRs needed refresh".
	ReportMarkdown string `json:"report_markdown"`

	// Warnings collects non-fatal issues that don't fit one of the
	// per-item statuses (e.g. PostCommentOnSkipped=true but a
	// CommentPR call failed).
	Warnings []string `json:"warnings,omitempty"`
}

// RefreshSummary is the cheap roll-up.
type RefreshSummary struct {
	TotalTargets   int `json:"total_targets"`
	CleanCount     int `json:"clean_count"`
	ConflictsCount int `json:"conflicts_count"`
	SkippedCount   int `json:"skipped_count"`
	DryRunCount    int `json:"dry_run_count"`
	FailedCount    int `json:"failed_count"`
}
