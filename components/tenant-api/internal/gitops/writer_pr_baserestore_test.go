package gitops

// #2070: WritePR / WritePRBatch Step 7 (return to base after the push) used to
// only warn on failure and report success, leaving conf.d on the feature
// branch so every reader served the un-merged proposal. These tests pin the
// new contract: one immediate retry, then a failed write carrying
// ErrBaseRestore — and never ErrWriteOverloaded, which would tell the client
// to retry a write whose branch is already on origin.
//
// Fault injection is structural: the per-Writer beforeBaseRestore seam
// creates <repo>/.git/index.lock right before an attempt, so checkoutBaseClean's
// `reset --hard` really fails the way a competing git process would make it.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/confd"
)

// lockIndexOnAttempts returns a beforeBaseRestore hook that holds
// <repo>/.git/index.lock during the listed attempts and removes it before any
// other attempt, plus a func the test calls to drop the lock afterwards.
func lockIndexOnAttempts(t *testing.T, repo string, lockedAttempts ...int) (hook func(int), attemptsSeen *int, unlock func()) {
	t.Helper()
	lockPath := filepath.Join(repo, ".git", "index.lock")
	seen := 0
	unlock = func() {
		if err := os.Remove(lockPath); err != nil && !os.IsNotExist(err) {
			t.Fatalf("remove index.lock: %v", err)
		}
	}
	hook = func(attempt int) {
		seen = attempt
		for _, a := range lockedAttempts {
			if a == attempt {
				if err := os.WriteFile(lockPath, nil, 0o644); err != nil {
					t.Fatalf("create index.lock: %v", err)
				}
				return
			}
		}
		unlock()
	}
	t.Cleanup(unlock)
	return hook, &seen, unlock
}

// assertBaseRestoreError pins the error contract every both-attempts-failed
// test shares: ErrBaseRestore, none of the sentinels the handler maps to a
// non-500 answer, and the branch name in the message.
func assertBaseRestoreError(t *testing.T, err error, branchPrefix string) {
	t.Helper()
	if err == nil {
		t.Fatal("write reported success although the tree could not return to base (#2070)")
	}
	if !errors.Is(err, ErrBaseRestore) {
		t.Errorf("errors.Is(err, ErrBaseRestore) = false; err = %v", err)
	}
	// ⛔ The inner failure is index.lock contention, which gitErr maps to
	// ErrWriteOverloaded. If it leaked through the wrap, the handler would
	// answer 503 "retry" for a write whose branch is already pushed.
	for _, s := range []error{ErrWriteOverloaded, ErrForgeDegraded, ErrValidation, ErrNoChanges, confd.ErrAmbiguousTenantFile} {
		if errors.Is(err, s) {
			t.Errorf("errors.Is(err, %v) = true — the handler would not answer 500; err = %v", s, err)
		}
	}
	if !strings.Contains(err.Error(), branchPrefix) {
		t.Errorf("error %q does not name the %s* branch — an operator cannot find the pushed branch", err, branchPrefix)
	}
	if !strings.Contains(err.Error(), "pushed to origin: true") {
		t.Errorf("error %q does not say the branch was pushed", err)
	}
}

// (a) First attempt fails, second succeeds → the write succeeds exactly as
// before: HEAD on base, clean tree, local branch dropped, remote branch kept.
func TestWritePR_BaseRestoreRetriesOnce(t *testing.T) {
	dir := initRepoOnMain(t)
	addBareRemote(t, dir)
	w := NewWriter(dir, dir)
	hook, seen, _ := lockIndexOnAttempts(t, dir, 1)
	w.beforeBaseRestore = hook

	res, err := w.WritePR(context.Background(), "db-a", "alice@example.com", validTenantYAML)
	if err != nil {
		t.Fatalf("WritePR with one transient restore failure: %v", err)
	}
	if *seen != 2 {
		t.Errorf("restore attempts = %d, want 2 (one failure + one retry)", *seen)
	}
	assertCleanOnBase(t, dir, "main", "tenant-api/")
	gitOut(t, dir, "rev-parse", "--verify", "refs/remotes/origin/"+res.BranchName)
}

// (b) Both attempts fail → WritePR fails with ErrBaseRestore (not a retryable
// 503 sentinel) naming the pushed branch. Once the lock is gone the next
// write re-anchors on base and succeeds, so the tree does get back to base.
func TestWritePR_BaseRestoreFailsTwiceReturnsError(t *testing.T) {
	dir := initRepoOnMain(t)
	addBareRemote(t, dir)
	w := NewWriter(dir, dir)
	hook, seen, unlock := lockIndexOnAttempts(t, dir, 1, 2)
	w.beforeBaseRestore = hook

	res, err := w.WritePR(context.Background(), "db-a", "alice@example.com", validTenantYAML)
	if res != nil {
		t.Errorf("result = %+v, want nil on a failed restore", res)
	}
	assertBaseRestoreError(t, err, "tenant-api/db-a/")
	if *seen != 2 {
		t.Errorf("restore attempts = %d, want 2", *seen)
	}
	// The failure is real: the tree is still on the feature branch.
	if cur := gitOut(t, dir, "rev-parse", "--abbrev-ref", "HEAD"); !strings.HasPrefix(cur, "tenant-api/db-a/") {
		t.Errorf("HEAD = %q, want the stranded feature branch", cur)
	}
	// And the branch did reach origin — which is why a retry must not be invited.
	if b := gitOut(t, dir, "branch", "-r", "--format=%(refname:short)"); !strings.Contains(b, "origin/tenant-api/db-a/") {
		t.Errorf("pushed branch not on origin:\n%s", b)
	}

	// Recovery: lock gone, next write (another tenant so the second-precision
	// branch name cannot collide with the stranded one) succeeds and ends on base.
	unlock()
	w.beforeBaseRestore = nil
	if _, err := w.WritePR(context.Background(), "db-b", "alice@example.com",
		"tenants:\n  db-b:\n    _silent_mode: \"warning\"\n"); err != nil {
		t.Fatalf("follow-up WritePR after the lock cleared: %v", err)
	}
	if cur := gitOut(t, dir, "rev-parse", "--abbrev-ref", "HEAD"); cur != "main" {
		t.Errorf("after recovery HEAD = %q, want main", cur)
	}
	if st := gitOut(t, dir, "status", "--porcelain"); st != "" {
		t.Errorf("after recovery working tree dirty:\n%s", st)
	}
}

// (c) The batch path has the same Step 7 and the same contract.
func TestWritePRBatch_BaseRestoreFailsTwiceReturnsError(t *testing.T) {
	dir := seedTenantRepo(t)
	addBareRemote(t, dir)
	w := NewWriter(dir, dir)
	hook, seen, unlock := lockIndexOnAttempts(t, dir, 1, 2)
	w.beforeBaseRestore = hook

	appendSilent := func(existing []byte) (string, error) {
		return string(existing) + "    _silent_mode: warning\n", nil
	}
	res, err := w.WritePRBatch(context.Background(), []PRBatchOp{
		{TenantID: "db-a", Merge: appendSilent},
	}, "op@example.com")
	if res != nil {
		t.Errorf("result = %+v, want nil on a failed restore", res)
	}
	assertBaseRestoreError(t, err, "tenant-api/batch/")
	if *seen != 2 {
		t.Errorf("restore attempts = %d, want 2", *seen)
	}

	unlock()
	w.beforeBaseRestore = nil
	if _, err := w.WritePR(context.Background(), "db-b", "op@example.com",
		"tenants:\n  db-b:\n    _silent_mode: \"warning\"\n"); err != nil {
		t.Fatalf("follow-up WritePR after the lock cleared: %v", err)
	}
	if cur := gitOut(t, dir, "rev-parse", "--abbrev-ref", "HEAD"); cur != "main" {
		t.Errorf("after recovery HEAD = %q, want main", cur)
	}
	if st := gitOut(t, dir, "status", "--porcelain"); st != "" {
		t.Errorf("after recovery working tree dirty:\n%s", st)
	}
}

// The batch retry arm: one transient failure is absorbed and the write succeeds.
func TestWritePRBatch_BaseRestoreRetriesOnce(t *testing.T) {
	dir := seedTenantRepo(t)
	addBareRemote(t, dir)
	w := NewWriter(dir, dir)
	hook, seen, _ := lockIndexOnAttempts(t, dir, 1)
	w.beforeBaseRestore = hook

	appendSilent := func(existing []byte) (string, error) {
		return string(existing) + "    _silent_mode: warning\n", nil
	}
	if _, err := w.WritePRBatch(context.Background(), []PRBatchOp{
		{TenantID: "db-a", Merge: appendSilent},
	}, "op@example.com"); err != nil {
		t.Fatalf("WritePRBatch with one transient restore failure: %v", err)
	}
	if *seen != 2 {
		t.Errorf("restore attempts = %d, want 2", *seen)
	}
	assertCleanOnBase(t, dir, "main", "tenant-api/batch/")
}
