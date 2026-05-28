package gitops

import (
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
	w := NewWriter(dir, dir) // baseBranch defaults to "main"

	mainHead := gitOut(t, dir, "rev-parse", "main")

	// Strand the tree on another tenant's feature branch with an extra commit.
	gitRun(t, dir, "checkout", "-b", "tenant-api/other/stuck")
	gitRun(t, dir, "commit", "--allow-empty", "-m", "other tenant's unpushed work")

	// WritePR for db-a (push to a nonexistent origin fails and is swallowed).
	res, err := w.WritePR("db-a", "alice@example.com", validTenantYAML)
	if err != nil {
		t.Fatalf("WritePR: %v", err)
	}

	// The feature branch's parent MUST be main's tip, NOT the stuck branch's.
	if parent := gitOut(t, dir, "rev-parse", res.BranchName+"~1"); parent != mainHead {
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
	w := NewWriter(dir, dir)
	w.SetBaseBranch("nonexistent-base")

	if _, err := w.WritePR("db-a", "alice@example.com", validTenantYAML); err == nil {
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
	w := NewWriter(dir, dir)
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
	res, err := w.WritePR("db-b", "alice@example.com", "tenants:\n  db-b:\n    _silent_mode: \"warning\"\n")
	if err != nil {
		t.Fatalf("WritePR did not recover from a dirty tree: %v", err)
	}
	if parent := gitOut(t, dir, "rev-parse", res.BranchName+"~1"); parent != mainHead {
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
	w := NewWriter(dir, dir)

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

	w := NewWriter(dir, dir)
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
