package gitops

// #1097 PR-mode coverage: WritePRBatch must MERGE each op's patch into the
// tenant's existing keys (preserving un-patched keys + comments) on the feature
// branch, not overwrite the whole file. Runs against a local git repo with no
// remote — push warns-and-continues, so the created batch branch is retained
// and we can inspect its committed content. (gitRun/gitOut live in
// writer_hardening_test.go.)

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// seedTenantRepo builds a local git repo (branch main) with _defaults.yaml and a
// db-a tenant file carrying several keys + a comment, then returns its path.
func seedTenantRepo(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"),
		[]byte("defaults:\n  mysql_connections: 80\n  mysql_cpu: 90\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	const seed = "tenants:\n" +
		"  db-a:\n" +
		"    mysql_connections: \"50\"  # warning threshold\n" +
		"    mysql_cpu: \"40\"\n" +
		"    _metadata:\n" +
		"      owner: \"team-x\"\n"
	if err := os.WriteFile(filepath.Join(dir, "db-a.yaml"), []byte(seed), 0o644); err != nil {
		t.Fatal(err)
	}
	gitRun(t, dir, "init")
	gitRun(t, dir, "config", "user.email", "test@test.com")
	gitRun(t, dir, "config", "user.name", "Test")
	gitRun(t, dir, "add", ".")
	gitRun(t, dir, "commit", "-m", "seed")
	gitRun(t, dir, "branch", "-M", "main")
	return dir
}

// TestWriteMerged_PreservesKeys is the direct commit-on-write #1097 guard: the
// read-merge-write runs under the lock and preserves un-patched keys + comments.
func TestWriteMerged_PreservesKeys(t *testing.T) {
	dir := seedTenantRepo(t)
	w := NewWriter(dir, dir)
	merge := func(existing []byte) (string, error) {
		return string(existing) + "    _silent_mode: warning\n", nil
	}
	if err := w.WriteMerged(context.Background(), "db-a", "op@example.com", merge); err != nil {
		t.Fatalf("WriteMerged: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"mysql_connections", "mysql_cpu", "_metadata", "team-x", "# warning threshold", "_silent_mode"} {
		if !strings.Contains(string(got), want) {
			t.Errorf("#1097 regression: committed file missing %q:\n%s", want, got)
		}
	}
}

// TestWriteMerged_IdempotentNoOp_NotConflict guards the #1097 self-review fix:
// a merge that changes nothing (an idempotent patch / a client retry after a
// succeeded write) must be a no-op SUCCESS, not a spurious ErrConflict. The
// pre-fix bug only fired when HEAD~1 existed (gitCommit no-ops → HEAD unmoved →
// commitFileChange's parent check misfired), so the repo needs >=2 commits.
func TestWriteMerged_IdempotentNoOp_NotConflict(t *testing.T) {
	dir := seedTenantRepo(t) // commit #1
	if err := os.WriteFile(filepath.Join(dir, "other.yaml"),
		[]byte("tenants:\n  z:\n    mysql_cpu: \"1\"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitRun(t, dir, "add", ".")
	gitRun(t, dir, "commit", "-m", "second") // commit #2 → HEAD~1 exists

	w := NewWriter(dir, dir)
	head0 := gitOut(t, dir, "rev-parse", "HEAD")
	// Idempotent merge: return the file verbatim (byte-identical → nothing staged).
	noop := func(existing []byte) (string, error) { return string(existing), nil }
	if err := w.WriteMerged(context.Background(), "db-a", "op@example.com", noop); err != nil {
		t.Fatalf("idempotent WriteMerged should be a no-op success, got: %v", err)
	}
	if head1 := gitOut(t, dir, "rev-parse", "HEAD"); head1 != head0 {
		t.Errorf("idempotent WriteMerged moved HEAD %s → %s (should not commit)", head0, head1)
	}
}

// TestWritePRBatch_AllNoOp_ReturnsErrNoChanges guards the #1102 review fix: a
// batch whose every op is byte-identical (idempotent) must return ErrNoChanges
// (→ clean "no changes" handler response) and leave NO feature branch behind,
// rather than pushing an empty branch / opening a change-free PR.
func TestWritePRBatch_AllNoOp_ReturnsErrNoChanges(t *testing.T) {
	dir := seedTenantRepo(t)
	w := NewWriter(dir, dir)
	noop := func(existing []byte) (string, error) { return string(existing), nil } // identity
	_, err := w.WritePRBatch(context.Background(), []PRBatchOp{{TenantID: "db-a", Merge: noop}}, "op@example.com")
	if !errors.Is(err, ErrNoChanges) {
		t.Fatalf("all-no-op batch: want ErrNoChanges, got: %v", err)
	}
	// No dangling feature branch may remain.
	if b := gitOut(t, dir, "branch", "--format=%(refname:short)"); strings.Contains(b, "tenant-api/batch/") {
		t.Errorf("all-no-op batch left a dangling branch:\n%s", b)
	}
}

func TestWritePRBatch_MergesPreservingKeys(t *testing.T) {
	dir := seedTenantRepo(t)
	w := NewWriter(dir, dir)
	// Exercise the writer plumbing (read current file under lock → merge →
	// validate → commit on branch). The merge is a passthrough that appends one
	// key: if WritePRBatch failed to read+pass the current file, `existing` would
	// be empty and the result would fail validate() (no tenants block) — so this
	// meaningfully proves the read-merge happens on the branch base.
	merge := func(existing []byte) (string, error) {
		return string(existing) + "    _silent_mode: warning\n", nil
	}
	if _, err := w.WritePRBatch(context.Background(), []PRBatchOp{{TenantID: "db-a", Merge: merge}}, "op@example.com"); err != nil {
		t.Fatalf("WritePRBatch: %v", err)
	}

	// Find the created batch branch (push failed → branch retained).
	branches := gitOut(t, dir, "branch", "--format=%(refname:short)")
	var branch string
	for _, ln := range strings.Split(branches, "\n") {
		ln = strings.TrimSpace(ln)
		if strings.HasPrefix(ln, "tenant-api/batch/") {
			branch = ln
			break
		}
	}
	if branch == "" {
		t.Fatalf("no tenant-api/batch/* branch found; branches:\n%s", branches)
	}

	committed := gitOut(t, dir, "show", branch+":db-a.yaml")
	for _, want := range []string{"mysql_connections", "mysql_cpu", "_metadata", "team-x", "# warning threshold", "_silent_mode"} {
		if !strings.Contains(committed, want) {
			t.Errorf("#1097 PR-mode regression: committed file missing %q:\n%s", want, committed)
		}
	}
}
