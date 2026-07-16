package gitops

// Rollback / failure-path coverage for WritePR + WritePRBatch (ROI refactor
// R3, E2). The invariant every test here pins: ANY mid-flight failure must
// leave the repo re-anchored on a CLEAN base with the half-built feature
// branch deleted — a failed write for tenant A must never pollute the next
// tenant's PR (dirty tree, stray branch, or half-committed file).
//
// Fault injection follows writer_hardening_test.go's fixture style: real
// local git repos (initRepoOnMain / seedTenantRepo / addBareRemote) with the
// failure induced structurally — a configDir that does not exist (os.WriteFile
// fails), an empty commit ident (the in-repo `git commit` itself fails after
// write + add succeeded), or a MergeFunc that succeeds at pre-flight and
// fails under the lock.

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"testing"
)

// assertCleanOnBase asserts the repo is back on `base` with a clean working
// tree and no leftover feature branch matching branchPrefix — the "next
// tenant is not polluted" invariant shared by every rollback test.
func assertCleanOnBase(t *testing.T, dir, base, branchPrefix string) {
	t.Helper()
	if cur := gitOut(t, dir, "rev-parse", "--abbrev-ref", "HEAD"); cur != base {
		t.Errorf("after failure, HEAD on %q, want %q (tree stranded off base)", cur, base)
	}
	if st := gitOut(t, dir, "status", "--porcelain"); st != "" {
		t.Errorf("after failure, working tree dirty (would pollute the next write):\n%s", st)
	}
	if b := gitOut(t, dir, "branch", "--format=%(refname:short)"); strings.Contains(b, branchPrefix) {
		t.Errorf("after failure, a %s* branch was left behind:\n%s", branchPrefix, b)
	}
}

// TestWritePR_WriteFileFailureRollsBack: os.WriteFile fails (configDir does
// not exist) AFTER the feature branch was created → WritePR must delete the
// branch and re-anchor on a clean base.
func TestWritePR_WriteFileFailureRollsBack(t *testing.T) {
	repo := initRepoOnMain(t)
	missingConfigDir := filepath.Join(repo, "conf.d-does-not-exist")
	w := NewWriter(missingConfigDir, repo)

	_, err := w.WritePR(context.Background(), "db-a", "alice@example.com", validTenantYAML)
	if err == nil {
		t.Fatal("expected WritePR to fail when the config dir is missing, got nil")
	}
	if !strings.Contains(err.Error(), "write file") {
		t.Errorf("error = %q, want it to carry the 'write file' failure semantics", err.Error())
	}
	assertCleanOnBase(t, repo, "main", "tenant-api/")
}

// TestWritePR_CommitFailureRollsBack: the IN-REPO commit-failure arm — the
// file write AND `git add` both succeed inside the repo, then the commit
// itself fails (empty author identity → git's "empty ident name" refusal,
// which overrides repo-local config via the inline `-c user.name=`).
//
// Measured behavior this pins: the rollback IS fully clean here. gitCommit's
// `git add` runs before the failure, so the new file is STAGED — and
// checkoutBaseClean's `reset --hard` removes staged-but-uncommitted files
// from the working tree along with the index. The residue seam therefore
// does NOT open on this in-process path.
//
// KNOWN SEAM (not exercisable in-process, documented for the record): a
// process crash BETWEEN os.WriteFile and gitCommit's `git add` leaves the
// new file UNTRACKED — checkoutBaseClean has no `git clean`, so the next
// write's re-anchor keeps it, and GET handlers (which read configDir
// directly) can serve the residue of a write that never committed. Closing
// it would need a `git clean` scoped to *.yaml in checkoutBaseClean — a
// production decision, out of scope for this test-only pass.
func TestWritePR_CommitFailureRollsBack(t *testing.T) {
	repo := initRepoOnMain(t)
	w := NewWriter(repo, repo)

	// Empty author email → gitCommit's inline `-c user.name=` / `-c
	// user.email=` resolve to empty idents and the commit fails AFTER the
	// in-repo write + add succeeded.
	_, err := w.WritePR(context.Background(), "db-a", "", validTenantYAML)
	if err == nil {
		t.Fatal("expected WritePR to fail when the commit ident is empty, got nil")
	}
	if !strings.Contains(err.Error(), "git commit on branch") {
		t.Errorf("error = %q, want the 'git commit on branch' failure semantics", err.Error())
	}
	// Full rollback contract, including a clean working tree: the staged
	// file dies with the `reset --hard`. If this ever reports a dirty tree,
	// the add-before-commit ordering (or the reset) changed — re-audit the
	// crash-window seam note above alongside it.
	assertCleanOnBase(t, repo, "main", "tenant-api/")
}

// TestWritePRBatch_EmptyOps: the guard clause — an empty batch is a hard
// error before any git activity.
func TestWritePRBatch_EmptyOps(t *testing.T) {
	dir := initRepoOnMain(t)
	w := NewWriter(dir, dir)
	if _, err := w.WritePRBatch(context.Background(), nil, "op@example.com"); err == nil {
		t.Fatal("expected an error for an empty batch, got nil")
	} else if !strings.Contains(err.Error(), "empty batch operations") {
		t.Errorf("error = %q, want 'empty batch operations'", err.Error())
	}
}

// TestWritePRBatch_SecondOpMergeFailureRollsBack: op 1 commits on the batch
// branch, then op 2's merge fails UNDER THE LOCK (it passed pre-flight) — the
// per-op rollback must drop the whole branch including op 1's commit, leave
// the base untouched, and propagate the failure.
func TestWritePRBatch_SecondOpMergeFailureRollsBack(t *testing.T) {
	dir := seedTenantRepo(t) // main: db-a.yaml + _defaults.yaml
	w := NewWriter(dir, dir)
	mainHead := gitOut(t, dir, "rev-parse", "main")

	okMerge := func(existing []byte) (string, error) {
		return string(existing) + "    _silent_mode: warning\n", nil
	}
	// Passes the pre-flight pass (call 1), fails under the lock (call 2) —
	// e.g. the on-disk state changed shape between the two reads.
	sentinel := errors.New("op2 merge blew up under the lock")
	calls := 0
	flakyMerge := func(existing []byte) (string, error) {
		calls++
		if calls >= 2 {
			return "", sentinel
		}
		return "tenants:\n  db-b:\n    _silent_mode: \"warning\"\n", nil
	}

	_, err := w.WritePRBatch(context.Background(), []PRBatchOp{
		{TenantID: "db-a", Merge: okMerge},
		{TenantID: "db-b", Merge: flakyMerge},
	}, "op@example.com")
	if !errors.Is(err, sentinel) {
		t.Fatalf("WritePRBatch error = %v, want it to propagate the op-2 merge failure", err)
	}

	assertCleanOnBase(t, dir, "main", "tenant-api/batch/")
	// Op 1's commit must have died with the branch: main is unmoved and its
	// db-a.yaml does not carry op 1's patch.
	if head := gitOut(t, dir, "rev-parse", "main"); head != mainHead {
		t.Errorf("main moved %s → %s during a failed batch", mainHead, head)
	}
	if content := gitOut(t, dir, "show", "main:db-a.yaml"); strings.Contains(content, "_silent_mode") {
		t.Errorf("op 1's patch leaked onto main despite the batch failing:\n%s", content)
	}
}

// TestWritePRBatch_WriteFileFailureRollsBack: the os.WriteFile per-op failure
// arm (configDir missing) — branch dropped, base clean, error names the tenant.
func TestWritePRBatch_WriteFileFailureRollsBack(t *testing.T) {
	repo := initRepoOnMain(t)
	missingConfigDir := filepath.Join(repo, "conf.d-does-not-exist")
	w := NewWriter(missingConfigDir, repo)

	merge := func(existing []byte) (string, error) {
		return "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n", nil
	}
	_, err := w.WritePRBatch(context.Background(),
		[]PRBatchOp{{TenantID: "db-a", Merge: merge}}, "op@example.com")
	if err == nil {
		t.Fatal("expected WritePRBatch to fail when the config dir is missing, got nil")
	}
	if !strings.Contains(err.Error(), "write file for db-a") {
		t.Errorf("error = %q, want 'write file for db-a'", err.Error())
	}
	assertCleanOnBase(t, repo, "main", "tenant-api/batch/")
}

// TestWritePRBatch_DeletesLocalBranchAfterConfirmedPush mirrors the #641
// single-write regression for the batch path: after a CONFIRMED push the
// local batch branch must be dropped (the remote copy feeds the PR), so the
// long-lived replica doesn't leak one loose ref per batch.
func TestWritePRBatch_DeletesLocalBranchAfterConfirmedPush(t *testing.T) {
	dir := seedTenantRepo(t)
	addBareRemote(t, dir)
	w := NewWriter(dir, dir)

	merge := func(existing []byte) (string, error) {
		return string(existing) + "    _silent_mode: warning\n", nil
	}
	res, err := w.WritePRBatch(context.Background(),
		[]PRBatchOp{{TenantID: "db-a", Merge: merge}}, "op@example.com")
	if err != nil {
		t.Fatalf("WritePRBatch: %v", err)
	}
	// Local branch gone…
	if b := gitOut(t, dir, "branch", "--format=%(refname:short)"); strings.Contains(b, res.BranchName) {
		t.Errorf("local batch branch %s still present after successful push (#641 leak):\n%s", res.BranchName, b)
	}
	// …remote branch present (the PR needs it) — gitOut aborts the test on miss.
	gitOut(t, dir, "rev-parse", "--verify", fmt.Sprintf("refs/remotes/origin/%s", res.BranchName))
}
