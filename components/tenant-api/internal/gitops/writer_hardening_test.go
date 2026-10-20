package gitops

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// validTenantYAML is a minimal config that passes validate() for tenant "db-a".
const validTenantYAML = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"

func gitRun(t *testing.T, dir string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
}

func gitOut(t *testing.T, dir string, args ...string) string {
	t.Helper()
	out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).Output()
	if err != nil {
		t.Fatalf("git %v: %v", args, err)
	}
	return strings.TrimSpace(string(out))
}

// initRepoOnMain creates a temp git repo whose single commit sits on "main"
// (matching the Writer's default base branch), regardless of git's init default.
func initRepoOnMain(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	cmds := [][]string{
		{"init"},
		{"config", "user.email", "t@t.com"},
		{"config", "user.name", "T"},
		{"commit", "--allow-empty", "-m", "initial"},
		{"branch", "-M", "main"},
	}
	for _, args := range cmds {
		cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Skipf("git %v unavailable: %v\n%s", args, err, out)
		}
	}
	return dir
}

// TestWritePR_BranchesFromBaseNotStuckBranch is the #638 item-2 regression: even
// when a prior write left the working tree stranded on another tenant's feature
// branch (with un-pushed commits), WritePR must branch the new tenant's PR from the
// BASE — never from the stuck branch — so PRs can't cross-contaminate.
func TestWritePR_BranchesFromBaseNotStuckBranch(t *testing.T) {
	dir := initRepoOnMain(t)
	w := newTestWriter(dir, dir) // baseBranch defaults to "main"
	addBareRemote(t, dir)        // the push must succeed (#1724)

	mainHead := gitOut(t, dir, "rev-parse", "main")

	// Strand the tree on another tenant's feature branch with an extra commit.
	gitRun(t, dir, "checkout", "-b", "tenant-api/other/stuck")
	gitRun(t, dir, "commit", "--allow-empty", "-m", "other tenant's unpushed work")

	res, err := w.WritePR(context.Background(), "db-a", "alice@example.com", validTenantYAML)
	if err != nil {
		t.Fatalf("WritePR: %v", err)
	}

	// The feature branch's parent MUST be main's tip, NOT the stuck branch's.
	if parent := gitOut(t, dir, "rev-parse", originRef(res.BranchName)+"~1"); parent != mainHead {
		t.Errorf("feature branch parent = %s, want main %s — branched from the stuck feature branch (cross-tenant leak)", parent, mainHead)
	}
	// And the tree must be returned to main (explicit anchor), not left on the feature branch.
	if cur := gitOut(t, dir, "rev-parse", "--abbrev-ref", "HEAD"); cur != "main" {
		t.Errorf("after WritePR, HEAD on %q, want main", cur)
	}
}

// TestWritePR_AbortsWhenBaseMissing: if the configured base branch doesn't exist,
// WritePR aborts (rather than branching from an unknown ref) — the safe failure.
func TestWritePR_AbortsWhenBaseMissing(t *testing.T) {
	dir := initRepoOnMain(t)
	w := newTestWriter(dir, dir)
	w.SetBaseBranch("nonexistent-base")

	if _, err := w.WritePR(context.Background(), "db-a", "alice@example.com", validTenantYAML); err == nil {
		t.Fatal("expected WritePR to abort when the base branch is missing, got nil error")
	} else if !strings.Contains(err.Error(), "checkout base") {
		t.Errorf("error = %q, want it to mention 'checkout base'", err.Error())
	}
}

// TestWritePR_RecoversFromDirtyTree is the dirty-tree wedge (#638, caught in
// review): a write killed AFTER os.WriteFile but BEFORE its commit leaves a
// modified-uncommitted tracked file, on which a PLAIN `git checkout <base>`
// REFUSES ("local changes would be overwritten") — wedging every subsequent PR
// write. The ironclad checkoutBaseClean (reset --hard + checkout -f) must recover.
func TestWritePR_RecoversFromDirtyTree(t *testing.T) {
	dir := initRepoOnMain(t)
	addBareRemote(t, dir) // the push must succeed (#1724)
	w := newTestWriter(dir, dir)
	cfgPath := filepath.Join(dir, "db-a.yaml")
	write := func(mode string) {
		if err := os.WriteFile(cfgPath, []byte("tenants:\n  db-a:\n    _silent_mode: \""+mode+"\"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	// main: db-a.yaml = v1 (committed).
	write("warning")
	gitRun(t, dir, "add", "db-a.yaml")
	gitRun(t, dir, "commit", "-m", "v1")
	mainHead := gitOut(t, dir, "rev-parse", "main")

	// Strand on a feature branch with db-a.yaml = v2 (committed), then leave v3
	// UNCOMMITTED — now `git checkout main` (main has v1) would refuse.
	gitRun(t, dir, "checkout", "-b", "tenant-api/other/stuck")
	write("critical")
	gitRun(t, dir, "commit", "-am", "v2")
	write("info") // v3, uncommitted → dirty tree that blocks a plain checkout

	// WritePR must recover (reset --hard + checkout -f) and branch db-b from main.
	res, err := w.WritePR(context.Background(), "db-b", "alice@example.com", "tenants:\n  db-b:\n    _silent_mode: \"warning\"\n")
	if err != nil {
		t.Fatalf("WritePR did not recover from a dirty tree: %v", err)
	}
	if parent := gitOut(t, dir, "rev-parse", originRef(res.BranchName)+"~1"); parent != mainHead {
		t.Errorf("feature branch parent = %s, want main %s", parent, mainHead)
	}
	if cur := gitOut(t, dir, "rev-parse", "--abbrev-ref", "HEAD"); cur != "main" {
		t.Errorf("after WritePR, HEAD on %q, want main", cur)
	}
}

// TestClearStaleGitLocks: a SIGKILL'd git leaves index.lock / ref locks /
// packed-refs.lock; clearStaleGitLocks (#638) must remove all of them so the next
// write isn't bricked. Proven by a real commit succeeding afterward.
func TestClearStaleGitLocks(t *testing.T) {
	dir := initRepoOnMain(t)
	w := newTestWriter(dir, dir)

	gitMeta := filepath.Join(dir, ".git")
	nestedRefLock := filepath.Join(gitMeta, "refs", "heads", "tenant-api", "db-a", "20260101.lock")
	if err := os.MkdirAll(filepath.Dir(nestedRefLock), 0o755); err != nil {
		t.Fatalf("mkdir ref dir: %v", err)
	}
	locks := []string{
		filepath.Join(gitMeta, "index.lock"),
		filepath.Join(gitMeta, "HEAD.lock"),
		filepath.Join(gitMeta, "packed-refs.lock"),
		filepath.Join(gitMeta, "config.lock"),
		nestedRefLock,
	}
	for _, p := range locks {
		if err := os.WriteFile(p, []byte("stale"), 0o644); err != nil {
			t.Fatalf("create stale lock %s: %v", p, err)
		}
	}

	w.clearStaleGitLocks()

	for _, p := range locks {
		if _, err := os.Stat(p); !os.IsNotExist(err) {
			t.Errorf("stale lock not removed: %s (err=%v)", p, err)
		}
	}
	// The repo must be writable again (index.lock gone).
	gitRun(t, dir, "commit", "--allow-empty", "-m", "post-heal")
}

// TestGitExecTimeout_SelfHealsIndexLock wires it together: a deadline-killed git
// triggers gitErr's self-heal, which clears a pre-existing index.lock. Uses the
// slow-git stub (unix) — same seam as the #630 timeout tests.
func TestGitExecTimeout_SelfHealsIndexLock(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("unix sleep stub; Go tests for this package run in the dev container / Linux CI")
	}
	dir := initRepoOnMain(t)
	stub := filepath.Join(t.TempDir(), "slowgit.sh")
	if err := os.WriteFile(stub, []byte("#!/bin/sh\nexec sleep 30\n"), 0o755); err != nil {
		t.Fatalf("write stub: %v", err)
	}
	lock := filepath.Join(dir, ".git", "index.lock")
	if err := os.WriteFile(lock, []byte("stale"), 0o644); err != nil {
		t.Fatalf("create index.lock: %v", err)
	}

	w := newTestWriter(dir, dir)
	w.gitBinary = stub
	w.gitTimeout = 200 * time.Millisecond
	w.gitWaitDelay = 500 * time.Millisecond

	if err := w.gitExec("status"); err == nil || !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("expected a timeout error, got %v", err)
	}
	if _, err := os.Stat(lock); !os.IsNotExist(err) {
		t.Errorf("index.lock not self-healed after timeout (err=%v)", err)
	}
}

// originRef is the remote-tracking ref a successful push of branch leaves
// in the pushing repo — where a test reads a PR branch once the write has
// deleted the local one (#641, #1724).
func originRef(branch string) string { return "refs/remotes/origin/" + branch }

// addBareRemote points repoDir's "origin" at a fresh bare local repo, so
// `git push` actually succeeds in tests: a PR-mode write whose push fails
// is a failed write (#1724).
func addBareRemote(t *testing.T, repoDir string) {
	t.Helper()
	remoteDir := t.TempDir()
	cmd := exec.Command("git", "init", "--bare", remoteDir)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Skipf("git init --bare: %v\n%s", err, out)
	}
	gitRun(t, repoDir, "remote", "add", "origin", remoteDir)
}

// TestWritePR_DeletesLocalBranchAfterConfirmedPush is the #641 regression: after
// a successful push, the local feature branch must be DROPPED (it's safely on
// origin and the PR is created from origin/<branch> — the local ref would just
// leak forever on this single long-lived replica). The REMOTE branch must remain.
func TestWritePR_DeletesLocalBranchAfterConfirmedPush(t *testing.T) {
	dir := initRepoOnMain(t)
	addBareRemote(t, dir)
	w := newTestWriter(dir, dir)

	res, err := w.WritePR(context.Background(), "db-a", "alice@example.com", validTenantYAML)
	if err != nil {
		t.Fatalf("WritePR: %v", err)
	}

	// Local branch must be GONE.
	if out, err := exec.Command("git", "-C", dir, "rev-parse", "--verify", "--quiet", "refs/heads/"+res.BranchName).CombinedOutput(); err == nil && len(strings.TrimSpace(string(out))) > 0 {
		t.Errorf("local feature branch %s still present after successful push (got %s) — #641 leak", res.BranchName, out)
	}
	// REMOTE branch must exist (the PR needs it). rev-parse --verify aborts the test on miss.
	gitOut(t, dir, "rev-parse", "--verify", "refs/remotes/origin/"+res.BranchName)
}

// TestWritePR_PushFailureFailsAndDropsBranch (#1724): when the push FAILS (no
// remote here) the write fails with *PushError — it used to log a WARN and
// return success, so the handler opened a PR/MR on a head origin did not
// hold — and the local feature branch is DROPPED, with the tree back on a
// clean base. #641 kept the branch as "the only copy of the commit"; a retry
// rebuilds the commit, and the kept branch is what a same-named later write
// collided with.
func TestWritePR_PushFailureFailsAndDropsBranch(t *testing.T) {
	dir := initRepoOnMain(t)
	// NO remote configured → push fails.
	w := newTestWriter(dir, dir)
	mainHead := gitOut(t, dir, "rev-parse", "main")

	res, err := w.WritePR(context.Background(), "db-a", "alice@example.com", validTenantYAML)
	if res != nil {
		t.Errorf("result = %+v, want nil on a failed push", res)
	}
	var pe *PushError
	if !errors.As(err, &pe) || !errors.Is(err, ErrPushFailed) {
		t.Fatalf("err = %v, want a *PushError wrapping ErrPushFailed", err)
	}
	if !strings.HasPrefix(pe.Branch, "tenant-api/db-a/") || pe.Cause == nil {
		t.Errorf("PushError = {Branch: %q, Cause: %v}, want the db-a branch and the git error", pe.Branch, pe.Cause)
	}
	if b := gitOut(t, dir, "branch", "--list", "tenant-api/*"); b != "" {
		t.Errorf("local feature branch left after a FAILED push:\n%s", b)
	}
	if cur := gitOut(t, dir, "rev-parse", "--abbrev-ref", "HEAD"); cur != "main" {
		t.Errorf("HEAD on %q, want main", cur)
	}
	if head := gitOut(t, dir, "rev-parse", "main"); head != mainHead {
		t.Errorf("main moved to %s, want %s", head, mainHead)
	}
	if st := gitOut(t, dir, "status", "--porcelain"); st != "" {
		t.Errorf("working tree dirty after the failed write:\n%s", st)
	}
}
