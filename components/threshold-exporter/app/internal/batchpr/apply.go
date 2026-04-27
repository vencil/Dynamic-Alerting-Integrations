package batchpr

// PR-2 — Apply() orchestration.
//
// Walks Plan.Items in order, performing for each item:
//
//   1. Compute deterministic branch name from (BranchPrefix,
//      planHash, item).
//   2. Idempotency check: does the branch already exist on the
//      remote? If yes → check for an existing open PR; if found,
//      record ApplyStatusSkippedExisting and move on. If branch
//      exists but no open PR (e.g. someone closed the PR but left
//      the branch), Apply() also skips with a warning — it does
//      NOT force-push or re-open.
//   3. If DryRun: record ApplyStatusDryRun and skip the rest of
//      this item's work.
//   4. CreateBranch(name, repo.BaseBranch).
//   5. WriteFiles(name, item's files map).
//   6. Commit(name, message, CommitAuthor).
//   7. Push(name).
//   8. OpenPR(title, body, head=name, base=repo.BaseBranch).
//      For tenant items, body still contains the `<base>`
//      placeholder; we rewrite after the base PR opens.
//   9. Sleep InterCallDelayMillis to soften GitHub secondary
//      rate-limit pressure for big batches.
//
// After all items processed:
//
//   - If a base PR opened (Status created OR skipped_existing) AND
//     tenant PRs reference `<base>`, walk every tenant PR and
//     UpdatePRDescription with the base PR number substituted in.
//
// Failure handling: any per-item error becomes ApplyStatusFailed +
// ErrorMessage; the loop continues so a single bad tenant chunk
// doesn't sink the rest of the batch. Apply() never returns a
// hard error after item 0 starts — callers inspect ApplyResult.

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"
)

// Apply executes a Plan against the remote.
//
// Returns:
//   - (*ApplyResult, nil) on every normal flow (including all-failed
//     runs). The result's per-item Status describes what happened.
//   - (nil, error) only on caller-input contract violations
//     (nil Plan, nil clients, etc.) detected before any item work
//     starts.
func Apply(ctx context.Context, in ApplyInput, git GitClient, pr PRClient) (*ApplyResult, error) {
	if err := validateInput(in, git, pr); err != nil {
		return nil, err
	}

	prefix := in.BranchPrefix
	if prefix == "" {
		prefix = defaultBranchPrefix
	}
	planHash := computePlanHash(in.Plan)

	result := &ApplyResult{
		Items: make([]ApplyItemResult, len(in.Plan.Items)),
	}

	tenantPRsAwaitingBase := make([]int, 0, len(in.Plan.Items))

	for i, item := range in.Plan.Items {
		select {
		case <-ctx.Done():
			// Caller cancelled; record remaining items as failed
			// with the cancellation reason and return what we
			// have. The cancel error itself is surfaced via the
			// last filled item's ErrorMessage.
			for j := i; j < len(in.Plan.Items); j++ {
				result.Items[j] = ApplyItemResult{
					PlanItemIndex: j,
					Kind:          in.Plan.Items[j].Kind,
					BranchName:    branchNameFor(prefix, planHash, in.Plan.Items[j]),
					Status:        ApplyStatusFailed,
					ErrorMessage:  fmt.Sprintf("context cancelled before item processed: %v", ctx.Err()),
				}
			}
			finaliseSummary(result)
			return result, nil
		default:
		}

		ir := applyOne(ctx, in, git, pr, prefix, planHash, i, item)
		result.Items[i] = ir
		switch item.Kind {
		case PlanItemBase:
			if ir.Status == ApplyStatusCreated || ir.Status == ApplyStatusSkippedExisting {
				result.BasePRNumber = ir.PRNumber
			}
		case PlanItemTenant:
			if ir.Status == ApplyStatusCreated && containsBasePlaceholder(item.Description) {
				tenantPRsAwaitingBase = append(tenantPRsAwaitingBase, i)
			}
		}

		if in.InterCallDelayMillis > 0 && ir.Status == ApplyStatusCreated {
			// Sleep ONLY after items that actually opened a PR;
			// dry-run / skipped-existing items don't burn rate
			// limit budget so don't pause unnecessarily.
			delay := time.Duration(in.InterCallDelayMillis) * time.Millisecond
			select {
			case <-time.After(delay):
			case <-ctx.Done():
				// Treat cancellation during sleep the same as
				// cancellation before the next item.
			}
		}
	}

	// <base> placeholder substitution. Only attempted when:
	//   - The Base PR ended up with a valid PR number, AND
	//   - At least one tenant PR was created with the placeholder.
	if result.BasePRNumber > 0 && len(tenantPRsAwaitingBase) > 0 {
		for _, idx := range tenantPRsAwaitingBase {
			item := in.Plan.Items[idx]
			body := strings.ReplaceAll(item.Description, "<base>",
				fmt.Sprintf("#%d", result.BasePRNumber))
			if err := pr.UpdatePRDescription(ctx, result.Items[idx].PRNumber, body); err != nil {
				result.Warnings = append(result.Warnings, fmt.Sprintf(
					"failed to substitute <base> placeholder in PR #%d: %v (manual edit needed)",
					result.Items[idx].PRNumber, err))
				continue
			}
			result.Summary.BasePlaceholderRewrites++
		}
	} else if len(tenantPRsAwaitingBase) > 0 {
		// Tenant PRs referenced <base> but the base PR didn't
		// open → leave a clear warning so reviewers know the
		// placeholder is still raw in those PRs.
		result.Warnings = append(result.Warnings, fmt.Sprintf(
			"%d tenant PR(s) still contain the literal `<base>` placeholder because the Base PR did not open successfully",
			len(tenantPRsAwaitingBase)))
	}

	finaliseSummary(result)
	return result, nil
}

// applyOne dispatches one PlanItem through the per-step pipeline.
// Failures at any step are swallowed into ApplyStatusFailed + an
// error message; the orchestration above never sees a hard error
// from this function.
func applyOne(
	ctx context.Context,
	in ApplyInput,
	git GitClient,
	pr PRClient,
	prefix, planHash string,
	itemIdx int,
	item PlanItem,
) ApplyItemResult {
	branch := branchNameFor(prefix, planHash, item)
	out := ApplyItemResult{
		PlanItemIndex: itemIdx,
		Kind:          item.Kind,
		BranchName:    branch,
	}

	files := in.ItemFiles[itemIdx]
	if len(files) == 0 {
		out.Status = ApplyStatusEmptyFiles
		return out
	}

	// Idempotency: branch already on the remote → look up its PR.
	exists, err := git.BranchExistsRemote(ctx, branch)
	if err != nil {
		out.Status = ApplyStatusFailed
		out.ErrorMessage = fmt.Sprintf("branch existence check: %v", err)
		return out
	}
	if exists {
		existing, err := pr.FindPRByBranch(ctx, branch)
		if err != nil {
			out.Status = ApplyStatusFailed
			out.ErrorMessage = fmt.Sprintf("look up existing PR for branch %q: %v", branch, err)
			return out
		}
		out.Status = ApplyStatusSkippedExisting
		if existing != nil {
			out.PRNumber = existing.Number
			out.PRURL = existing.URL
		} else {
			// Anomaly: remote branch exists but no open PR
			// matches it. Possible causes: PR was closed manually
			// without deleting the branch, or another tool
			// pushed a branch with the same name. We refuse to
			// re-push (would violate idempotency contract); the
			// orphan-branch warning surfaces in ErrorMessage so
			// the operator can investigate (typically: delete the
			// remote branch and re-run Apply()).
			out.ErrorMessage = fmt.Sprintf(
				"branch %q exists on remote but has no open PR; left untouched (delete the remote branch and re-run if you want a fresh PR)",
				branch)
		}
		return out
	}

	if in.DryRun {
		out.Status = ApplyStatusDryRun
		return out
	}

	if err := git.CreateBranch(ctx, branch, in.Repo.BaseBranch); err != nil {
		out.Status = ApplyStatusFailed
		out.ErrorMessage = fmt.Sprintf("create branch: %v", err)
		return out
	}
	if err := git.WriteFiles(ctx, branch, files); err != nil {
		out.Status = ApplyStatusFailed
		out.ErrorMessage = fmt.Sprintf("write files: %v", err)
		return out
	}
	commitMsg := commitMessageFor(item)
	if err := git.Commit(ctx, branch, commitMsg, in.CommitAuthor); err != nil {
		out.Status = ApplyStatusFailed
		out.ErrorMessage = fmt.Sprintf("commit: %v", err)
		return out
	}
	if err := git.Push(ctx, branch); err != nil {
		out.Status = ApplyStatusFailed
		out.ErrorMessage = fmt.Sprintf("push: %v", err)
		return out
	}

	opened, err := pr.OpenPR(ctx, OpenPRInput{
		Title: item.Title,
		Body:  item.Description,
		Head:  branch,
		Base:  in.Repo.BaseBranch,
	})
	if err != nil {
		out.Status = ApplyStatusFailed
		out.ErrorMessage = fmt.Sprintf("open PR: %v", err)
		return out
	}
	if opened == nil {
		out.Status = ApplyStatusFailed
		out.ErrorMessage = "open PR returned nil result without error"
		return out
	}
	out.Status = ApplyStatusCreated
	out.PRNumber = opened.Number
	out.PRURL = opened.URL
	return out
}

// validateInput rejects malformed ApplyInput up-front so Apply()
// doesn't have to defend mid-loop.
func validateInput(in ApplyInput, git GitClient, pr PRClient) error {
	if in.Plan == nil {
		return errors.New("apply: ApplyInput.Plan is nil")
	}
	if len(in.Plan.Items) == 0 {
		return errors.New("apply: Plan has zero items")
	}
	if in.Repo.Owner == "" || in.Repo.Name == "" {
		return errors.New("apply: Repo.Owner and Repo.Name are required")
	}
	if in.Repo.BaseBranch == "" {
		return errors.New("apply: Repo.BaseBranch is required")
	}
	if git == nil {
		return errors.New("apply: GitClient is nil")
	}
	if pr == nil {
		return errors.New("apply: PRClient is nil")
	}
	return nil
}

// commitMessageFor builds a deterministic commit message per item.
// Conservative format: title is the PR title; body is a short
// pointer back to the Plan Item index so reviewers + grep'ers can
// trace a commit back to its plan position.
func commitMessageFor(item PlanItem) string {
	return fmt.Sprintf("%s\n\nGenerated by C-10 batchpr.Apply (Plan item kind=%s, chunk_key=%q).",
		item.Title, item.Kind, item.ChunkKey)
}

// containsBasePlaceholder reports whether `s` contains the literal
// `<base>` token PR-1's renderer writes into tenant PR descriptions.
func containsBasePlaceholder(s string) bool {
	return strings.Contains(s, "<base>")
}

// finaliseSummary computes the per-status counts after all items
// have been processed.
func finaliseSummary(r *ApplyResult) {
	r.Summary.TotalItems = len(r.Items)
	for _, it := range r.Items {
		switch it.Status {
		case ApplyStatusCreated:
			r.Summary.CreatedCount++
		case ApplyStatusSkippedExisting:
			r.Summary.SkippedExistingCount++
		case ApplyStatusDryRun:
			r.Summary.DryRunCount++
		case ApplyStatusEmptyFiles:
			r.Summary.EmptyFilesCount++
		case ApplyStatusFailed:
			r.Summary.FailedCount++
		}
	}
}
