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
	"strings"
	"time"
)

// PRWriteResult contains the result of a PR-mode write operation.
type PRWriteResult struct {
	BranchName string // the feature branch name (e.g. "tenant-api/db-a-prod/20260406-143022")
	FilePath   string // the path of the written file
	// Notices is validate()'s advisory deprecation channel (#1231 1b) —
	// non-blocking deprecated-key alias advisories the handler surfaces in
	// its 200 pending_review response. For WritePRBatch it aggregates every
	// op's notices; each message already self-identifies its tenant
	// (`tenant=<id>` is part of the ValidateTenantKeys wording).
	Notices []string
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
	// Step 1: PRE-FLIGHT validate, before anything touches git.
	//
	// ⛔ THIS IS NOT THE AUTHORITATIVE RUN — Step 3c is. Everything stateful in
	// validate (the addedTenantKeys baseline, the eol-expansion guard) reads the
	// tenant file on the tree as it is RIGHT NOW, and Step 3 replaces that tree
	// with the fresh origin/<base>. Keeping this pre-flight anyway mirrors
	// WritePRBatch, and for its reason: it maps a bad body to ErrValidation→400
	// without needing a git repo to reach it, and stops an invalid write from
	// cutting a dangling branch. Its notices are DISCARDED — Step 3c collects
	// them once, against the tree the write lands on.
	filePath, err := w.tenantFilePath(tenantID)
	if err != nil {
		return nil, err
	}
	if errs, _ := validate(w.configDir, tenantID, filePath, yamlContent); len(errs) > 0 {
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

	// Step 3b: RE-resolve the tenant's file now that the feature branch is
	// checked out (#1673). The path resolved at Step 1 describes the tree as
	// it was BEFORE checkoutBaseClean + resolveFreshBaseRef; if the fresh base
	// carries a rename (`db-a.yml` → `db-a.yaml`, or the reverse), writing the
	// pre-checkout path would recreate the old spelling beside the new one —
	// the exact duplicate this change exists to prevent. WritePRBatch already
	// resolves inside its post-checkout loop; this brings the single-tenant
	// path in line.
	filePath, err = w.tenantFilePath(tenantID)
	if err != nil {
		w.abortFeatureBranch(base, branchName)
		return nil, err
	}

	// Step 3c: AUTHORITATIVE validate, against the tree the write actually lands
	// on (#1681 timing half).
	//
	// ⛔ RE-RESOLVING THE PATH WAS ONLY HALF OF #1673. The path now describes the
	// fresh base, but the DECISION was still Step 1's — taken against the tree as
	// it was before the checkout. `addedTenantKeys` derives its baseline from the
	// tenant file's existing `tenants:` keys, so the two trees disagreeing about
	// that file makes the gate answer a question nobody asked:
	//
	//   - stale base declares `other`, fresh base does not → BYPASS: the section
	//     is grandfathered against a tree the write does not land on, and the
	//     write ADDS it. (A long-lived pod's local base going stale is the very
	//     condition resolveFreshBaseRef exists for — TRK-318.)
	//   - stale base lacks a section the fresh base grandfathered → OVER-REJECT:
	//     a tenant legitimately sharing a flat file cannot edit its own section
	//     until the pod restarts.
	//
	// Both directions are the same defect, and both are fixed by deciding on the
	// tree that will receive the bytes. WritePRBatch already does exactly this —
	// it re-runs readMergeValidate inside its post-checkout loop — so this brings
	// the single-tenant path in line rather than inventing a second shape.
	//
	// notices come from HERE, not Step 1, for the same reason the batch path
	// discards its pre-flight's: they describe the body as merged against the
	// base it lands on, and collecting them twice would duplicate them.
	errs, notices := validate(w.configDir, tenantID, filePath, yamlContent)
	if len(errs) > 0 {
		w.abortFeatureBranch(base, branchName)
		return nil, fmt.Errorf("%w: %s", ErrValidation, strings.Join(errs, "; "))
	}

	// Step 4: write file — the tenant's existing file on THIS branch, whatever
	// its spelling, never a second file beside it.
	if err := os.WriteFile(filePath, []byte(yamlContent), 0644); err != nil {
		// Rollback: force back to a clean base (the file we just wrote is now a
		// dirty tracked change) + drop the branch.
		w.abortFeatureBranch(base, branchName)
		return nil, fmt.Errorf("write file: %w", err)
	}

	// Step 5: commit on feature branch
	committed, err := w.gitCommit(filePath, tenantID, authorEmail)
	if err != nil {
		w.abortFeatureBranch(base, branchName)
		return nil, fmt.Errorf("git commit on branch: %w", err)
	}
	// Nothing was staged — the body is byte-identical to the branch base, so
	// this branch would carry no commits. Pushing it and asking the forge to
	// open a change-free PR/MR is a 422 plus a leaked branch, so roll back and
	// signal the same clean "no changes" outcome WritePRBatch already returns
	// for an all-no-op batch (#1102). The notices ride along for the same
	// reason they do there: an idempotent retry of a body carrying a deprecated
	// spelling must keep its migration signal.
	if !committed {
		w.abortFeatureBranch(base, branchName)
		return &PRWriteResult{Notices: notices}, ErrNoChanges
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
		Notices:    notices,
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
		// Pre-flight notices are discarded — the authoritative in-lock merge
		// below re-runs validate against the fresh base and collects them
		// exactly once (no duplicates in the aggregated result).
		opPath, err := w.tenantFilePath(op.TenantID)
		if err != nil {
			return nil, err
		}
		if _, _, _, err := w.readMergeValidate(op.TenantID, opPath, op.Merge); err != nil {
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
	// notices aggregates every op's advisory deprecation channel (#1231 1b) —
	// collected BEFORE the no-op short-circuit, because a merge that changed no
	// bytes still re-committed to a body carrying a deprecated spelling.
	changed := false
	var notices []string
	for _, op := range ops {
		// #1673: one resolution per op — the file read by readMergeValidate and
		// the file written below must be the same one.
		filePath, err := w.tenantFilePath(op.TenantID)
		if err != nil {
			w.abortFeatureBranch(base, branchName)
			return nil, err
		}
		content, existing, opNotices, err := w.readMergeValidate(op.TenantID, filePath, op.Merge)
		if err != nil {
			w.abortFeatureBranch(base, branchName)
			return nil, err
		}
		notices = append(notices, opNotices...)
		if existing != nil && string(existing) == content {
			continue // no-op for this tenant — nothing to write or commit
		}
		if err := os.WriteFile(filePath, []byte(content), 0644); err != nil {
			w.abortFeatureBranch(base, branchName)
			return nil, fmt.Errorf("write file for %s: %w", op.TenantID, err)
		}
		committed, err := w.gitCommit(filePath, op.TenantID, authorEmail)
		if err != nil {
			w.abortFeatureBranch(base, branchName)
			return nil, fmt.Errorf("commit for %s: %w", op.TenantID, err)
		}
		// Track the commit that actually happened, not the attempt: content
		// can differ from the dirty working tree yet equal HEAD, in which case
		// nothing is staged and the branch gains no commit. Marking `changed`
		// there would push a commit-free branch and ask the forge to open a
		// change-free PR/MR (the 422 the block below exists to avoid).
		changed = changed || committed
	}

	// Every op was a no-op → the branch has no commits beyond base. Don't push an
	// empty branch or open a change-free PR/MR (a forge would 422). Roll the
	// branch back and signal the handler to return a clean "no changes" result —
	// the PR-mode analogue of WriteMerged's direct-path no-op success (#1102).
	// #1231 review F5: the result still carries the aggregated notices — an
	// idempotent retry of a body with a deprecated spelling must keep its
	// migration signal, exactly like WriteMerged's no-op short-circuit does
	// (the notices were deliberately collected BEFORE the per-op no-op skip
	// above; dropping them here contradicted that comment's whole point).
	if !changed {
		w.abortFeatureBranch(base, branchName)
		return &PRWriteResult{Notices: notices}, ErrNoChanges
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
		Notices:    notices,
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
