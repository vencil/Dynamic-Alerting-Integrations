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
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/platform"
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

// baseRestoreAttempts is how many times restoreBase tries checkoutBaseClean
// before giving up (#2070): the first try plus one immediate retry.
const baseRestoreAttempts = 2

// restoreBase returns the worktree to a clean base after the feature branch
// was committed (Step 7 of WritePR / WritePRBatch). A failed attempt is
// retried once, immediately and without sleeping — the caller holds the tree
// lock, and a sleep here would only hold every other write behind it. If both
// attempts fail it logs at ERROR and returns an error wrapping ErrBaseRestore:
// the tree is stranded on branch, so conf.d readers would see the un-merged
// proposal, and the write must not report success.
//
// ⛔ The git error is rendered with %v, NOT wrapped: gitErr turns index.lock
// contention into ErrWriteOverloaded, which the handler answers with a 503
// "retry" — after a successful push that retry would cut a second branch for
// the same change. See ErrBaseRestore.
//
// Callers hold the tree lock (lockTree); this never takes or releases it.
func (w *Writer) restoreBase(base, branch string, pushed bool) error {
	var err error
	for attempt := 1; attempt <= baseRestoreAttempts; attempt++ {
		if w.beforeBaseRestore != nil {
			w.beforeBaseRestore(attempt)
		}
		if err = w.checkoutBaseClean(base); err == nil {
			return nil
		}
		if attempt < baseRestoreAttempts {
			slog.Warn("gitops: failed to switch back to base branch — retrying",
				"base", base, "branch", branch, "pushed", pushed,
				"attempt", attempt, "error", err)
		}
	}
	slog.Error("gitops: failed to switch back to base branch — worktree left on feature branch (#2070)",
		"base", base, "branch", branch, "pushed", pushed,
		"attempts", baseRestoreAttempts, "error", err)
	return &BaseRestoreError{Branch: branch, Pushed: pushed, Attempts: baseRestoreAttempts, Cause: err}
}

// BaseRestoreError is the error restoreBase returns. It unwraps to
// ErrBaseRestore ONLY — the git error is kept in Cause and rendered with %v,
// never wrapped, for the reason restoreBase gives.
//
// The fields exist so a handler can tell the operator what they need — the
// branch, and whether it reached origin — without passing on the git error,
// whose text can carry server paths (an index.lock path, #1700). Error() is
// the full text, for the log.
type BaseRestoreError struct {
	Branch   string
	Pushed   bool
	Attempts int
	Cause    error
}

func (e *BaseRestoreError) Error() string {
	return fmt.Sprintf("%v (branch %q, pushed to origin: %t, attempts: %d): %v",
		ErrBaseRestore, e.Branch, e.Pushed, e.Attempts, e.Cause)
}

// Unwrap makes errors.Is(err, ErrBaseRestore) hold, and nothing else.
func (e *BaseRestoreError) Unwrap() error { return ErrBaseRestore }

// Summary is Error() without the git error: what the operator needs to find a
// branch that may be on origin with no PR/MR for it.
func (e *BaseRestoreError) Summary() string {
	return fmt.Sprintf("%v (branch %q, pushed to origin: %t)", ErrBaseRestore, e.Branch, e.Pushed)
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
	return w.WritePRChecked(ctx, tenantID, authorEmail, yamlContent, nil)
}

// PRBaseCheck judges a PR-mode write against the FRESH base the feature
// branch is cut from: configDir is the working tree, checked out at that
// branch, before anything of the write is on it. A non-nil error refuses the
// write — the branch is dropped, nothing is written, committed or pushed —
// and is returned to the caller unchanged.
type PRBaseCheck func(configDir string) error

// WritePRChecked is WritePR plus check, run under the writer lock after the
// branch is cut from the fresh origin base and the tenant's file is resolved
// there, before the body is validated and written (Step 3b'). PUT passes its
// authoritative checks here — the metadata write scope (#2857), then the
// domain policy (#2486) — because its pre-checks read the pod's local tree,
// which may lag the base. A nil check is WritePR.
func (w *Writer) WritePRChecked(ctx context.Context, tenantID, authorEmail, yamlContent string, check PRBaseCheck) (*PRWriteResult, error) {
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
	//
	// ⛔ AND IT NO LONGER RESOLVES THE TENANT'S PATH HERE. tenantFilePath walks
	// configDir (confd.ResolveTenantFile → os.ReadDir), so its errors are
	// judgments on the CURRENT tree too — ErrAmbiguousTenantFile most of all. A
	// tenant whose duplicate spelling was cleaned up remotely would be refused
	// here on a tree the write does not land on, which is the same defect this
	// change exists to remove. Step 0's guardTenantID already covers the part
	// that is NOT tree-derived (the id itself), and Step 3b resolves the path
	// against the base the write lands on. Keeping a second resolution here
	// bought nothing and re-introduced the bug one line above its own fix.
	if errs := validateBodyOnly(tenantID, yamlContent); len(errs) > 0 {
		return nil, fmt.Errorf("%w: %s", ErrValidation, strings.Join(errs, "; "))
	}

	// Step 1b: load-shedding admission (TRK-320) before w.mu.
	if err := w.acquireWrite(ctx); err != nil {
		return nil, err
	}
	defer w.releaseWrite()

	if err := w.lockTreeRecovered(); err != nil { // #2900: finish an interrupted batch publish
		return nil, err
	}
	defer w.unlockTree()

	// Step 2: generate branch name
	ts := time.Now().UTC().Format("20060102-150405")
	branchName := platform.BranchPrefix + tenantID + "/" + ts

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

	// Step 3b: resolve the tenant's file now that the feature branch is checked
	// out (#1673) — the ONLY resolution on this path since #1718.
	//
	// It must happen here and nowhere earlier: if the fresh base carries a
	// rename (`db-a.yml` → `db-a.yaml`, or the reverse), a path taken from the
	// pre-checkout tree would recreate the old spelling beside the new one — the
	// exact duplicate #1673 exists to prevent — and an ambiguity that the fresh
	// base has already cleaned up would refuse a write that is fine where it
	// lands (#1718). WritePRBatch resolves inside its post-checkout loop for the
	// same reason.
	filePath, err := w.tenantFilePath(tenantID)
	if err != nil {
		w.abortFeatureBranch(base, branchName)
		return nil, err
	}

	// Step 3b': the caller's check, on the fresh base (WritePRChecked).
	if check != nil {
		if err := check(w.configDir); err != nil {
			w.abortFeatureBranch(base, branchName)
			return nil, err
		}
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
	errs, notices := validateReplacing(w.configDir, tenantID, filePath, yamlContent, replaceUnparseableAllowed(ctx))
	if len(errs) > 0 {
		w.abortFeatureBranch(base, branchName)
		return nil, fmt.Errorf("%w: %s", ErrValidation, strings.Join(errs, "; "))
	}

	// Step 4: write file — the tenant's existing file on THIS branch, whatever
	// its spelling, never a second file beside it. Replaced by rename, as a
	// direct write (writeFileAtomic): a tenant file that is a symlink —
	// dangling, or pointing outside conf.d — becomes a regular file, and
	// nothing is written where it pointed (#2939, ADR-036 §4's repair path).
	if err := writeFileAtomic(filePath, []byte(yamlContent), 0644); err != nil {
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

	// Step 7: return to a clean base branch, retrying once (#2070). If both
	// attempts fail the tree is still on the feature branch — every conf.d
	// reader would serve the un-merged proposal — so the write FAILS (500)
	// instead of reporting success, and the error names the branch and whether
	// it reached origin. Step 8 is skipped: we are still on that branch, so it
	// cannot be deleted. The next PR-mode write's Step 3 re-anchor on the base
	// clears the stranded tree, and so does the next direct-commit write
	// (lockTreeOnBase → leavePRBranch, #1723) — which refuses that one write.
	if err := w.restoreBase(base, branchName, pushed); err != nil {
		return nil, err
	}

	// Step 8: drop the local feature branch after a CONFIRMED push (#641). The
	// commit is now safely on origin and the PR is created from origin/<branch> —
	// the local ref is no longer needed. Without this, every WritePR leaks a local
	// `tenant-api/<tenant>/<ts>` ref forever (the deployment runs one long-lived
	// replica, so this is the only thing bounding the loose-ref accumulation).
	// On push failure we KEEP the branch (the only copy of the commit) — same as
	// before. Must run AFTER step 7 (can't -D the currently-checked-out branch),
	// and only runs when step 7 succeeded — a failed step 7 returns above.
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
// This supports batch PR mode where all changes are consolidated into one PR,
// as ONE commit (#2900): every op is merged, validated and written in turn,
// and the changed files are committed together only when all of them passed.
// Every op is judged, even after one is refused: a refusal returns
// *BatchRejectedError with one outcome per op, and nothing is committed or
// pushed unless every op passes.
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
		//
		// ⛔ AMBIGUITY HERE IS NOT A VERDICT. This resolution runs before the
		// checkout, so like the validators it is reading a tree the write does
		// not land on — and WritePR stopped resolving in its pre-flight for
		// exactly that reason (#1718). Batch cannot drop the call the same way
		// (MergeFunc needs the existing body), so it drops only the refusal: a
		// tenant whose duplicate spelling was already cleaned up remotely must
		// not be turned away here while WritePR accepts it. Skipping the
		// pre-flight for that op costs nothing that matters — the post-checkout
		// loop resolves again and refuses there, with abortFeatureBranch, so no
		// dangling branch survives. ⚠️ Only THIS error is tolerated: every
		// other one is about reaching configDir at all, not about its shape.
		//
		// Placement only — confd directly, NOT tenantFilePath: the #2078
		// "declared elsewhere" walk is a verdict on the whole tree, and this is
		// not the tree the write lands on, so it is taken only after checkout.
		opPath, err := confd.TenantFilePathForWrite(w.configDir, op.TenantID)
		if err != nil {
			if errors.Is(err, confd.ErrAmbiguousTenantFile) {
				continue
			}
			return nil, err
		}
		if err := w.readMergeBodyOnly(op.TenantID, opPath, op.Merge); err != nil {
			// #2373: a MergeFunc refusing the file's CONTENT is, like the
			// ambiguity above, a verdict on a tree the write does not land
			// on — the branch is cut from the fresh origin base, where the
			// file may already be repaired. Tolerated here; the
			// post-checkout loop re-merges and refuses (abortFeatureBranch).
			if errors.Is(err, ErrMergeBaseNotLoadable) {
				continue
			}
			return nil, err
		}
	}

	if err := w.lockTreeRecovered(); err != nil { // #2900: finish an interrupted batch publish
		return nil, err
	}
	defer w.unlockTree()

	ts := time.Now().UTC().Format("20060102-150405")
	branchName := platform.BranchPrefix + "batch/" + ts

	// Anchor the serving tree on a clean base (#638 — see WritePR Step 3):
	// the PR-mode invariant between writes. The batch itself never touches
	// the serving tree (#2900).
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

	// #2900: the branch is cut in a STAGING worktree at the fresh base, and
	// every op is merged, validated and written there, in request order — a
	// second op for the same tenant merges onto the first op's result, and
	// the placement walk sees the earlier ops' files. EVERY op is judged
	// (FreshMerge gets the staging conf.d), so a refusal reports each refused
	// op (*BatchRejectedError); nothing is committed, pushed or left behind.
	// notices aggregates every op's advisory channel (#1231 1b), the no-op
	// ones' included.
	st, err := w.openStaging(branchPoint, branchName, false)
	if err != nil {
		return nil, err
	}
	pushed := false
	defer func() {
		w.closeStaging(st)
		// The local branch only outlives the batch as the one copy of a
		// commit whose push failed (#641).
		if pushed || st.branchUnneeded {
			if err := w.gitExec("branch", "-D", branchName); err != nil {
				slog.Warn("gitops: failed to delete local batch branch", "branch", branchName, "error", err)
			}
		}
	}()
	st.branchUnneeded = true

	batch := make([]BatchOp, len(ops))
	for i, op := range ops {
		batch[i] = BatchOp{TenantID: op.TenantID, Merge: op.mergeIn(st.configDir)}
	}
	outcomes, err := w.applyBatch(ctx, st, batch, w.stagingScan(st))
	if err != nil {
		return nil, err
	}
	var notices []string
	for _, o := range outcomes {
		notices = append(notices, o.Notices...)
	}

	before, err := w.gitOutput(st.root, "rev-parse", "HEAD")
	if err != nil {
		return nil, fmt.Errorf("read batch base: %w", err)
	}
	commit, err := w.commitStaging(st, strings.TrimSpace(string(before)), authorEmail)
	if err != nil {
		return nil, fmt.Errorf("commit batch: %w", err)
	}
	// Every op was a no-op → the branch has no commits beyond base. Don't push an
	// empty branch or open a change-free PR/MR (a forge would 422); signal the
	// handler to return a clean "no changes" result (#1102), with the
	// aggregated notices (#1231 F5).
	if commit == "" {
		return &PRWriteResult{Notices: notices}, ErrNoChanges
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}

	if err := w.gitIn(st.root, "push", "origin", branchName); err != nil {
		slog.Warn("gitops: push batch branch failed", "branch", branchName, "error", err)
		st.branchUnneeded = false // keep the only copy of the commit
	} else {
		pushed = true
	}

	slog.Info("gitops: PR batch branch created",
		"branch", branchName, "commit", commit, "ops", len(ops), "author", authorEmail)

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
//
// FreshMerge, when set, replaces Merge in the post-checkout pass only — the
// pass that merges onto the fresh base the branch is cut from, in the staging
// worktree. It is where
// verdicts that depend on the tree belong (the caller's metadata write scope,
// the domain policy, #2857): the pre-flight runs Merge over the pod's local
// tree, which may lag the base, so a refusal there would be a verdict on a
// tree the write does not land on, and an approval there is no approval at
// all. A FreshMerge error refuses its op, which rejects the batch
// (*BatchRejectedError, every op judged); nothing of the batch is written.
type PRBatchOp struct {
	TenantID string
	Merge    MergeFunc
	// FreshMerge gets configDir: the conf.d of the staging worktree the
	// branch is cut in (#2900), which is where its tree-dependent verdicts
	// must read.
	FreshMerge func(configDir string, existing []byte) (string, error)
}

// mergeIn is the post-checkout merge of op on the tree at configDir.
func (op PRBatchOp) mergeIn(configDir string) MergeFunc {
	if op.FreshMerge != nil {
		return func(existing []byte) (string, error) { return op.FreshMerge(configDir, existing) }
	}
	return op.Merge
}
