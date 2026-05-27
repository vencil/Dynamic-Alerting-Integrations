package gitops

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// TestGitTimeoutFromEnv covers TENANT_API_GIT_TIMEOUT parsing + the clamp that
// keeps a fat-fingered 0/negative/garbage value from disabling the #630 safety
// net (an unbounded git would re-introduce the global write freeze).
func TestGitTimeoutFromEnv(t *testing.T) {
	cases := []struct {
		name string
		env  string // "" exercises the unset path (gitTimeoutFromEnv treats it as unset)
		want time.Duration
	}{
		{"unset → default", "", defaultGitTimeout},
		{"valid duration", "5s", 5 * time.Second},
		{"valid sub-second", "250ms", 250 * time.Millisecond},
		{"unparseable → default", "not-a-duration", defaultGitTimeout},
		{"zero → default", "0s", defaultGitTimeout},
		{"negative → default", "-3s", defaultGitTimeout},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("TENANT_API_GIT_TIMEOUT", tc.env)
			if got := gitTimeoutFromEnv(); got != tc.want {
				t.Errorf("gitTimeoutFromEnv() = %v, want %v", got, tc.want)
			}
		})
	}
}

// TestNewWriterGitDefaults verifies a freshly constructed Writer is wired for the
// timeout path: real "git" binary + the default deadline when the env is unset.
func TestNewWriterGitDefaults(t *testing.T) {
	t.Setenv("TENANT_API_GIT_TIMEOUT", "")
	w := NewWriter(t.TempDir(), "")
	if w.gitBinary != "git" {
		t.Errorf("gitBinary = %q, want \"git\"", w.gitBinary)
	}
	if w.gitTimeout != defaultGitTimeout {
		t.Errorf("gitTimeout = %v, want %v", w.gitTimeout, defaultGitTimeout)
	}
}

// TestGitExecTimeout_HungGitReleasesLock is the core #630 regression: a git child
// that never returns (degraded forge / network microcut on push) must hit the
// per-command deadline and fail LOUDLY, instead of holding the writer mutex (and
// therefore freezing every tenant's writes) forever.
//
// We point gitBinary at a stub that blocks far longer than the deadline. The stub
// `exec`s sleep so it BECOMES the sleeping process — CommandContext's SIGKILL then
// hits it directly and the output pipe closes promptly (a plain child `sleep`
// would orphan, keep the pipe's write end open, and stall CombinedOutput).
func TestGitExecTimeout_HungGitReleasesLock(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("unix sleep stub; Go tests for this package run in the dev container / Linux CI")
	}
	dir := t.TempDir()
	stub := filepath.Join(dir, "slowgit.sh")
	if err := os.WriteFile(stub, []byte("#!/bin/sh\nexec sleep 30\n"), 0o755); err != nil {
		t.Fatalf("write stub: %v", err)
	}

	w := NewWriter(dir, dir)
	w.gitBinary = stub
	w.gitTimeout = 200 * time.Millisecond
	w.gitWaitDelay = 500 * time.Millisecond

	start := time.Now()
	err := w.gitExec("push", "origin", "feature") // args are ignored by the stub
	elapsed := time.Since(start)

	if err == nil {
		t.Fatal("expected a timeout error from a hung git, got nil")
	}
	if !strings.Contains(err.Error(), "timed out") {
		t.Errorf("error = %q, want it to mention 'timed out'", err.Error())
	}
	// The whole point of #630: the deadline fires ~immediately (≈200ms), not after
	// the stub's 30s sleep. A generous 5s ceiling keeps the assert non-flaky on slow
	// CI while still proving the lock isn't held for the full sleep.
	if elapsed > 5*time.Second {
		t.Errorf("gitExec blocked for %v — the deadline did not release the lock promptly", elapsed)
	}
}

// TestGitExecTimeout_GrandchildHoldsPipe is the harder, production-shaped variant:
// a real `git push` over HTTPS/SSH forks a remote-helper (`git-remote-https`/`ssh`)
// that inherits git's stdout/stderr. CommandContext SIGKILLs only the direct git
// child on deadline; if a surviving grandchild keeps the pipe's write-end open,
// CombinedOutput's reader (and thus Wait) would block far past the deadline — the
// writer mutex would stay pinned and #630 would NOT actually be fixed. cmd.WaitDelay
// is what guarantees Wait returns anyway. The stub reproduces exactly that: a
// backgrounded `sleep` (the surviving grandchild holding the pipe) plus an exec'd
// `sleep` as the direct child that gets killed. Without WaitDelay this blocks for
// the full stub sleep; with it, it returns shortly after the deadline.
func TestGitExecTimeout_GrandchildHoldsPipe(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("unix sleep stub; Go tests for this package run in the dev container / Linux CI")
	}
	dir := t.TempDir()
	stub := filepath.Join(dir, "slowgit-fork.sh")
	// `sleep 10 &` is the grandchild that inherits stdout and outlives the SIGKILL;
	// `exec sleep 10` is the direct child the deadline kills. 10s ≫ deadline so a
	// regression (WaitDelay removed) hangs ~10s and still fails the elapsed assert.
	if err := os.WriteFile(stub, []byte("#!/bin/sh\nsleep 10 &\nexec sleep 10\n"), 0o755); err != nil {
		t.Fatalf("write stub: %v", err)
	}

	w := NewWriter(dir, dir)
	w.gitBinary = stub
	w.gitTimeout = 200 * time.Millisecond
	w.gitWaitDelay = 500 * time.Millisecond

	start := time.Now()
	err := w.gitExec("push", "origin", "feature")
	elapsed := time.Since(start)

	if err == nil {
		t.Fatal("expected a timeout error from a hung git, got nil")
	}
	if !strings.Contains(err.Error(), "timed out") {
		t.Errorf("error = %q, want it to mention 'timed out'", err.Error())
	}
	// deadline (200ms) + WaitDelay (500ms) + slack. Without WaitDelay this would be
	// ~10s (the grandchild holding the pipe), so a 3s ceiling is a real regression guard.
	if elapsed > 3*time.Second {
		t.Errorf("gitExec blocked for %v — WaitDelay did not release the lock despite a grandchild holding the pipe", elapsed)
	}
}

// TestGitExecSucceedsUnderTimeout guards the happy path of the seam: a git that
// returns well within the deadline must NOT be reported as an error.
func TestGitExecSucceedsUnderTimeout(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("unix shell stub; Go tests for this package run in the dev container / Linux CI")
	}
	dir := t.TempDir()
	stub := filepath.Join(dir, "fastgit.sh")
	if err := os.WriteFile(stub, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatalf("write stub: %v", err)
	}

	w := NewWriter(dir, dir)
	w.gitBinary = stub
	w.gitTimeout = 5 * time.Second

	if err := w.gitExec("status"); err != nil {
		t.Errorf("gitExec on a fast git returned error: %v", err)
	}
}
