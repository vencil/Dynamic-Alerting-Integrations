package gitops

// Direct-commit no-op coverage: re-writing a tenant file with the content it
// already has must be an idempotent SUCCESS, not a conflict.
//
// commitFileChange detects a concurrent external commit by checking that the
// commit it just created hangs off the HEAD it recorded beforehand. When `git
// add` stages nothing — the body equals what HEAD already holds — no commit is
// created at all, HEAD does not move, and HEAD~1 is the previous commit rather
// than the recorded HEAD. Read as a conflict, that turns every idempotent
// re-write into a permanent 409: retrying sends the same bytes and gets the
// same answer, and no client action can clear it.
//
// WriteMerged's no-op short-circuit already documented and sidestepped this
// misfire for the merge paths; these tests pin it closed for Write() and for
// the special-file writers, which reach commitFileChange directly.

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// initRepoWithDefaults builds a git repo whose working tree is also a configDir
// carrying the pilot _defaults.yaml, so tenant-only bodies validate.
func initRepoWithDefaults(t *testing.T) string {
	t.Helper()
	dir := pilotDefaultsDir(t)
	cmds := [][]string{
		{"init", "-b", "main"},
		{"config", "user.email", "t@t.com"},
		{"config", "user.name", "T"},
		{"add", "."},
		{"commit", "-m", "initial"},
	}
	for _, args := range cmds {
		cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Skipf("git %v unavailable: %v\n%s", args, err, out)
		}
	}
	return dir
}

func TestWrite_IdenticalBodyIsIdempotentSuccess(t *testing.T) {
	t.Parallel()
	dir := initRepoWithDefaults(t)
	w := NewWriter(dir, dir)
	ctx := context.Background()
	const body = "tenants:\n  db-a:\n    mysql_threads_running: \"70\"\n"

	if _, err := w.Write(ctx, "db-a", "op@example.com", body); err != nil {
		t.Fatalf("first write: %v", err)
	}
	headAfterFirst := gitRevParse(t, dir, "HEAD")

	// Same bytes again: nothing to stage, nothing to commit.
	for i := 2; i <= 3; i++ {
		if _, err := w.Write(ctx, "db-a", "op@example.com", body); err != nil {
			t.Fatalf("write #%d with unchanged body: %v (ErrConflict=%v)",
				i, err, errors.Is(err, ErrConflict))
		}
	}
	if got := gitRevParse(t, dir, "HEAD"); got != headAfterFirst {
		t.Errorf("HEAD moved on a no-op re-write: %s -> %s", headAfterFirst, got)
	}
}

// A no-op re-write must not mask a REAL conflict: once the body changes, the
// external-commit detection still has to fire.
func TestWrite_ExternalCommitStillConflicts(t *testing.T) {
	t.Parallel()
	dir := initRepoWithDefaults(t)
	w := NewWriter(dir, dir)
	ctx := context.Background()

	if _, err := w.Write(ctx, "db-a", "op@example.com",
		"tenants:\n  db-a:\n    mysql_threads_running: \"70\"\n"); err != nil {
		t.Fatalf("seed write: %v", err)
	}

	// Advance the branch behind the writer's back, in the window between the
	// HEAD it records and the commit it creates. See the equivalent helper in
	// internal/handler/tenant_batch_errmap_test.go for why this has to be a
	// one-shot post-index-change hook rather than pre-commit.
	hook := filepath.Join(dir, ".git", "hooks", "post-index-change")
	const script = `#!/bin/sh
set -e
marker="$(git rev-parse --git-dir)/external-commit-done"
[ -e "$marker" ] && exit 0
: > "$marker"
ref=$(git symbolic-ref HEAD)
tree=$(git rev-parse HEAD^{tree})
new=$(git commit-tree "$tree" -p HEAD -m "external commit")
git update-ref "$ref" "$new"
`
	if err := os.WriteFile(hook, []byte(script), 0o755); err != nil {
		t.Fatalf("install post-index-change hook: %v", err)
	}

	_, err := w.Write(ctx, "db-a", "op@example.com",
		"tenants:\n  db-a:\n    mysql_threads_running: \"75\"\n")
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("changed body over an external commit: err = %v, want ErrConflict", err)
	}
}

func gitRevParse(t *testing.T, dir, rev string) string {
	t.Helper()
	out, err := exec.Command("git", "-C", dir, "rev-parse", rev).Output()
	if err != nil {
		t.Fatalf("git rev-parse %s: %v", rev, err)
	}
	return string(out[:len(out)-1])
}
