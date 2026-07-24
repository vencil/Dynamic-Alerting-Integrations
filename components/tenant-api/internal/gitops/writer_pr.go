package gitops

// PR-mode write-back (ADR-011): instead of committing tenant config to the base
// branch, WritePR / WritePRBatch cut a feature branch, commit there, and push so
// a handler can open a GitHub PR / GitLab MR. Split out of writer.go (Cycle 8
// refactor) so the PR-mode flow reads apart from the direct commit-on-write path
// — no behavior change, pure intra-package move. The base-branch anchoring
// helpers these rely on (base / checkoutBaseClean / resolveFreshBaseRef) and the
// shared admission + commit primitives stay in writer.go.

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// PRWriteResult contains the result of a PR-mode write operation.
type PRWriteResult struct {
	BranchName string // the feature branch name (e.g. "tenant-api/db-a-prod/20260406-143022")
	FilePath   string // the path of the written file
}

// abortFeatureBranch best-effort rolls back a failed PR write: force the
// worktree back to a clean base FIRST, then drop the feature branch.
// The ordering is load-bearing (branch -D refuses to delete the branch we
// are still on / with dirty state). Errors are intentionally ignored —
// callers are already returning the primary failure. Callers hold w.mu.
func (w *Writer) abortFeatureBranch(base, branchName string) {
	_ = w.checkoutBaseClean(base)
	_ = w.gitExec("branch", "-D", branchName)
}

// WritePR validates and writes a tenant config to a feature branch for PR creation.
//
// Unlike Write(), this method:
//  1. Checks out the base branch, then creates a feature branch from it
//  2. Writes the file and commits on the feature branch
//  3. Pushes the branch to origin
//  4. Returns to the base branch + returns the branch name (caller creates the PR)
//
// The caller (handler) is responsible for creating the GitHub PR using the returned branch name.
func (w *Writer) WritePR(ctx context.Context, tenantID, authorEmail, yamlContent string) (*PRWriteResult, error) {
	// Step 0: reserved-id backstop (defense-in-depth; see guardTenantID).
	if err := guardTenantID(tenantID); err != nil {
		return nil, err
	}
	// Step 1: validate schema before anything
	if errs := validate(w.configDir, tenantID, yamlContent); len(errs) > 0 {
		return nil, fmt.Errorf("%w: %s", ErrValidation, strings.Join(errs, "; "))
	}

	// Step 1b: load-shedding admission (TRK-320) before w.mu.
	if err := w.acquireWrite(ctx); err != nil {
		return nil, err
	}
	defer w.releaseWrite()

	w.mu.Lock()
	defer w.mu.Unlock()

	// Step 2: generate branch name
	ts := time.Now().UTC().Format("20060102-150405")
	branchName := fmt.Sprintf("tenant-api/%s/%s", tenantID, ts)

	// Step 3: anchor on a clean base, THEN branch from it. Always checking out the
	// base first (rather than branching from "current HEAD" and returning via the
	// relative `checkout -`) makes cross-tenant branch pollution impossible: even if
	// a prior write left the tree on some feature branch, this re-establishes the
	// base every time (#638). Abort if the base is unreachable — branching from an
	// unknown ref is exactly the bug we're preventing.
	base := w.base()
	if err := w.checkoutBaseClean(base); err != nil {
		return nil, err
	}
	// TRK-318: cut the feature branch from the freshest origin/<base>, NOT the
	// (possibly stale) local base — a long-lived pod's local base stalls after a
	// remote merge and would silently roll back a shared file another tenant
	// already merged. A fetch timeout returns ErrForgeDegraded → lock released →
	// 503 (never proceed on a stale base). See ADR-023 §B.
	branchPoint, err := w.resolveFreshBaseRef(base)
	if err != nil {
		return nil, err
	}
	if err := w.gitExec("checkout", "-b", branchName, "--no-track", branchPoint); err != nil {
		return nil, fmt.Errorf("create branch: %w", err)
	}

	// Step 4: write file
	filePath := filepath.Join(w.configDir, tenantID+".yaml")
	if err := os.WriteFile(filePath, []byte(yamlContent), 0644); err != nil {
		// Rollback: force back to a clean base (the file we just wrote is now a
		// dirty tracked change) + drop the branch.
		w.abortFeatureBranch(base, branchName)
		return nil, fmt.Errorf("write file: %w", err)
	}

	// Step 5: commit on feature branch
	if err := w.gitCommit(filePath, tenantID, authorEmail); err != nil {
		w.abortFeatureBranch(base, branchName)
		return nil, fmt.Errorf("git commit on branch: %w", err)
	}

	// Step 6: push branch to origin
	pushed := true
	if err := w.gitExec("push", "origin", branchName); err != nil {
		pushed = false
		slog.Warn("gitops: push branch failed",
			"branch", branchName, "error", err, "note", "PR creation will fail")
		// Don't delete the branch — the commit is valuable even if push fails
	}

	// Step 7: return to a clean base branch. On failure we only warn: the next
	// WritePR re-anchors on the base at Step 3 regardless, so the tree can never
	// stay stranded on a feature branch and pollute the next tenant's PR.
	if err := w.checkoutBaseClean(base); err != nil {
		slog.Warn("gitops: failed to switch back to base branch",
			"base", base, "branch", branchName, "error", err)
	}

	// Step 8: drop the local feature branch after a CONFIRMED push (#641). The
	// commit is now safely on origin and the PR is created from origin/<branch> —
	// the local ref is no longer needed. Without this, every WritePR leaks a local
	// `tenant-api/<tenant>/<ts>` ref forever (the deployment runs one long-lived
	// replica, so this is the only thing bounding the loose-ref accumulation).
	// On push failure we KEEP the branch (the only copy of the commit) — same as
	// before. Must run AFTER step 7 (can't -D the currently-checked-out branch).
	// Edge: if step 7 itself only warned (still on the feature branch), this -D
	// fails ("checked out branch") and that one branch leaks — bounded by the
	// next WritePR's #638 ironclad re-anchor at step 3.
	if pushed {
		if err := w.gitExec("branch", "-D", branchName); err != nil {
			slog.Warn("gitops: failed to delete local feature branch after push",
				"branch", branchName, "error", err)
		}
	}

	slog.Info("gitops: PR branch created",
		"branch", branchName, "tenant", tenantID, "author", authorEmail)

	return &PRWriteResult{
		BranchName: branchName,
		FilePath:   filePath,
	}, nil
}

// WritePRBatch merges and writes multiple tenant configs to a single feature branch.
// This supports batch PR mode where all changes are consolidated into one PR.
//
// Each op carries a MergeFunc (not pre-built content): the authoritative merge
// runs under the lock against the freshly-checked-out base so a partial patch
// preserves untouched keys (#1097). A pre-flight pass validates every op FIRST,
// against the current on-disk base, so one bad op fails fast (ErrValidation→400,
// #795 F1) without leaving an orphaned feature branch behind.
func (w *Writer) WritePRBatch(ctx context.Context, ops []PRBatchOp, authorEmail string) (*PRWriteResult, error) {
	if len(ops) == 0 {
		return nil, fmt.Errorf("empty batch operations")
	}

	// Load-shedding admission (TRK-320) FIRST — the pre-flight below is per-op
	// disk read + YAML merge + schema validation, real CPU/I/O that must queue
	// behind the single-writer token, not bypass it (#1102 review). Acquiring the
	// token here (before the pre-flight, not just before w.mu) also makes the
	// pre-flight read race-free: the shared token is held for the whole write, so
	// no other write path can mutate the working tree during the pre-flight.
	if err := w.acquireWrite(ctx); err != nil {
		return nil, err
	}
	defer w.releaseWrite()

	// Pre-flight: merge + validate every op against the current on-disk base
	// before cutting a branch. The authoritative merge re-runs under the lock
	// below (against the fresh origin base), but rejecting here keeps a single
	// invalid op from creating a dangling branch and preserves the
	// ErrValidation→400 mapping without requiring a git repo to reach it.
	for _, op := range ops {
		// Reserved-id backstop per op (defense-in-depth; see guardTenantID).
		if err := guardTenantID(op.TenantID); err != nil {
			return nil, err
		}
		if _, _, err := w.readMergeValidate(op.TenantID, op.Merge); err != nil {
			return nil, err
		}
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	ts := time.Now().UTC().Format("20060102-150405")
	branchName := fmt.Sprintf("tenant-api/batch/%s", ts)

	// Anchor on a clean base then branch from it (#638 — see WritePR Step 3).
	base := w.base()
	if err := w.checkoutBaseClean(base); err != nil {
		return nil, err
	}
	// TRK-318: cut from the freshest origin/<base>, not the stale local base
	// (see WritePR Step 3). Fetch timeout → ErrForgeDegraded → lock released → 503.
	branchPoint, err := w.resolveFreshBaseRef(base)
	if err != nil {
		return nil, err
	}
	if err := w.gitExec("checkout", "-b", branchName, "--no-track", branchPoint); err != nil {
		return nil, fmt.Errorf("create branch: %w", err)
	}

	// Merge, write, and commit each op against the freshly-checked-out base.
	// readMergeValidate re-reads the on-disk file per op, so a second op for the
	// same tenant merges onto the first op's just-committed result (not the base).
	// Byte-identical (no-op) merges are skipped so an idempotent patch/retry never
	// churns an empty write; `changed` tracks whether ANY op mutated content.
	changed := false
	for _, op := range ops {
		content, existing, err := w.readMergeValidate(op.TenantID, op.Merge)
		if err != nil {
			w.abortFeatureBranch(base, branchName)
			return nil, err
		}
		if existing != nil && string(existing) == content {
			continue // no-op for this tenant — nothing to write or commit
		}
		filePath := filepath.Join(w.configDir, op.TenantID+".yaml")
		if err := os.WriteFile(filePath, []byte(content), 0644); err != nil {
			w.abortFeatureBranch(base, branchName)
			return nil, fmt.Errorf("write file for %s: %w", op.TenantID, err)
		}
		if err := w.gitCommit(filePath, op.TenantID, authorEmail); err != nil {
			w.abortFeatureBranch(base, branchName)
			return nil, fmt.Errorf("commit for %s: %w", op.TenantID, err)
		}
		changed = true
	}

	// Every op was a no-op → the branch has no commits beyond base. Don't push an
	// empty branch or open a change-free PR/MR (a forge would 422). Roll the
	// branch back and signal the handler to return a clean "no changes" result —
	// the PR-mode analogue of WriteMerged's direct-path no-op success (#1102).
	if !changed {
		w.abortFeatureBranch(base, branchName)
		return nil, ErrNoChanges
	}

	pushed := true
	if err := w.gitExec("push", "origin", branchName); err != nil {
		pushed = false
		slog.Warn("gitops: push batch branch failed",
			"branch", branchName, "error", err)
	}

	if err := w.checkoutBaseClean(base); err != nil {
		slog.Warn("gitops: failed to switch back to base branch",
			"base", base, "branch", branchName, "error", err)
	}

	// Drop the local batch branch after a confirmed push (#641, same rationale as
	// WritePR Step 8). On push failure we keep it — the commit is only local.
	if pushed {
		if err := w.gitExec("branch", "-D", branchName); err != nil {
			slog.Warn("gitops: failed to delete local batch branch after push",
				"branch", branchName, "error", err)
		}
	}

	slog.Info("gitops: PR batch branch created",
		"branch", branchName, "ops", len(ops), "author", authorEmail)

	return &PRWriteResult{
		BranchName: branchName,
	}, nil
}

// PRBatchOp represents a single operation in a PR-mode batch write.
//
// Merge computes the tenant file's new content from its current on-disk bytes
// (#1097) — carrying a MergeFunc rather than pre-built content lets the
// authoritative merge run under the writer lock against the fresh base, so a
// partial patch preserves keys it did not name.
type PRBatchOp struct {
	TenantID string
	Merge    MergeFunc
}
