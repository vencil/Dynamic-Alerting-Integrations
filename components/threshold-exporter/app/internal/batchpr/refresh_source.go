package batchpr

// PR-4 — RefreshSource() orchestration.
//
// Per-target pipeline:
//
//   1. GetPR(target.PRNumber) → state {open, closed, merged}.
//      Closed/merged → record PatchSkippedClosed; optional
//      comment (PostCommentOnSkipped).
//   2. Empty Files → record PatchSkippedNoChange (caller's diff
//      produced no changes for this tenant; not a failure).
//   3. (DryRun) → record PatchDryRun, skip writes.
//   4. CheckoutBranch(target.BranchName) → switch to the existing
//      tenant branch.
//   5. WriteFiles(branch, target.Files) → replace the affected
//      files with the new content.
//   6. Commit(branch, message, author) → single commit referencing
//      SourceRuleIDs in its body.
//   7. Push(branch) → REGULAR push (not force-push). PR-4 adds a
//      new commit on top; rewriting history is unnecessary and
//      would break ongoing reviewer conversations.
//   8. CommentPR(num, body) → reviewer note about the patch.
//      Failure recorded as PatchFailed step="comment" but with a
//      clear "commit + push DID succeed" note in ErrorMessage.
//
// After all targets processed:
//
//   - finaliseRefreshSourceSummary computes per-status counts.
//   - renderRefreshSourceReport builds the patch-plan.md content.
//
// Hard errors vs per-target failures: RefreshSource returns (nil,
// error) only on input contract violations (nil git/pr clients,
// invalid Repo). Any per-target failure becomes PatchFailed in the
// result; the loop continues so a single bad PR doesn't sink the
// batch.

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
)

// RefreshSource walks RefreshSourceInput.Targets and applies the
// per-tenant data-layer hot-fix to each tenant PR's branch.
//
// Empty Targets is NOT an error — a hot-fix run that finds nothing
// affected should be a quiet no-op. The result's Summary
// .TotalTargets reports zero and ReportMarkdown notes "no PRs
// needed patching".
func RefreshSource(ctx context.Context, in RefreshSourceInput, git GitClient, pr PRClient) (*RefreshSourceResult, error) {
	if err := validateRefreshSourceInput(in, git, pr); err != nil {
		return nil, err
	}

	result := &RefreshSourceResult{
		Items: make([]RefreshSourceItemResult, 0, len(in.Targets)),
	}

	for i, target := range in.Targets {
		select {
		case <-ctx.Done():
			// Caller cancelled; record remaining targets as failed
			// with a short cancellation reason and stop.
			for j := i; j < len(in.Targets); j++ {
				result.Items = append(result.Items, RefreshSourceItemResult{
					PRNumber:      in.Targets[j].PRNumber,
					BranchName:    in.Targets[j].BranchName,
					PRState:       PRStateUnknown,
					Status:        PatchFailed,
					Step:          "context",
					SourceRuleIDs: in.Targets[j].SourceRuleIDs,
					ErrorMessage:  fmt.Sprintf("context cancelled before target processed: %v", ctx.Err()),
				})
			}
			finaliseRefreshSourceSummary(result)
			result.ReportMarkdown = renderRefreshSourceReport(in, result)
			return result, nil
		default:
		}

		ir := refreshSourceOne(ctx, in, git, pr, target)
		result.Items = append(result.Items, ir)

		// Soften GitHub secondary rate-limits between targets that
		// actually executed remote work (clean update OR a comment-
		// on-skipped). Skip the sleep when nothing remote happened.
		if in.InterCallDelayMillis > 0 && (ir.Status == PatchUpdated || ir.Status == PatchFailed) {
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
			if ir.Status != PatchSkippedClosed {
				continue
			}
			body := fmt.Sprintf(
				"Data-layer hot-fix: skipped — this PR is %s. No patch applied.",
				ir.PRState)
			if err := pr.CommentPR(ctx, ir.PRNumber, body); err != nil {
				result.Warnings = append(result.Warnings, fmt.Sprintf(
					"failed to post skipped-status comment on PR #%d: %v",
					ir.PRNumber, err))
			}
		}
	}

	finaliseRefreshSourceSummary(result)
	result.ReportMarkdown = renderRefreshSourceReport(in, result)
	return result, nil
}

// refreshSourceOne dispatches one target through the per-step
// pipeline. Failures swallow into PatchFailed + an error message;
// the orchestration above never sees a hard error.
func refreshSourceOne(
	ctx context.Context,
	in RefreshSourceInput,
	git GitClient,
	pr PRClient,
	target RefreshSourceTarget,
) RefreshSourceItemResult {
	out := RefreshSourceItemResult{
		PRNumber:      target.PRNumber,
		BranchName:    target.BranchName,
		PRState:       PRStateUnknown,
		SourceRuleIDs: target.SourceRuleIDs,
	}

	details, err := pr.GetPR(ctx, target.PRNumber)
	if err != nil {
		out.Status = PatchFailed
		out.Step = "get_pr"
		out.ErrorMessage = fmt.Sprintf("get PR #%d: %v", target.PRNumber, err)
		return out
	}
	if details == nil {
		out.Status = PatchFailed
		out.Step = "get_pr"
		out.ErrorMessage = fmt.Sprintf("get PR #%d: nil result without error", target.PRNumber)
		return out
	}
	out.PRState = details.State

	// Closed / merged → skip without patching. The data-layer
	// hot-fix is moot for a non-open PR.
	if details.State == PRStateClosed || details.State == PRStateMerged {
		out.Status = PatchSkippedClosed
		return out
	}
	if details.State != PRStateOpen {
		// Unknown state from a future GitHub API change. Be
		// conservative: skip rather than patch-blind.
		out.Status = PatchSkippedClosed
		return out
	}

	// Empty Files → caller's diff produced nothing for this PR.
	// Record explicitly rather than silently no-op so the report
	// shows "considered but unchanged".
	if len(target.Files) == 0 {
		out.Status = PatchSkippedNoChange
		return out
	}

	if in.DryRun {
		out.Status = PatchDryRun
		out.FilesUpdated = len(target.Files)
		return out
	}

	if err := git.CheckoutBranch(ctx, target.BranchName); err != nil {
		out.Status = PatchFailed
		out.Step = "checkout"
		out.ErrorMessage = fmt.Sprintf("checkout %q: %v", target.BranchName, err)
		return out
	}

	if err := git.WriteFiles(ctx, target.BranchName, target.Files); err != nil {
		out.Status = PatchFailed
		out.Step = "write"
		out.ErrorMessage = fmt.Sprintf("write files: %v", err)
		return out
	}

	commitMsg := patchCommitMessage(in, target)
	if err := git.Commit(ctx, target.BranchName, commitMsg, in.CommitAuthor); err != nil {
		out.Status = PatchFailed
		out.Step = "commit"
		out.ErrorMessage = fmt.Sprintf("commit: %v", err)
		return out
	}

	if err := git.Push(ctx, target.BranchName); err != nil {
		out.Status = PatchFailed
		out.Step = "push"
		out.ErrorMessage = fmt.Sprintf("push: %v", err)
		return out
	}

	body := patchCommentBody(in, target)
	if err := pr.CommentPR(ctx, target.PRNumber, body); err != nil {
		out.Status = PatchFailed
		out.Step = "comment"
		out.FilesUpdated = len(target.Files)
		out.ErrorMessage = fmt.Sprintf(
			"comment on PR #%d: %v (commit + push DID succeed; comment is the only failure)",
			target.PRNumber, err)
		return out
	}

	out.Status = PatchUpdated
	out.FilesUpdated = len(target.Files)
	return out
}

// patchCommitMessage resolves the commit message for one target.
// Default: "Data-layer hot-fix: re-emitted source rules X, Y, Z".
// Override: caller's CommitMessageOverride with `<source-rule-ids>`
// substituted.
func patchCommitMessage(in RefreshSourceInput, target RefreshSourceTarget) string {
	idsList := joinSourceRuleIDs(target.SourceRuleIDs)
	if in.CommitMessageOverride != "" {
		return strings.ReplaceAll(in.CommitMessageOverride, "<source-rule-ids>", idsList)
	}
	if idsList == "" {
		// Edge case: no source rule IDs supplied. Still produce
		// a meaningful message so the commit log isn't cryptic.
		return "Data-layer hot-fix: re-emitted threshold artifacts.\n\nGenerated by C-10 batchpr.RefreshSource."
	}
	return fmt.Sprintf(
		"Data-layer hot-fix: re-emitted source rules %s.\n\nGenerated by C-10 batchpr.RefreshSource.",
		idsList)
}

// patchCommentBody resolves the reviewer-comment body for one
// successful target patch. Mirror pattern to PR-3's
// refreshCommentBody.
func patchCommentBody(in RefreshSourceInput, target RefreshSourceTarget) string {
	idsList := joinSourceRuleIDs(target.SourceRuleIDs)
	if in.CommentBody != "" {
		return strings.ReplaceAll(in.CommentBody, "<source-rule-ids>", idsList)
	}
	if idsList == "" {
		return "Data-layer hot-fix applied — please re-review the latest commit."
	}
	return fmt.Sprintf(
		"Data-layer hot-fix applied for source rules %s — please re-review the latest commit.",
		idsList)
}

// joinSourceRuleIDs renders a comma-joined list, capping the
// rendered length at ~120 chars to keep commit subjects + comments
// readable. Long ID lists collapse to "first, second, ... + N
// more".
func joinSourceRuleIDs(ids []string) string {
	if len(ids) == 0 {
		return ""
	}
	const cap = 120
	full := strings.Join(ids, ", ")
	if len(full) <= cap {
		return full
	}
	// Collapse: take as many as fit before the "... + N more" tail.
	tail := func(n int) string { return fmt.Sprintf(" ... + %d more", n) }
	for take := len(ids) - 1; take >= 1; take-- {
		head := strings.Join(ids[:take], ", ")
		if len(head)+len(tail(len(ids)-take)) <= cap {
			return head + tail(len(ids)-take)
		}
	}
	// Even one entry is too long; just return the first one
	// untruncated and report the rest in the tail.
	return ids[0] + tail(len(ids)-1)
}

// validateRefreshSourceInput rejects malformed RefreshSourceInput.
func validateRefreshSourceInput(in RefreshSourceInput, git GitClient, pr PRClient) error {
	if in.Repo.Owner == "" || in.Repo.Name == "" {
		return errors.New("refresh-source: Repo.Owner and Repo.Name are required")
	}
	if in.Repo.BaseBranch == "" {
		return errors.New("refresh-source: Repo.BaseBranch is required")
	}
	if git == nil {
		return errors.New("refresh-source: GitClient is nil")
	}
	if pr == nil {
		return errors.New("refresh-source: PRClient is nil")
	}
	for i, t := range in.Targets {
		if t.PRNumber <= 0 {
			return fmt.Errorf("refresh-source: Targets[%d].PRNumber must be > 0", i)
		}
		if t.BranchName == "" {
			return fmt.Errorf("refresh-source: Targets[%d].BranchName is required", i)
		}
	}
	return nil
}

// finaliseRefreshSourceSummary computes the per-status counts.
func finaliseRefreshSourceSummary(r *RefreshSourceResult) {
	r.Summary.TotalTargets = len(r.Items)
	for _, it := range r.Items {
		switch it.Status {
		case PatchUpdated:
			r.Summary.UpdatedCount++
			r.Summary.TotalFilesPatch += it.FilesUpdated
		case PatchSkippedClosed:
			r.Summary.SkippedCount++
		case PatchSkippedNoChange:
			r.Summary.NoChangeCount++
		case PatchDryRun:
			r.Summary.DryRunCount++
			r.Summary.TotalFilesPatch += it.FilesUpdated
		case PatchFailed:
			r.Summary.FailedCount++
		}
	}
}

// renderRefreshSourceReport builds patch-plan.md content. Stable
// across runs given the same input.
//
// Structure:
//   - Header (repo / mode / total source rules / total tenants)
//   - Summary counts
//   - Per-PR outcomes table
//   - Source-rules → tenant PRs cross-reference (helpful when one
//     hot-fix touches many PRs and the reviewer wants to see which
//     rule maps to which tenant).
//   - Warnings section.
func renderRefreshSourceReport(in RefreshSourceInput, r *RefreshSourceResult) string {
	out := strings.Builder{}
	out.WriteString("# Patch plan (data-layer hot-fix)\n\n")
	out.WriteString(fmt.Sprintf("**Repo**: `%s`\n", in.Repo.FullName()))
	if in.DryRun {
		out.WriteString("**Mode**: dry-run (no checkout / write / commit / push / comment executed)\n")
	}
	out.WriteString(fmt.Sprintf("**Targets**: %d tenant PR(s)\n", len(in.Targets)))
	allRuleIDs := uniqueSourceRuleIDs(in.Targets)
	out.WriteString(fmt.Sprintf("**Source rules**: %d unique rule ID(s)\n\n", len(allRuleIDs)))

	if len(r.Items) == 0 {
		out.WriteString("No tenant PRs were targeted; refresh-source is a no-op.\n")
		return out.String()
	}

	out.WriteString("## Summary\n\n")
	out.WriteString(fmt.Sprintf("- Total targets: %d\n", r.Summary.TotalTargets))
	out.WriteString(fmt.Sprintf("- Updated: %d (%d file(s) total)\n",
		r.Summary.UpdatedCount, r.Summary.TotalFilesPatch))
	out.WriteString(fmt.Sprintf("- Skipped (closed / merged): %d\n", r.Summary.SkippedCount))
	out.WriteString(fmt.Sprintf("- No change (caller diff was empty): %d\n", r.Summary.NoChangeCount))
	if r.Summary.DryRunCount > 0 {
		out.WriteString(fmt.Sprintf("- Dry-run: %d\n", r.Summary.DryRunCount))
	}
	if r.Summary.FailedCount > 0 {
		out.WriteString(fmt.Sprintf("- Failed: %d\n", r.Summary.FailedCount))
	}
	out.WriteString("\n")

	out.WriteString("## Per-PR outcomes\n\n")
	out.WriteString("| PR | Branch | State | Status | Files | Notes |\n")
	out.WriteString("|----|--------|-------|--------|-------|-------|\n")
	for _, it := range r.Items {
		notes := "—"
		switch it.Status {
		case PatchFailed:
			notes = fmt.Sprintf("step=%s: %s", it.Step, it.ErrorMessage)
		case PatchDryRun:
			notes = "would update + commit + push + comment"
		case PatchSkippedNoChange:
			notes = "caller's diff was empty for this tenant"
		}
		out.WriteString(fmt.Sprintf("| #%d | `%s` | %s | %s | %d | %s |\n",
			it.PRNumber, it.BranchName, it.PRState, it.Status, it.FilesUpdated, notes))
	}
	out.WriteString("\n")

	// Source-rules → tenant PRs cross-reference. Useful when one
	// hot-fix touches many PRs and the reviewer wants to see which
	// rule maps to which tenant. Sorted by source rule ID then by
	// PR number for stable output.
	if len(allRuleIDs) > 0 {
		ruleToPRs := buildRuleToPRsIndex(in.Targets)
		out.WriteString("## Source-rules → tenant PRs\n\n")
		for _, ruleID := range allRuleIDs {
			prs := ruleToPRs[ruleID]
			sort.Ints(prs)
			prStrs := make([]string, len(prs))
			for i, n := range prs {
				prStrs[i] = fmt.Sprintf("#%d", n)
			}
			out.WriteString(fmt.Sprintf("- `%s` → %s\n",
				ruleID, strings.Join(prStrs, ", ")))
		}
		out.WriteString("\n")
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

// uniqueSourceRuleIDs returns the unique sorted union of source
// rule IDs across all targets. Used by the report's header summary
// + the "source-rules → tenant PRs" cross-reference section.
func uniqueSourceRuleIDs(targets []RefreshSourceTarget) []string {
	seen := make(map[string]struct{})
	for _, t := range targets {
		for _, id := range t.SourceRuleIDs {
			seen[id] = struct{}{}
		}
	}
	out := make([]string, 0, len(seen))
	for id := range seen {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

// buildRuleToPRsIndex inverts the targets' (PR → rule IDs) shape
// into a (rule ID → PR numbers) lookup so the report's cross-
// reference section can render efficiently.
func buildRuleToPRsIndex(targets []RefreshSourceTarget) map[string][]int {
	idx := make(map[string][]int)
	for _, t := range targets {
		for _, id := range t.SourceRuleIDs {
			idx[id] = append(idx[id], t.PRNumber)
		}
	}
	return idx
}
