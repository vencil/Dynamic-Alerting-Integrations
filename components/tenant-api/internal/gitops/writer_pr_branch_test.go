package gitops

// #1724: PR-mode branch names and push failures.
//
// The branch name used to be "<prefix><tenant>/<yyyymmdd-hhmmss>" — resolved
// to the second — so two writes in one second cut the same branch: the
// second push was rejected (non-fast-forward), logged at WARN, and the write
// returned success with the FIRST write's branch name; a third write in that
// second hit the kept local branch and failed `checkout -b` (500). Now the
// name carries a random suffix, and a failed push fails the write
// (*PushError), drops the local branch and returns the tree to base.
//
// The clock and suffix seams (prBranchClock / prBranchSuffix) make "the same
// second" and "the same name" deterministic.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/platform"
)

// fixedSecond pins prBranchName's clock to one instant.
func fixedSecond(w *Writer) {
	at := time.Date(2026, 10, 20, 14, 10, 27, 0, time.UTC)
	w.prBranchClock = func() time.Time { return at }
}

func TestPRBranchName_Shape(t *testing.T) {
	t.Parallel()
	w := newTestWriter(t.TempDir(), "")
	fixedSecond(w)
	re := regexp.MustCompile(`^tenant-api/db-a/20261020-141027-[0-9a-f]{12}$`)
	seen := map[string]bool{}
	for range 64 {
		name, err := w.prBranchName(platform.BranchPrefix + "db-a/")
		if err != nil {
			t.Fatal(err)
		}
		if !re.MatchString(name) {
			t.Fatalf("branch name %q, want %s", name, re)
		}
		if seen[name] {
			t.Fatalf("branch name %q repeated within one second", name)
		}
		seen[name] = true
		// The suffix must not change which tenant the branch names.
		if got := platform.TenantIDFromBranch(name); got != "db-a" {
			t.Errorf("TenantIDFromBranch(%q) = %q, want db-a", name, got)
		}
	}
	batch, err := w.prBranchName(platform.BatchBranchPrefix)
	if err != nil {
		t.Fatal(err)
	}
	if !platform.IsBatchBranch(batch) || platform.TenantIDFromBranch(batch) != "" {
		t.Errorf("batch branch %q: IsBatchBranch=%t TenantIDFromBranch=%q, want true and \"\"",
			batch, platform.IsBatchBranch(batch), platform.TenantIDFromBranch(batch))
	}
}

// Two writes of the same tenant in the same second both succeed, on two
// branches, and origin holds each one's content.
func TestWritePR_SameSecondSameTenant_TwoBranches(t *testing.T) {
	t.Parallel()
	dir := initRepoOnMain(t)
	addBareRemote(t, dir)
	w := newTestWriter(dir, dir)
	fixedSecond(w)

	bodies := []string{
		"tenants:\n  db-a:\n    _silent_mode: \"warning\"\n",
		"tenants:\n  db-a:\n    _silent_mode: \"critical\"\n",
	}
	var branches []string
	for i, body := range bodies {
		res, err := w.WritePR(context.Background(), "db-a", "alice@example.com", body)
		if err != nil {
			t.Fatalf("write %d: %v", i+1, err)
		}
		branches = append(branches, res.BranchName)
	}
	if branches[0] == branches[1] {
		t.Fatalf("both writes in one second got branch %q", branches[0])
	}
	for i, b := range branches {
		if got := gitOut(t, dir, "show", originRef(b)+":db-a.yaml"); got+"\n" != bodies[i] {
			t.Errorf("origin %s holds %q, want write %d's body %q", b, got, i+1, bodies[i])
		}
	}
}

// Two batches over different tenants in the same second: both succeed, on
// two branches. The batch path takes no per-tenant claim, so this was the
// easiest one to hit.
func TestWritePRBatch_SameSecond_TwoBranches(t *testing.T) {
	t.Parallel()
	dir := seedTenantRepo(t)
	addBareRemote(t, dir)
	w := newTestWriter(dir, dir)
	fixedSecond(w)

	appendKey := func(line string) MergeFunc {
		return func(existing []byte) (string, error) { return string(existing) + line, nil }
	}
	var branches []string
	for i, line := range []string{"    _silent_mode: warning\n", "    _silent_mode: critical\n"} {
		res, err := w.WritePRBatch(context.Background(), []PRBatchOp{{TenantID: "db-a", Merge: appendKey(line)}}, "op@example.com")
		if err != nil {
			t.Fatalf("batch %d: %v", i+1, err)
		}
		branches = append(branches, res.BranchName)
		if got := gitOut(t, dir, "show", originRef(res.BranchName)+":db-a.yaml"); !strings.Contains(got, strings.TrimSpace(line)) {
			t.Errorf("origin %s holds %q, want batch %d's %q", res.BranchName, got, i+1, line)
		}
	}
	if branches[0] == branches[1] {
		t.Fatalf("both batches in one second got branch %q", branches[0])
	}
}

// When two writes DO get the same name (the suffix seam forces it), the
// second push is rejected: that write fails with *PushError — not a success
// carrying the first write's branch — leaves no local branch behind, and
// origin keeps the first write's content. A third write, with its own name,
// then succeeds: nothing of the failed one is in its way.
func TestWritePR_SameNameSecondPushFails(t *testing.T) {
	t.Parallel()
	dir := initRepoOnMain(t)
	addBareRemote(t, dir)
	w := newTestWriter(dir, dir)
	fixedSecond(w)
	suffix := "000000000000"
	w.prBranchSuffix = func() string { return suffix }

	first := "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	res, err := w.WritePR(context.Background(), "db-a", "alice@example.com", first)
	if err != nil {
		t.Fatalf("first write: %v", err)
	}
	res2, err := w.WritePR(context.Background(), "db-a", "alice@example.com", "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	var pe *PushError
	if !errors.As(err, &pe) {
		t.Fatalf("second write = (%+v, %v), want a *PushError", res2, err)
	}
	if pe.Branch != res.BranchName {
		t.Errorf("PushError.Branch = %q, want the colliding %q", pe.Branch, res.BranchName)
	}
	if b := gitOut(t, dir, "branch", "--list", "tenant-api/*"); b != "" {
		t.Errorf("local branch left after the failed push:\n%s", b)
	}
	if cur := gitOut(t, dir, "rev-parse", "--abbrev-ref", "HEAD"); cur != "main" {
		t.Errorf("HEAD on %q, want main", cur)
	}
	if got := gitOut(t, dir, "show", originRef(res.BranchName)+":db-a.yaml"); got+"\n" != first {
		t.Errorf("origin %s holds %q, want the first write's body", res.BranchName, got)
	}

	suffix = "111111111111"
	if _, err := w.WritePR(context.Background(), "db-a", "alice@example.com", "tenants:\n  db-a:\n    _silent_mode: \"info\"\n"); err != nil {
		t.Fatalf("third write after the failed one: %v", err)
	}
}

// A batch whose push fails: *PushError, no local batch branch, no staging
// worktree, and the serving tree clean on main.
func TestWritePRBatch_PushFailureFailsAndDropsBranch(t *testing.T) {
	t.Parallel()
	dir := seedTenantRepo(t) // no origin → the push fails
	w := newTestWriter(dir, dir)
	mainHead := gitOut(t, dir, "rev-parse", "main")

	merge := func(existing []byte) (string, error) { return string(existing) + "    _silent_mode: warning\n", nil }
	res, err := w.WritePRBatch(context.Background(), []PRBatchOp{{TenantID: "db-a", Merge: merge}}, "op@example.com")
	var pe *PushError
	if !errors.As(err, &pe) || !errors.Is(err, ErrPushFailed) {
		t.Fatalf("WritePRBatch = (%+v, %v), want a *PushError wrapping ErrPushFailed", res, err)
	}
	if !platform.IsBatchBranch(pe.Branch) {
		t.Errorf("PushError.Branch = %q, want a batch branch", pe.Branch)
	}
	if b := gitOut(t, dir, "branch", "--list", "tenant-api/*"); b != "" {
		t.Errorf("local batch branch left after the failed push:\n%s", b)
	}
	if wt := gitOut(t, dir, "worktree", "list", "--porcelain"); strings.Count(wt, "worktree ") != 1 {
		t.Errorf("staging worktree left behind:\n%s", wt)
	}
	if cur := gitOut(t, dir, "rev-parse", "--abbrev-ref", "HEAD"); cur != "main" {
		t.Errorf("HEAD on %q, want main", cur)
	}
	if head := gitOut(t, dir, "rev-parse", "main"); head != mainHead {
		t.Errorf("main moved to %s, want %s", head, mainHead)
	}
	if st := gitOut(t, dir, "status", "--porcelain"); st != "" {
		t.Errorf("serving tree dirty after the failed batch:\n%s", st)
	}
}

// A push that times out is a failed push too: *PushError, and it does NOT
// unwrap to ErrGitTimeout (nor context.DeadlineExceeded) — the handler
// answers it as GIT_PUSH_FAILED, not as the "nothing written" 503 of a git
// timeout (#3173) — while the cause still records the timeout for the log.
func TestWritePR_PushTimeoutIsPushError(t *testing.T) {
	t.Parallel()
	dir := initRepoOnMain(t)
	addBareRemote(t, dir)
	hooks := t.TempDir()
	if err := os.WriteFile(filepath.Join(hooks, "pre-push"), []byte("#!/bin/sh\nexec sleep 30\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	gitRun(t, dir, "config", "core.hooksPath", hooks)
	w := newTestWriter(dir, dir)
	w.gitTimeout = 2 * time.Second
	w.gitWaitDelay = 200 * time.Millisecond

	_, err := w.WritePR(context.Background(), "db-a", "alice@example.com", validTenantYAML)
	var pe *PushError
	if !errors.As(err, &pe) {
		t.Fatalf("err = %v, want a *PushError", err)
	}
	// #3173: a git timeout is ErrGitTimeout, never context.DeadlineExceeded;
	// the push's stays in Cause and does not reach the caller's errors.Is.
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, ErrGitTimeout) {
		t.Errorf("a push timeout unwraps to a timeout sentinel: %v", err)
	}
	if !errors.Is(pe.Cause, ErrGitTimeout) {
		t.Errorf("PushError.Cause = %v, want the git timeout", pe.Cause)
	}
	if b := gitOut(t, dir, "branch", "--list", "tenant-api/*"); b != "" {
		t.Errorf("local branch left after the timed-out push:\n%s", b)
	}
	if cur := gitOut(t, dir, "rev-parse", "--abbrev-ref", "HEAD"); cur != "main" {
		t.Errorf("HEAD on %q, want main", cur)
	}
}
