package gitops

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// TRK-318 — the in-lock base fetch that anchors each PR write on the freshest
// origin/<base>, closing the stale-local-base window a long-lived pod opens
// after a remote merge (ADR-023 §B).

// TestFetchTimeoutFromEnv covers TA_GIT_FETCH_TIMEOUT parsing + the clamp that
// keeps a fat-fingered 0/negative/garbage value from disabling the fail-loud
// safety net (an unbounded fetch would re-admit the stale-base hazard).
func TestFetchTimeoutFromEnv(t *testing.T) {
	cases := []struct {
		name string
		env  string
		want time.Duration
	}{
		{"unset → default", "", defaultGitFetchTimeout},
		{"valid duration", "2s", 2 * time.Second},
		{"valid sub-second", "750ms", 750 * time.Millisecond},
		{"unparseable → default", "not-a-duration", defaultGitFetchTimeout},
		{"zero → default", "0s", defaultGitFetchTimeout},
		{"negative → default", "-3s", defaultGitFetchTimeout},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("TA_GIT_FETCH_TIMEOUT", tc.env)
			if got := fetchTimeoutFromEnv(); got != tc.want {
				t.Errorf("fetchTimeoutFromEnv() = %v, want %v", got, tc.want)
			}
		})
	}
}

// TestFetchTimeoutIndependentFromGitTimeout proves the two knobs are wired
// separately on a fresh Writer: a custom git timeout must NOT bleed into the
// fetch timeout, and vice versa (ADR-023 §B: TA_GIT_FETCH_TIMEOUT is
// deliberately independent of TENANT_API_GIT_TIMEOUT).
func TestFetchTimeoutIndependentFromGitTimeout(t *testing.T) {
	t.Setenv("TENANT_API_GIT_TIMEOUT", "90s")
	t.Setenv("TA_GIT_FETCH_TIMEOUT", "")
	w := NewWriter(t.TempDir(), "")
	if w.gitTimeout != 90*time.Second {
		t.Errorf("gitTimeout = %v, want 90s", w.gitTimeout)
	}
	if w.fetchTimeout != defaultGitFetchTimeout {
		t.Errorf("fetchTimeout = %v, want default %v (must not inherit the git timeout)",
			w.fetchTimeout, defaultGitFetchTimeout)
	}
}

// gitClone clones remote into dst (skips the test if git is unavailable).
func gitClone(t *testing.T, remote, dst string) {
	t.Helper()
	if out, err := exec.Command("git", "clone", remote, dst).CombinedOutput(); err != nil {
		t.Skipf("git clone: %v\n%s", err, out)
	}
}

// initBareRemoteOnMain creates a bare remote whose default branch is "main".
func initBareRemoteOnMain(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	if out, err := exec.Command("git", "init", "--bare", "-b", "main", dir).CombinedOutput(); err != nil {
		t.Skipf("git init --bare: %v\n%s", err, out)
	}
	return dir
}

func writeFileInDir(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
}

// TestWritePR_AnchorsOnFreshOriginBase is the TRK-318 cross-merge regression on a
// SHARED file. Disaster scenario: tenant A merges a change to _groups.yaml
// remotely; a long-lived pod's local base goes stale (no fetch since startup);
// tenant B then opens a PR. Branched from the stale base, B's PR would silently
// roll _groups.yaml back to A's pre-merge version (silent data loss). After the
// fix, WritePR fetches + anchors on origin/main, so the branch carries A's merged
// _groups.yaml and only ADDS db-b.yaml — no rollback.
func TestWritePR_AnchorsOnFreshOriginBase(t *testing.T) {
	remoteDir := initBareRemoteOnMain(t)

	// "author" clone seeds the repo and later advances the remote (simulating
	// another tenant's merged PR landing on origin/main).
	authorDir := t.TempDir()
	gitClone(t, remoteDir, authorDir)
	gitRun(t, authorDir, "config", "user.email", "a@a.com")
	gitRun(t, authorDir, "config", "user.name", "A")
	writeFileInDir(t, authorDir, "_groups.yaml", "groups:\n  team-a:\n    members: [db-a]\n")
	writeFileInDir(t, authorDir, "db-a.yaml", validTenantYAML)
	gitRun(t, authorDir, "add", "-A")
	gitRun(t, authorDir, "commit", "-m", "seed shared file + db-a")
	gitRun(t, authorDir, "push", "origin", "main")

	// The tenant-api's long-lived clone: fetched once here ("pod startup"), then
	// never again until the fix's in-lock fetch.
	dir := t.TempDir()
	gitClone(t, remoteDir, dir)
	gitRun(t, dir, "config", "user.email", "t@t.com")
	gitRun(t, dir, "config", "user.name", "T")

	// Tenant A merges a change to the SHARED file remotely. The Writer's clone
	// does NOT have it (its local main is now stale).
	writeFileInDir(t, authorDir, "_groups.yaml", "groups:\n  team-a:\n    members: [db-a, db-c]\n")
	gitRun(t, authorDir, "commit", "-am", "A merges a shared-file change")
	gitRun(t, authorDir, "push", "origin", "main")

	// Tenant B opens a PR from the (stale) local base.
	w := NewWriter(dir, dir)
	res, err := w.WritePR("db-b", "bob@example.com", "tenants:\n  db-b:\n    _silent_mode: \"critical\"\n")
	if err != nil {
		t.Fatalf("WritePR: %v", err)
	}

	// The pushed branch must carry A's merged _groups.yaml (v2 with db-c), proving
	// it was branched from the FRESH origin/main — not the stale local base that
	// would have rolled it back.
	got := gitOut(t, dir, "show", "refs/remotes/origin/"+res.BranchName+":_groups.yaml")
	if !strings.Contains(got, "db-c") {
		t.Errorf("branch _groups.yaml = %q, want A's merged change (db-c present) — stale base rolled back the shared file (TRK-318)", got)
	}
	// And the branch's only delta vs the fresh origin/main is db-b.yaml.
	diff := gitOut(t, dir, "diff", "--name-only", "origin/main", "refs/remotes/origin/"+res.BranchName)
	if diff != "db-b.yaml" {
		t.Errorf("branch vs origin/main changed files = %q, want only db-b.yaml (no shared-file rollback)", diff)
	}
}

// dispatchGitStub writes a git wrapper that sleeps forever on `git fetch ...`
// (simulating a hung/degraded forge) but delegates every other subcommand to the
// real git on PATH — so checkout/reset/commit/push still work while only the
// in-lock fetch hits the deadline.
func dispatchGitStub(t *testing.T) string {
	t.Helper()
	stub := filepath.Join(t.TempDir(), "git-fetch-hangs.sh")
	script := "#!/bin/sh\n" +
		"for a in \"$@\"; do\n" +
		"  if [ \"$a\" = \"fetch\" ]; then exec sleep 30; fi\n" +
		"done\n" +
		"exec git \"$@\"\n"
	if err := os.WriteFile(stub, []byte(script), 0o755); err != nil {
		t.Fatalf("write stub: %v", err)
	}
	return stub
}

// TestWritePR_FetchTimeout_ReturnsForgeDegraded is the fail-loud regression: when
// the in-lock fetch exceeds TA_GIT_FETCH_TIMEOUT, WritePR must return
// ErrForgeDegraded PROMPTLY (releasing the writer mutex) rather than silently
// branching from a stale base or pinning the lock for the full 30s hang.
func TestWritePR_FetchTimeout_ReturnsForgeDegraded(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("unix sleep stub; Go tests for this package run in the dev container / Linux CI")
	}
	// Real repo + real bare remote so the delegated checkout/reset/remote calls
	// succeed and origin/main exists — the fetch is the ONLY thing that hangs.
	remoteDir := initBareRemoteOnMain(t)
	dir := initRepoOnMain(t)
	gitRun(t, dir, "remote", "add", "origin", remoteDir)
	gitRun(t, dir, "push", "origin", "main")

	w := NewWriter(dir, dir)
	w.gitBinary = dispatchGitStub(t)
	w.fetchTimeout = 200 * time.Millisecond
	w.gitWaitDelay = 500 * time.Millisecond

	start := time.Now()
	_, err := w.WritePR("db-a", "alice@example.com", validTenantYAML)
	elapsed := time.Since(start)

	if !errors.Is(err, ErrForgeDegraded) {
		t.Fatalf("WritePR err = %v, want ErrForgeDegraded", err)
	}
	// deadline (200ms) + WaitDelay (500ms) + slack. A regression that proceeds on
	// the stale base or holds the lock for the full sleep would blow this ceiling.
	if elapsed > 5*time.Second {
		t.Errorf("WritePR blocked for %v — the fetch deadline did not release the lock promptly", elapsed)
	}

	// The lock must be free: a follow-up call proceeds (it will itself time out on
	// fetch, but it RUNS — proving the mutex wasn't pinned by the first call).
	done := make(chan struct{})
	go func() {
		_, _ = w.WritePR("db-a", "alice@example.com", validTenantYAML)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Error("second WritePR did not run within 5s — first call leaked the writer mutex")
	}
}

// TestWritePR_PreservesLocalBaseCommit is the safety invariant the fresh-anchor
// must NOT violate: the special-file writes (WriteGroupsFile / WriteViewsFile /
// WriteFederationPolicyFile) commit straight to the local base via
// commitFileChange even in PR mode, so the local base can carry an un-pushed
// commit. Cutting the PR branch from origin/<base> must leave that local commit
// (and its working-tree state) intact — a `reset --hard origin/<base>` would
// silently discard it. This test fails loudly if the anchor regresses to a hard
// reset.
func TestWritePR_PreservesLocalBaseCommit(t *testing.T) {
	remoteDir := initBareRemoteOnMain(t)

	authorDir := t.TempDir()
	gitClone(t, remoteDir, authorDir)
	gitRun(t, authorDir, "config", "user.email", "a@a.com")
	gitRun(t, authorDir, "config", "user.name", "A")
	writeFileInDir(t, authorDir, "db-a.yaml", validTenantYAML)
	gitRun(t, authorDir, "add", "-A")
	gitRun(t, authorDir, "commit", "-m", "seed")
	gitRun(t, authorDir, "push", "origin", "main")

	dir := t.TempDir()
	gitClone(t, remoteDir, dir)
	gitRun(t, dir, "config", "user.email", "t@t.com")
	gitRun(t, dir, "config", "user.name", "T")

	// A direct-committed shared file lands on the LOCAL base only (never pushed) —
	// exactly what WriteGroupsFile does in PR mode.
	const localGroups = "groups:\n  local-only:\n    members: [db-a]\n"
	writeFileInDir(t, dir, "_groups.yaml", localGroups)
	gitRun(t, dir, "add", "_groups.yaml")
	gitRun(t, dir, "commit", "-m", "local-only groups edit (not pushed)")

	// Meanwhile origin/main advances elsewhere, so the fresh anchor has real work.
	writeFileInDir(t, authorDir, "db-a.yaml", "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	gitRun(t, authorDir, "commit", "-am", "remote advance")
	gitRun(t, authorDir, "push", "origin", "main")

	w := NewWriter(dir, dir)
	if _, err := w.WritePR("db-b", "bob@example.com", "tenants:\n  db-b:\n    _silent_mode: \"warning\"\n"); err != nil {
		t.Fatalf("WritePR: %v", err)
	}

	// The local-only commit must survive on disk and in local base history.
	// Normalize CRLF — git's autocrlf may rewrite line endings on checkout (Windows).
	denorm := func(s string) string { return strings.ReplaceAll(s, "\r\n", "\n") }
	if got, err := os.ReadFile(filepath.Join(dir, "_groups.yaml")); err != nil {
		t.Fatalf("read _groups.yaml after WritePR: %v", err)
	} else if denorm(string(got)) != localGroups {
		t.Errorf("_groups.yaml = %q after WritePR, want the local-only edit preserved (%q) — the fresh anchor discarded an un-pushed local base commit (TRK-318 regression)", got, localGroups)
	}
	if head := denorm(gitOut(t, dir, "show", "main:_groups.yaml")); head != strings.TrimRight(localGroups, "\n") {
		t.Errorf("main:_groups.yaml = %q, want the local-only commit intact", head)
	}
}

// TestWritePR_NoOriginSkipsFetch guards the dev/local path: with no origin remote
// configured, WritePR must NOT fail (nothing to be stale against) — it branches
// from the local base exactly as before TRK-318.
func TestWritePR_NoOriginSkipsFetch(t *testing.T) {
	dir := initRepoOnMain(t)
	w := NewWriter(dir, dir) // no remote added

	if _, err := w.WritePR("db-a", "alice@example.com", validTenantYAML); err != nil {
		t.Fatalf("WritePR without an origin remote should skip the fetch and succeed, got: %v", err)
	}
}
