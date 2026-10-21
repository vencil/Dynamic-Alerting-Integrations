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
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/platform"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
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
//
// #3175: if base's git config names a driver, checkoutBaseClean refuses
// after its move (HEAD is on base); the branch -D that follows only
// deletes a ref and runs no driver.
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
		// #3175: a refusal of checkoutBaseClean's LAST step, the driver
		// check after the move — the reset and checkout failures are never
		// either sentinel — so the tree IS on base, with base's files.
		// Base was checked when this write anchored on it (Step 3), so what
		// the check meets appeared mid-write. No retry — its reset would
		// run under base's config, which is not shown clean — and no
		// failure: the branch is committed (and pushed, if pushed), the
		// tree is on base, and what is left (Step 8's branch -D) touches no
		// file. Failing here would make a client retry a pushed write (see
		// ErrBaseRestore). Every later git command on files is checked
		// again first: the next write's check under the lock
		// (lockTreeRecovered), a preview's (PreviewPut), a reader's
		// (TreeOnBase).
		if errors.Is(err, ErrGitDriverConfigured) {
			slog.Error("gitops: back on base, whose git config now names a driver; the next write will be refused (#3175)",
				"base", base, "branch", branch, "pushed", pushed, "error", err)
			return nil
		}
		if errors.Is(err, ErrGitConfigUnreadable) {
			// #3226: the same place, but the config could not be read —
			// nothing says a driver is configured. The next write's check
			// reads it again and refuses only if it still cannot.
			slog.Error("gitops: back on base, but its git config could not be read to check it for drivers; the next write checks again (#3175)",
				"base", base, "branch", branch, "pushed", pushed, "error", err)
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

// ErrPushFailed is what a PR-mode write returns (as *PushError) when pushing
// its feature branch to origin failed — for any reason, a timeout included
// (#1724). No PR/MR may be opened: the forge would open it on whatever
// origin holds under that name, which is not this write's commit. The local
// branch is deleted and the worktree returned to base, so a retry starts
// clean and cuts a new branch (prBranchName); the commit is rebuilt from the
// request, so nothing is lost by dropping it.
var ErrPushFailed = errors.New("push of the PR-mode feature branch to origin failed; no PR/MR opened")

// PushError is the error a PR-mode write returns when its push failed. It
// unwraps to ErrPushFailed ONLY — like BaseRestoreError, the git error is
// kept in Cause and rendered with %v, never wrapped: a push that timed out
// wraps ErrGitTimeout (#3173) and one that met index.lock contention wraps
// ErrWriteOverloaded, and either would make a handler answer the generic
// cancelled / overloaded 503 instead of the push-failed one. Its text carries
// the remote's output, so it goes to the log, not the client.
type PushError struct {
	Branch string
	Cause  error
}

func (e *PushError) Error() string {
	return fmt.Sprintf("%v (branch %q): %v", ErrPushFailed, e.Branch, e.Cause)
}

// Unwrap makes errors.Is(err, ErrPushFailed) hold, and nothing else.
func (e *PushError) Unwrap() error { return ErrPushFailed }

// prBranchSuffixBytes is the random part of a PR-mode branch name, in bytes
// (hex-encoded: twice as many characters). The timestamp alone resolved to
// the second, so two writes in one second — a batch of other tenants, a
// retry right after a refusal — cut the same branch, and the second push was
// rejected or reused the first one's branch (#1724).
const prBranchSuffixBytes = 6

// prBranchName names a PR-mode feature branch: prefix, the UTC time to the
// second, and a random suffix ("<prefix><yyyymmdd-hhmmss>-<12 hex>").
// platform.TenantIDFromBranch reads only the segment after the branch
// prefix, so the suffix does not change which tenant a branch names.
func (w *Writer) prBranchName(prefix string) (string, error) {
	now := time.Now
	if w.prBranchClock != nil {
		now = w.prBranchClock
	}
	var suffix string
	if w.prBranchSuffix != nil {
		suffix = w.prBranchSuffix()
	} else {
		b := make([]byte, prBranchSuffixBytes)
		if _, err := rand.Read(b); err != nil {
			return "", fmt.Errorf("generate branch suffix: %w", err)
		}
		suffix = hex.EncodeToString(b)
	}
	return prefix + now().UTC().Format("20060102-150405") + "-" + suffix, nil
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
// branch is cut from: configDir is that base's conf.d as git holds it
// (openFreshBase, #2972 — not the serving tree, whose uncommitted entries
// the PR does not carry), before anything of the write is on it. A non-nil
// error refuses the write — the branch is dropped, nothing is written,
// committed or pushed — and is returned to the caller unchanged.
type PRBaseCheck func(configDir string) error

// WritePRChecked is WritePR plus check, run under the writer lock after the
// branch is cut from the fresh origin base and the tenant's file is resolved
// on it, before the body is validated and written (Step 3b'). PUT passes its
// authoritative check here — the metadata write scope (#2857) — because its
// pre-check reads the pod's local tree, which may lag the base. A nil check is WritePR.
func (w *Writer) WritePRChecked(ctx context.Context, tenantID, authorEmail, yamlContent string, check PRBaseCheck) (*PRWriteResult, error) {
	// Step 0: reserved-id backstop (defense-in-depth; see guardTenantID).
	if err := guardTenantID(tenantID); err != nil {
		return nil, err
	}
	// Step 1: PRE-FLIGHT validate, before anything touches git.
	//
	// ⛔ THIS IS NOT THE AUTHORITATIVE RUN — Step 3c is. Everything stateful in
	// validate (the eol-expansion guard, the merged key validation) reads the
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

	if err := w.lockTreeRecovered(); err != nil { // #2900/#3173: settle an interrupted publish
		return nil, err
	}
	defer w.unlockTree()

	// Step 2: generate branch name (unique per write, #1724)
	branchName, err := w.prBranchName(platform.BranchPrefix + tenantID + "/")
	if err != nil {
		return nil, err
	}

	// Step 3: anchor on a clean base, THEN branch from it. Always checking out the
	// base first (rather than branching from "current HEAD" and returning via the
	// relative `checkout -`) makes cross-tenant branch pollution impossible: even if
	// a prior write left the tree on some feature branch, this re-establishes the
	// base every time (#638). Abort if the base is unreachable — branching from an
	// unknown ref is exactly the bug we're preventing.
	// checkoutBaseClean re-checks the git config on base (#3175): the check
	// under the lock ran on whatever branch HEAD was on then, and the
	// `checkout -b` below runs under base's config.
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
	// #3175: on the feature branch an `includeIf "onbranch:…"` the check on
	// base (checkoutBaseClean, just above) could not see may apply. Checked
	// before anything is written, staged or committed on it. The way back
	// must not run git on files under the branch's config: HEAD is pointed
	// at base first (no file touched), and only then is the tree checked out
	// — under base's config, which checkoutBaseClean checked after its move.
	if err := w.checkGitDriversIn(w.gitDir); err != nil {
		// If HEAD cannot be pointed back at base, the abort's reset and
		// checkout would run under the branch's config — skip it. HEAD stays
		// on the feature branch, so the next write's check under the lock
		// sees the same driver and refuses before any git runs on files.
		if serr := w.gitExec("symbolic-ref", "HEAD", "refs/heads/"+base); serr != nil {
			slog.Error("PR-mode write: driver found on the feature branch and HEAD could not be pointed back at base; leaving the branch for the operator",
				"branch", branchName, "base", base, "error", serr)
			return nil, err
		}
		w.abortFeatureBranch(base, branchName)
		return nil, err
	}

	// Step 3b–3c: resolve the tenant's file, run the caller's check and the
	// authoritative validate — all on the fresh base as git holds it
	// (prResolveOnBase, #2972). filePath is the file in the serving tree,
	// which is where Step 4 writes it.
	filePath, content, notices, err := w.prResolveOnBase(ctx, branchPoint, tenantID, yamlContent, check)
	if err != nil {
		w.abortFeatureBranch(base, branchName)
		return nil, err
	}
	// Step 3b'': the serving tree's own entry at filePath, which Step 4
	// replaces there — the one thing of the serving tree this write touches.
	if err := w.refuseUntrackedNonRegular(filePath, tenantID); err != nil {
		w.abortFeatureBranch(base, branchName)
		return nil, err
	}

	// Step 3d (#2903, ADR-036 §4 "PR 模式驗的是分支起點"): the generator
	// judges the branch point — the feature branch's HEAD, nothing committed
	// on it yet — with the body written over the file.
	judged, jerr := w.sampleJudgedBase()
	if jerr == nil {
		jerr = w.validateTenantFile(ctx, judged, filePath, tenantID, []byte(content))
	}
	if err := jerr; err != nil {
		w.abortFeatureBranch(base, branchName)
		return nil, err
	}

	// Step 4: write file — the tenant's existing file on THIS branch, whatever
	// its spelling, never a second file beside it. Replaced by rename, as a
	// direct write (writeFileAtomic): a tenant file that is a symlink —
	// dangling, or pointing outside conf.d — becomes a regular file, and
	// nothing is written where it pointed (#2939, ADR-036 §4's repair path).
	if err := w.writeTenant(filePath, []byte(content), 0644); err != nil {
		// Rollback: force back to a clean base (the file we just wrote is now a
		// dirty tracked change) + drop the branch.
		w.abortFeatureBranch(base, branchName)
		return nil, fmt.Errorf("write file: %w", err)
	}

	// Step 5: commit on feature branch. Its index is the clean base's
	// (checkoutBaseClean: `reset --hard`), so the commit holds this path
	// only — no out-of-band staged file rides along (#1535).
	committed, err := w.gitCommitPaths(w.gitDir, []string{filePath}, tenantCommitMessage(tenantID),
		authorEmail, map[string][]byte{filePath: []byte(content)})
	if err != nil {
		w.abortFeatureBranch(base, branchName)
		if w.stagedRefusal(err) {
			return nil, err
		}
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

	// Step 6: push branch to origin. A failed push — rejected, unreachable,
	// timed out — FAILS the write (*PushError, #1724): the handler must not
	// open a PR/MR on a head origin does not hold as this commit.
	var pushErr error
	if err := w.gitExec("push", "origin", branchName); err != nil {
		slog.Warn("gitops: push branch failed — no PR/MR will be opened",
			"branch", branchName, "error", err)
		pushErr = &PushError{Branch: branchName, Cause: err}
	}
	pushed := pushErr == nil

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

	// Step 8: drop the local feature branch, pushed or not. After a push
	// (#641) the commit is on origin and the PR is created from
	// origin/<branch>; without this every WritePR leaks a local ref forever
	// (one long-lived replica). After a FAILED push it goes too (#1724):
	// #641 kept it as "the only copy of the commit", but a retry rebuilds the
	// commit from the request, and the kept branch was what a later write of
	// the same name collided with (`checkout -b` → 500). Must run AFTER step 7
	// (can't -D the checked-out branch), and only when step 7 succeeded — a
	// failed step 7 returns above.
	if err := w.gitExec("branch", "-D", branchName); err != nil {
		slog.Warn("gitops: failed to delete local feature branch",
			"branch", branchName, "pushed", pushed, "error", err)
	}
	if pushErr != nil {
		return nil, pushErr
	}

	slog.Info("gitops: PR branch created",
		"branch", branchName, "tenant", tenantID, "author", authorEmail)

	return &PRWriteResult{
		BranchName: branchName,
		FilePath:   filePath,
		Notices:    notices,
	}, nil
}

// openFreshBase opens the tree a PR-mode write is judged on: a staging
// worktree at rev, the commit the feature branch is cut from, with conf.d
// checked out exactly as git holds it (openStaging without the mirror) —
// the tree the service serves once the PR merges. branch, when not "", is
// created there (WritePRBatch commits on it); WritePRChecked passes "" and
// only judges there. The caller MUST closeStaging it.
//
// ⛔ ONE TREE FOR BOTH PR PATHS (#2972). The serving working tree, even
// checked out at the feature branch, also holds what git does not: an
// uncommitted FIFO, a .gitignore'd entry. Judging there refused a single
// PR write that WritePRBatch accepted on the same tree, for an entry the
// merged PR never carries. Both PR paths take every tree-derived verdict —
// placement, the #2078 walk, #2939's judgeTree, the caller's fresh-base
// check, the authoritative validate — on this tree.
func (w *Writer) openFreshBase(rev, branch string) (*stagingTree, error) {
	return w.openStaging(rev, branch, false)
}

// prResolveOnBase is WritePRChecked's Steps 3b–3c on the fresh base at
// branchPoint (openFreshBase), run after the feature branch is checked out
// in the serving tree and before anything is written there. It returns the
// tenant's file in the serving tree — the same conf.d-relative name as on
// the base, which the feature branch carries — and the authoritative
// validate's notices. Caller holds w.mu and drops the branch on an error.
//
// content is what Step 4 writes: yamlContent. A tenant git-managed on the
// fresh base is refused there (#3064, resolvePut), whatever the serving tree
// holds.
func (w *Writer) prResolveOnBase(ctx context.Context, branchPoint, tenantID, yamlContent string, check PRBaseCheck) (filePath, content string, notices []string, err error) {
	st, err := w.openFreshBase(branchPoint, "")
	if err != nil {
		// A conf.d that cannot be reached is the walk failing to run, which
		// the #2078 guard fails closed on (ErrTenantTreeScan), as the walk
		// itself would report it. ⛔ Only then: openFreshBase also fails on
		// git (index.lock contention is ErrWriteOverloaded, a retryable 503),
		// and the handlers map ErrTenantTreeScan to a 500 before they look
		// at ErrWriteOverloaded (the handler's kindsWriteFlow).
		if _, serr := os.Stat(w.configDir); serr != nil {
			return "", "", nil, fmt.Errorf("%w: tenant %s: %w", ErrTenantTreeScan, tenantID, err)
		}
		return "", "", nil, err
	}
	defer w.closeStaging(st)

	// Step 3b: resolve the tenant's file on the base the write lands on
	// (#1673) — the ONLY resolution on this path since #1718.
	//
	// It must happen here and nowhere earlier: if the fresh base carries a
	// rename (`db-a.yml` → `db-a.yaml`, or the reverse), a path taken from the
	// pre-checkout tree would recreate the old spelling beside the new one — the
	// exact duplicate #1673 exists to prevent — and an ambiguity that the fresh
	// base has already cleaned up would refuse a write that is fine where it
	// lands (#1718). WritePRBatch resolves on the same tree, per op.
	//
	// Step 3b': the caller's check, on the fresh base (WritePRChecked).
	//
	// Step 3c: AUTHORITATIVE validate, against the tree the write actually lands
	// on (#1681 timing half).
	//
	// ⛔ RE-RESOLVING THE PATH WAS ONLY HALF OF #1673. The path now describes the
	// fresh base, but the DECISION was still Step 1's — taken against the tree as
	// it was before the checkout. The #1681 added-section gate derived its
	// baseline from the tenant file's existing `tenants:` keys (since #3064 the
	// question is whether that file is API-managed, judged on the fresh base),
	// so the two trees disagreeing about that file makes the gate answer a
	// question nobody asked:
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
	//
	// #3062: resolvePut is the one copy of steps 3b–3c, shared with the
	// direct write and the previews; #3064: it refuses a tenant git-managed
	// on the base before the caller's check.
	var afterResolve func(string) error
	if check != nil {
		afterResolve = func(string) error { return check(st.configDir) }
	}
	res, errs, err := w.resolvePut(ctx, st.configDir, tenantID, yamlContent, w.stagingScan(st), nil, afterResolve)
	if err != nil {
		return "", "", nil, err
	}
	if len(errs) > 0 {
		return "", "", nil, fmt.Errorf("%w: %s", ErrValidation, strings.Join(errs, "; "))
	}
	basePath, content, notices := res.FilePath, res.Content, res.Notices
	return filepath.Join(w.configDir, filepath.FromSlash(confRelKey(st.configDir, basePath))), content, notices, nil
}

// refuseUntrackedNonRegular refuses a PR-mode write whose tenant file, in
// the SERVING tree, is an entry git does not track and that is not a regular
// file: a FIFO, a socket, a device, a directory, or a symlink to one. Caller
// holds w.mu, with the feature branch checked out.
//
// ⚠️ THE ONE PLACE PR MODE LOOKS AT THE SERVING TREE (#2972). Every verdict is
// taken on the fresh base (prResolveOnBase), where such an entry does not
// exist, so the write would go through: Step 4 renames the body over it and
// the return to base removes the file the base does not have — the entry, an
// operator's trace of a fault on tenant-api's copy, silently gone. So it is
// refused instead, with the TargetUnreadableError direct mode refuses such a
// tenant file with (TENANT_CONFIG_NOT_LOADABLE), and left where it is.
//
// Allowed, as before: nothing there; a tracked path (the checkout put git's
// content there); an untracked regular file; an unresolvable symlink (no
// content — ADR-036 §4's repair path). Any other failed stat is refused
// too: what is there cannot be told apart from content.
func (w *Writer) refuseUntrackedNonRegular(filePath, tenantID string) error {
	li, err := os.Lstat(filePath)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	reason := cfg.UnreadableSpecialFile
	if err == nil {
		if li.Mode().IsRegular() {
			return nil
		}
		if li.Mode()&fs.ModeSymlink != 0 {
			fi, serr := os.Stat(filePath)
			switch {
			case serr == nil && fi.Mode().IsRegular():
				return nil
			case serr != nil && isUnresolvableLink(filePath):
				return nil
			case serr != nil:
				reason = cfg.UnreadableStatError
			}
		}
	} else {
		reason = cfg.UnreadableStatError
	}
	top, rel, err := w.confTopRel()
	if err != nil {
		return err
	}
	// Literal (ls-files gets GIT_LITERAL_PATHSPECS, literalPathspecs; #3114): conf.d's path and the file's name are names,
	// not patterns — a `[`, `*` or `?` in either would glob onto another
	// tracked file and pass this entry as tracked. rel is relative to the top
	// level, so the command runs there (pathspec magic such as `:(top)` is off
	// under GIT_LITERAL_PATHSPECS).
	tracked, err := w.gitOutput(top, "ls-files", "-z", "--",
		filepath.ToSlash(filepath.Join(rel, filepath.Base(filePath))))
	if err != nil {
		return fmt.Errorf("check whether the tenant file is tracked: %w", err)
	}
	if len(tracked) > 0 {
		return nil
	}
	return &TargetUnreadableError{TenantID: tenantID, RelKey: confRelKey(w.configDir, filePath), Reason: reason}
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
			// #2939: so is a tenant file the local tree cannot read; on the
			// base it lands on, judgeTree refuses it (TargetUnreadableError)
			// or the read succeeds.
			// #3064: so is a tenant git-managed on the local tree.
			if errors.Is(err, ErrMergeBaseNotLoadable) || errors.Is(err, errReadTenantFile) || errors.Is(err, ErrTenantGitManaged) {
				continue
			}
			return nil, err
		}
	}

	if err := w.lockTreeRecovered(); err != nil { // #2900/#3173: settle an interrupted publish
		return nil, err
	}
	defer w.unlockTree()

	branchName, err := w.prBranchName(platform.BatchBranchPrefix) // unique per batch, #1724
	if err != nil {
		return nil, err
	}

	// Anchor the serving tree on a clean base (#638 — see WritePR Step 3):
	// the PR-mode invariant between writes. The batch itself never touches
	// the serving tree (#2900). checkoutBaseClean re-checks the git config
	// on base (#3175); a driver there refuses the batch, as it refuses every
	// write once the tree is on base.
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
	st, err := w.openFreshBase(branchPoint, branchName)
	if err != nil {
		return nil, err
	}
	defer func() {
		w.closeStaging(st)
		// The local branch never outlives the batch: pushed, it is on origin
		// (#641); not pushed, a retry rebuilds the commit (#1724).
		if st.branchUnneeded {
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
	// #2903 (ADR-036 §4): the generator judges the branch point with every
	// file the batch changed written over it, once for the whole batch.
	if err := w.validateStaging(ctx, st, strings.TrimSpace(string(before))); err != nil {
		return nil, err
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

	// A failed push fails the batch (#1724, as WritePRChecked's Step 6): no
	// PR/MR, and the deferred cleanup drops the local branch.
	if err := w.gitIn(st.root, "push", "origin", branchName); err != nil {
		slog.Warn("gitops: push batch branch failed — no PR/MR will be opened", "branch", branchName, "error", err)
		return nil, &PushError{Branch: branchName, Cause: err}
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
// #2857): the pre-flight runs Merge over the pod's local
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
