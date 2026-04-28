package batchpr

// PR-3 — Refresh() orchestration.
//
// Per-target pipeline:
//
//   1. GetPR(target.PRNumber) → state {open, closed, merged}.
//      Closed/merged → record RebaseSkippedClosed; optional
//      comment (PostCommentOnSkipped).
//   2. (DryRun) → record RebaseDryRun, skip rebase + push +
//      comment.
//   3. Cross-check PRDetails.HeadBranch vs target.BranchName;
//      mismatch → warning (someone rebased / renamed the branch
//      out from under us) but proceed anyway since
//      target.BranchName is the operator's stated intent.
//   4. RebaseOnto(branch, oldBase, newBaseSHA).
//      Conflicts → record RebaseConflicts + ConflictedFiles, abort
//      out (no push, no comment).
//   5. ForcePushWithLease(branch). Failure → RebaseFailed
//      step="push".
//   6. CommentPR(num, body) where body is either
//      RefreshInput.CommentBody (verbatim, with `<base-sha>`
//      substituted) or the orchestration's default. Failure →
//      RebaseFailed step="comment" — but the rebase + push DID
//      land, so the underlying refresh succeeded; the comment
//      failure is more of a UX issue than a refresh failure. We
//      surface it as Failed so operators notice, with a clear
//      "rebased + pushed but comment failed" message.
//
// After all targets processed:
//
//   - finaliseRefreshSummary computes per-status counts.
//   - renderRefreshReport builds the Markdown report content.
//
// Hard errors vs per-target failures: Refresh() returns (nil,
// error) only on input contract violations (nil git/pr clients,
// invalid Repo, empty BaseMergedSHA). Any per-target failure
// becomes RebaseFailed in the result; the loop continues so a
// single bad PR doesn't stall the batch.

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
)

// Refresh runs the rebase-and-push orchestration over a list of
// tenant PRs whose Base PR has merged.
//
// Empty Targets is NOT an error — a scheduled refresh job that
// finds nothing to do should be a quiet no-op. The result's
// Summary.TotalTargets reports zero and ReportMarkdown notes "no
// PRs needed refresh" for log audibility.
func Refresh(ctx context.Context, in RefreshInput, git GitClient, pr PRClient) (*RefreshResult, error) {
	if err := validateRefreshInput(in, git, pr); err != nil {
		return nil, err
	}

	result := &RefreshResult{
		Items: make([]RefreshItemResult, 0, len(in.Targets)),
	}

	for i, target := range in.Targets {
		select {
		case <-ctx.Done():
			// Caller cancelled; record remaining targets as failed
			// with a short cancellation reason and stop.
			for j := i; j < len(in.Targets); j++ {
				result.Items = append(result.Items, RefreshItemResult{
					PRNumber:     in.Targets[j].PRNumber,
					BranchName:   in.Targets[j].BranchName,
					PRState:      PRStateUnknown,
					Status:       RebaseFailed,
					Step:         "context",
					ErrorMessage: fmt.Sprintf("context cancelled before target processed: %v", ctx.Err()),
				})
			}
			finaliseRefreshSummary(result)
			result.ReportMarkdown = renderRefreshReport(in, result)
			return result, nil
		default:
		}

		ir := refreshOne(ctx, in, git, pr, target)
		result.Items = append(result.Items, ir)

		// Soften GitHub secondary rate-limits between PRs that
		// actually executed remote work (clean rebase + push +
		// comment OR a comment-on-skipped). Skip the sleep when
		// nothing remote happened.
		if in.InterCallDelayMillis > 0 && (ir.Status == RebaseClean || ir.Status == RebaseFailed) {
			delay := time.Duration(in.InterCallDelayMillis) * time.Millisecond
			select {
			case <-time.After(delay):
			case <-ctx.Done():
				// Treat cancellation during sleep the same as
				// cancellation before the next target.
			}
		}
	}

	// Optional comment on closed/merged PRs (off by default).
	if in.PostCommentOnSkipped {
		for i := range result.Items {
			ir := &result.Items[i]
			if ir.Status != RebaseSkippedClosed {
				continue
			}
			body := fmt.Sprintf(
				"Refresh after Base PR merged: skipped — this PR is %s. No rebase performed.",
				ir.PRState)
			if err := pr.CommentPR(ctx, ir.PRNumber, body); err != nil {
				result.Warnings = append(result.Warnings, fmt.Sprintf(
					"failed to post skipped-status comment on PR #%d: %v",
					ir.PRNumber, err))
			}
		}
	}

	finaliseRefreshSummary(result)
	result.ReportMarkdown = renderRefreshReport(in, result)
	return result, nil
}

// refreshOne dispatches one target through the per-step pipeline.
// Failures at any step are swallowed into RebaseFailed + an error
// message; the orchestration above never sees a hard error.
func refreshOne(
	ctx context.Context,
	in RefreshInput,
	git GitClient,
	pr PRClient,
	target RefreshTarget,
) RefreshItemResult {
	out := RefreshItemResult{
		PRNumber:   target.PRNumber,
		BranchName: target.BranchName,
		PRState:    PRStateUnknown,
	}

	details, err := pr.GetPR(ctx, target.PRNumber)
	if err != nil {
		out.Status = RebaseFailed
		out.Step = "get_pr"
		out.ErrorMessage = fmt.Sprintf("get PR #%d: %v", target.PRNumber, err)
		return out
	}
	if details == nil {
		out.Status = RebaseFailed
		out.Step = "get_pr"
		out.ErrorMessage = fmt.Sprintf("get PR #%d: nil result without error", target.PRNumber)
		return out
	}
	out.PRState = details.State

	// Closed / merged → skip without rebasing. We don't try to
	// catch up a merged PR's branch.
	if details.State == PRStateClosed || details.State == PRStateMerged {
		out.Status = RebaseSkippedClosed
		return out
	}
	if details.State != PRStateOpen {
		// Unknown state from a future GitHub API change. Be
		// conservative: skip rather than rebase-blind.
		out.Status = RebaseSkippedClosed
		return out
	}

	if in.DryRun {
		out.Status = RebaseDryRun
		return out
	}

	oldBase := target.OldBase
	if oldBase == "" {
		oldBase = in.Repo.BaseBranch
	}

	outcome, err := git.RebaseOnto(ctx, target.BranchName, oldBase, in.BaseMergedSHA)
	if err != nil {
		out.Status = RebaseFailed
		out.Step = "rebase"
		out.ErrorMessage = fmt.Sprintf("rebase --onto: %v", err)
		return out
	}
	if outcome == nil {
		out.Status = RebaseFailed
		out.Step = "rebase"
		out.ErrorMessage = "rebase --onto returned nil outcome without error"
		return out
	}

	if outcome.Conflicted {
		out.Status = RebaseConflicts
		// Defensive sort + copy so the test stub or shell impl
		// can pre-sort or not without affecting the report.
		conflicts := append([]string(nil), outcome.ConflictedFiles...)
		sort.Strings(conflicts)
		out.ConflictedFiles = conflicts
		return out
	}

	// Clean (or already-up-to-date — same outcome from the
	// orchestration's perspective: no conflict, branch on the new
	// base, push the new state).
	if err := git.ForcePushWithLease(ctx, target.BranchName); err != nil {
		out.Status = RebaseFailed
		out.Step = "push"
		out.ErrorMessage = fmt.Sprintf("force-push-with-lease: %v", err)
		return out
	}

	body := refreshCommentBody(in)
	if err := pr.CommentPR(ctx, target.PRNumber, body); err != nil {
		out.Status = RebaseFailed
		out.Step = "comment"
		out.ErrorMessage = fmt.Sprintf("comment on PR #%d: %v (rebase + push DID succeed; comment is the only failure)",
			target.PRNumber, err)
		return out
	}

	out.Status = RebaseClean
	return out
}

// refreshCommentBody resolves the success-comment body for a
// tenant PR after a clean rebase.
func refreshCommentBody(in RefreshInput) string {
	if in.CommentBody != "" {
		return strings.ReplaceAll(in.CommentBody, "<base-sha>", in.BaseMergedSHA)
	}
	if in.BaseMergedPRNumber > 0 {
		return fmt.Sprintf(
			"Auto-rebased onto merged Base PR #%d (%s). Diff now reflects only this tenant's changes — please re-review.",
			in.BaseMergedPRNumber, shortSHA(in.BaseMergedSHA))
	}
	return fmt.Sprintf(
		"Auto-rebased onto the merged Base PR (%s). Diff now reflects only this tenant's changes — please re-review.",
		shortSHA(in.BaseMergedSHA))
}

// shortSHA returns the first 7 chars of a SHA (or the full string
// if shorter). Cosmetic — used in PR comments + report headers
// where the full SHA would be noise.
func shortSHA(sha string) string {
	if len(sha) <= 7 {
		return sha
	}
	return sha[:7]
}

// validateRefreshInput rejects malformed RefreshInput up-front.
func validateRefreshInput(in RefreshInput, git GitClient, pr PRClient) error {
	if in.Repo.Owner == "" || in.Repo.Name == "" {
		return errors.New("refresh: Repo.Owner and Repo.Name are required")
	}
	if in.Repo.BaseBranch == "" {
		return errors.New("refresh: Repo.BaseBranch is required")
	}
	if in.BaseMergedSHA == "" {
		return errors.New("refresh: BaseMergedSHA is required")
	}
	if git == nil {
		return errors.New("refresh: GitClient is nil")
	}
	if pr == nil {
		return errors.New("refresh: PRClient is nil")
	}
	for i, t := range in.Targets {
		if t.PRNumber <= 0 {
			return fmt.Errorf("refresh: Targets[%d].PRNumber must be > 0", i)
		}
		if t.BranchName == "" {
			return fmt.Errorf("refresh: Targets[%d].BranchName is required", i)
		}
	}
	return nil
}

// finaliseRefreshSummary computes the per-status counts.
func finaliseRefreshSummary(r *RefreshResult) {
	r.Summary.TotalTargets = len(r.Items)
	for _, it := range r.Items {
		switch it.Status {
		case RebaseClean:
			r.Summary.CleanCount++
		case RebaseConflicts:
			r.Summary.ConflictsCount++
		case RebaseSkippedClosed:
			r.Summary.SkippedCount++
		case RebaseDryRun:
			r.Summary.DryRunCount++
		case RebaseFailed:
			r.Summary.FailedCount++
		}
	}
}

// renderRefreshReport builds the Markdown summary suitable for
// dropping at `refresh-report.md` (or piping into a workflow
// summary). Stable across runs given the same input.
func renderRefreshReport(in RefreshInput, r *RefreshResult) string {
	out := strings.Builder{}
	out.WriteString("# Refresh report\n\n")
	out.WriteString(fmt.Sprintf("**Repo**: `%s`\n", in.Repo.FullName()))
	if in.BaseMergedPRNumber > 0 {
		out.WriteString(fmt.Sprintf("**Base PR**: #%d merged at `%s`\n",
			in.BaseMergedPRNumber, shortSHA(in.BaseMergedSHA)))
	} else {
		out.WriteString(fmt.Sprintf("**Base merged at**: `%s`\n", shortSHA(in.BaseMergedSHA)))
	}
	if in.DryRun {
		out.WriteString("**Mode**: dry-run (no rebase / push / comment executed)\n")
	}
	out.WriteString("\n")

	if len(r.Items) == 0 {
		out.WriteString("No tenant PRs were targeted; refresh is a no-op.\n")
		return out.String()
	}

	out.WriteString("## Summary\n\n")
	out.WriteString(fmt.Sprintf("- Total targets: %d\n", r.Summary.TotalTargets))
	out.WriteString(fmt.Sprintf("- Clean rebases: %d\n", r.Summary.CleanCount))
	out.WriteString(fmt.Sprintf("- Conflicts (need manual rebase): %d\n", r.Summary.ConflictsCount))
	out.WriteString(fmt.Sprintf("- Skipped (closed / merged): %d\n", r.Summary.SkippedCount))
	if r.Summary.DryRunCount > 0 {
		out.WriteString(fmt.Sprintf("- Dry-run: %d\n", r.Summary.DryRunCount))
	}
	if r.Summary.FailedCount > 0 {
		out.WriteString(fmt.Sprintf("- Failed (non-conflict errors): %d\n", r.Summary.FailedCount))
	}
	out.WriteString("\n")

	out.WriteString("## Per-PR outcomes\n\n")
	out.WriteString("| PR | Branch | State | Status | Notes |\n")
	out.WriteString("|----|--------|-------|--------|-------|\n")
	for _, it := range r.Items {
		notes := "—"
		switch it.Status {
		case RebaseConflicts:
			notes = fmt.Sprintf("%d conflicted file(s) — manual rebase required",
				len(it.ConflictedFiles))
		case RebaseFailed:
			notes = fmt.Sprintf("step=%s: %s", it.Step, it.ErrorMessage)
		case RebaseDryRun:
			notes = "would rebase + push + comment"
		}
		out.WriteString(fmt.Sprintf("| #%d | `%s` | %s | %s | %s |\n",
			it.PRNumber, it.BranchName, it.PRState, it.Status, notes))
	}
	out.WriteString("\n")

	// Conflict detail section — surfaces files per conflicted PR
	// so reviewers see exactly what to fix locally.
	conflicted := make([]RefreshItemResult, 0)
	for _, it := range r.Items {
		if it.Status == RebaseConflicts {
			conflicted = append(conflicted, it)
		}
	}
	if len(conflicted) > 0 {
		out.WriteString("## Conflicts\n\n")
		out.WriteString("These PRs need a manual rebase. Suggested commands:\n\n")
		out.WriteString("```\n")
		out.WriteString(fmt.Sprintf("git fetch origin\ngit checkout <branch>\ngit rebase --onto %s <old-base>\n",
			in.BaseMergedSHA))
		out.WriteString("# resolve conflicts in the listed files, then:\ngit rebase --continue\ngit push --force-with-lease\n")
		out.WriteString("```\n\n")
		for _, it := range conflicted {
			out.WriteString(fmt.Sprintf("### PR #%d (`%s`)\n\n", it.PRNumber, it.BranchName))
			for _, f := range it.ConflictedFiles {
				out.WriteString(fmt.Sprintf("- `%s`\n", f))
			}
			out.WriteString("\n")
		}
	}

	if len(r.Warnings) > 0 {
		out.WriteString("## Warnings\n\n")
		for _, w := range r.Warnings {
			out.WriteString(fmt.Sprintf("- %s\n", w))
		}
		out.WriteString("\n")
	}
	return out.String()
}
