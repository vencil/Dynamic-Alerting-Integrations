package gitops

// #1231 1b: deprecated-key alias notices must flow through the writer chain —
// validate() exposes the advisory channel, and every write entry point
// (Write / WriteMerged / WritePR / WritePRBatch) threads it up to its caller
// WITHOUT ever blocking the write. Fixtures cover BOTH transition states:
// old-spelled defaults (pre-platform-rename, today's tree) and new-spelled
// defaults (the shipped state after #1231 commit 2 renames _defaults.yaml).

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// newNameDefaultsDir is the post-rename twin of pilotDefaultsDir: the platform
// defaults already carry the canonical spelling (mysql_threads_running),
// while the tenant body still writes the deprecated mysql_cpu.
func newNameDefaultsDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	const defaults = "defaults:\n  container_cpu: 80\n  mysql_threads_running: 80\n"
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte(defaults), 0o644); err != nil {
		t.Fatalf("write _defaults.yaml: %v", err)
	}
	return dir
}

// assertDeprecationNotice pins the author-facing wording contract on the
// surface handlers actually ship (#1231 c2): the notice must name BOTH
// spellings, must NOT contain the substring "skipping" (the Python CI fatal
// predicate is `"WARN" in w and "skipping" in w`), and must NOT start with
// "ERROR:" (the second fatal predicate).
func assertDeprecationNotice(t *testing.T, notices []string) {
	t.Helper()
	if len(notices) == 0 {
		t.Fatal("expected at least one deprecation notice, got none")
	}
	joined := strings.Join(notices, "; ")
	if !strings.Contains(joined, "mysql_cpu") || !strings.Contains(joined, "mysql_threads_running") {
		t.Errorf("notice must name both the old and the new spelling, got: %v", notices)
	}
	for _, n := range notices {
		if strings.Contains(n, "skipping") {
			t.Errorf("notice must not contain 'skipping' (Python CI fatal predicate), got: %q", n)
		}
		if strings.HasPrefix(strings.TrimSpace(n), "ERROR:") {
			t.Errorf("notice must not start with 'ERROR:' (blocking-prefix predicate), got: %q", n)
		}
	}
}

func TestValidate_DeprecatedKeyNotices(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name       string
		dir        func(*testing.T) string
		body       string
		wantErrs   bool
		wantNotice bool
	}{
		{
			// Today's tree: defaults still old-spelled. The override matches
			// defaults verbatim, but the alias table already flags the key.
			name:       "old-named defaults + old key override",
			dir:        pilotDefaultsDir,
			body:       "tenants:\n  db-a:\n    mysql_cpu: \"70\"\n",
			wantErrs:   false,
			wantNotice: true,
		},
		{
			// Shipped state after the platform rename (commit 2): defaults
			// new-spelled, tenant override still old-spelled — must resolve
			// via the alias, not fall into unknown-key.
			name:       "new-named defaults + old key override",
			dir:        newNameDefaultsDir,
			body:       "tenants:\n  db-a:\n    mysql_cpu: \"70\"\n",
			wantErrs:   false,
			wantNotice: true,
		},
		{
			// The critical tier derives its base through the alias: no
			// dangling-_critical error once the canonical base is in defaults.
			name:       "old-spelled _critical against new-named defaults",
			dir:        newNameDefaultsDir,
			body:       "tenants:\n  db-a:\n    mysql_cpu: \"70\"\n    mysql_cpu_critical: \"90\"\n",
			wantErrs:   false,
			wantNotice: true,
		},
		{
			// Exact-match contract (⛔ never prefix-match): a typo sharing the
			// prefix must keep failing unknown-key validation, with no notice.
			name:       "prefix typo still errors",
			dir:        newNameDefaultsDir,
			body:       "tenants:\n  db-a:\n    mysql_cpu_util: \"70\"\n",
			wantErrs:   true,
			wantNotice: false,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			errs, notices := validate(tt.dir(t), "db-a", tt.body)
			if tt.wantErrs != (len(errs) > 0) {
				t.Fatalf("errs = %v, wantErrs=%v", errs, tt.wantErrs)
			}
			if tt.wantNotice {
				assertDeprecationNotice(t, notices)
			} else if len(notices) != 0 {
				t.Errorf("expected no notices, got: %v", notices)
			}
		})
	}
}

// TestWrite_DeprecatedKeyBody_CommitsWithNotices proves the c2 core invariant
// end-to-end at the gitops layer: a body still using the OLD spelling writes
// successfully (never blocked) AND the success carries the migration signal.
func TestWrite_DeprecatedKeyBody_CommitsWithNotices(t *testing.T) {
	t.Parallel()
	dir := newNameDefaultsDir(t)
	initGitRepo(t, dir)
	w := NewWriter(dir, dir)

	body := "tenants:\n  db-a:\n    mysql_cpu: \"70\"\n"
	notices, err := w.Write(context.Background(), "db-a", "op@example.com", body)
	if err != nil {
		t.Fatalf("deprecated-but-aliased body must never block the write, got: %v", err)
	}
	assertDeprecationNotice(t, notices)
	if _, statErr := os.Stat(filepath.Join(dir, "db-a.yaml")); statErr != nil {
		t.Fatalf("write did not land on disk: %v", statErr)
	}
}

// TestWriteMerged_DeprecatedKeyPatch_ReturnsNotices covers the batch direct
// path's writer seam, including the idempotent no-op short-circuit: a merge
// that changes nothing still reports the body's deprecated spelling.
func TestWriteMerged_DeprecatedKeyPatch_ReturnsNotices(t *testing.T) {
	dir := newNameDefaultsDir(t)
	initGitRepo(t, dir)
	w := NewWriter(dir, dir)

	body := "tenants:\n  db-a:\n    mysql_cpu: \"70\"\n"
	merge := func([]byte) (string, error) { return body, nil }
	notices, err := w.WriteMerged(context.Background(), "db-a", "op@example.com", merge)
	if err != nil {
		t.Fatalf("WriteMerged: %v", err)
	}
	assertDeprecationNotice(t, notices)

	// Idempotent retry: byte-identical merge → no commit, but the notices
	// still surface (the file still carries the old spelling).
	notices, err = w.WriteMerged(context.Background(), "db-a", "op@example.com", merge)
	if err != nil {
		t.Fatalf("idempotent WriteMerged: %v", err)
	}
	assertDeprecationNotice(t, notices)
}

// initMainBranchRepo builds the PR-mode repo shape (WritePR anchors on a
// "main" base branch; push to the nonexistent origin fails and is swallowed).
func initMainBranchRepo(t *testing.T, dir string) {
	t.Helper()
	initGitRepo(t, dir)
	cmd := exec.Command("git", "-C", dir, "branch", "-M", "main")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Skipf("git branch -M main failed: %v\n%s", err, out)
	}
}

func TestWritePR_DeprecatedKeyBody_ResultCarriesNotices(t *testing.T) {
	t.Parallel()
	dir := newNameDefaultsDir(t)
	initMainBranchRepo(t, dir)
	w := NewWriter(dir, dir)

	res, err := w.WritePR(context.Background(), "db-a", "op@example.com",
		"tenants:\n  db-a:\n    mysql_cpu: \"70\"\n")
	if err != nil {
		t.Fatalf("WritePR: %v", err)
	}
	assertDeprecationNotice(t, res.Notices)
}

func TestWritePRBatch_DeprecatedKeyOps_AggregatesNotices(t *testing.T) {
	t.Parallel()
	dir := newNameDefaultsDir(t)
	initMainBranchRepo(t, dir)
	w := NewWriter(dir, dir)

	ops := []PRBatchOp{{
		TenantID: "db-a",
		Merge: func([]byte) (string, error) {
			return "tenants:\n  db-a:\n    mysql_cpu: \"70\"\n", nil
		},
	}}
	res, err := w.WritePRBatch(context.Background(), ops, "op@example.com")
	if err != nil {
		t.Fatalf("WritePRBatch: %v", err)
	}
	assertDeprecationNotice(t, res.Notices)
}

// TestWritePRBatch_AllNoOpDeprecatedOps_PreservesNotices (#1231 review F5):
// the all-no-op path must return the aggregated notices ALONGSIDE
// ErrNoChanges, matching WriteMerged's invariant ("a merge that changes
// nothing still reports the body's deprecated spelling" — see
// TestWriteMerged_DeprecatedKeyPatch_ReturnsNotices above). Before the fix
// the early `return nil, ErrNoChanges` evaporated notices that the loop had
// deliberately collected before its no-op short-circuit, so an idempotent
// batch retry silently lost the tenant's migration signal.
func TestWritePRBatch_AllNoOpDeprecatedOps_PreservesNotices(t *testing.T) {
	t.Parallel()
	dir := newNameDefaultsDir(t)
	const body = "tenants:\n  db-a:\n    mysql_cpu: \"70\"\n"
	if err := os.WriteFile(filepath.Join(dir, "db-a.yaml"), []byte(body), 0o644); err != nil {
		t.Fatalf("seed db-a.yaml: %v", err)
	}
	initMainBranchRepo(t, dir)
	w := NewWriter(dir, dir)

	// Identity merge: byte-identical to disk → every op is a no-op.
	ops := []PRBatchOp{{
		TenantID: "db-a",
		Merge:    func(existing []byte) (string, error) { return string(existing), nil },
	}}
	res, err := w.WritePRBatch(context.Background(), ops, "op@example.com")
	if !errors.Is(err, ErrNoChanges) {
		t.Fatalf("all-no-op batch: want ErrNoChanges, got: %v", err)
	}
	if res == nil {
		t.Fatal("all-no-op batch must still return a result carrying the notices, got nil")
	}
	assertDeprecationNotice(t, res.Notices)
}
