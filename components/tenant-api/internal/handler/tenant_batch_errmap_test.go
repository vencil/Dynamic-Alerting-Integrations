package handler

// Error-mapping coverage for applyPatch (ROI refactor R3, E3): the per-tenant
// BatchResult message must translate the writer's sentinel errors into the
// operator-actionable phrasing the portal shows —
//   gitops.ErrConflict        → "conflict: retry after refresh"
//   gitops.ErrWriteOverloaded → "write plane busy: retry shortly"
// plus the mergePatchYAML structural-error branches that were still uncovered.
//
// gitops.Writer is a concrete type (no fake-writer seam), so each sentinel is
// induced through the real machinery:
//   - overload: TA_WRITE_QUEUE_DEPTH=0 + a WriteMerged parked inside its
//     MergeFunc holds the single admission token; the next applyPatch sheds.
//   - conflict: a pre-commit hook advances the serving branch while the batch
//     commits in its staging worktree, so the compare-and-swap publish finds
//     the branch moved (#2900).

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// runGit runs a git command in dir, failing the test on error.
func runGit(t *testing.T, dir string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
}

func TestApplyPatch_MergeErrorNamesTenant(t *testing.T) {
	t.Parallel()
	// On-disk file the exporter rejects → the merge fails and must NOT fall
	// back to an overwrite. Since #2373 (review F1) the merge refuses it as a
	// file threshold-exporter rejects: code TENANT_CONFIG_NOT_LOADABLE,
	// config_error in the message. (#3064: the file is db-a's own; one that
	// is not YAML at all is git-managed, TENANT_GIT_MANAGED.)
	const broken = "tenants:\n  db-a:\n    mysql_connections: !!int zz\n"
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": broken,
	})
	initGitRepo(t, configDir) // a batch runs in a staging worktree (#2900)
	gw := newTestWriter(configDir)

	res := applyPatch(context.Background(), gw, configDir,
		BatchOperation{TenantID: "db-a", Patch: map[string]string{"_silent_mode": "warning"}},
		"op@example.com")
	if res.Status != "error" {
		t.Fatalf("status = %q, want error for an unparseable on-disk file", res.Status)
	}
	if res.TenantID != "db-a" || res.Code != "TENANT_CONFIG_NOT_LOADABLE" || !strings.Contains(res.Message, "config_error: invalid_config") {
		t.Errorf("result = %+v, want tenant db-a, code TENANT_CONFIG_NOT_LOADABLE, message naming config_error: invalid_config", res)
	}
	// The corrupt file must be untouched (no overwrite fallback).
	got, err := os.ReadFile(filepath.Join(configDir, "db-a.yaml"))
	if err != nil || string(got) != broken {
		t.Errorf("on-disk file changed after a failed merge (err=%v):\n%s", err, got)
	}
}

func TestApplyPatch_WriteOverloadedMapsToRetryMessage(t *testing.T) {
	// TA_WRITE_QUEUE_DEPTH=0 → only the single in-flight write is admitted;
	// anything else sheds immediately with ErrWriteOverloaded. (t.Setenv —
	// no t.Parallel.)
	t.Setenv("TA_WRITE_QUEUE_DEPTH", "0")
	configDir := setupConfigDir(t, nil)
	gw := newTestWriter(configDir) // reads the env at construction

	// Park a write inside its MergeFunc: once `started` closes, the admission
	// token is provably held and stays held until `release` closes.
	started := make(chan struct{})
	release := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		_, _ = gw.WriteMerged(context.Background(), "db-a", "op@example.com",
			func(existing []byte) (string, error) {
				close(started)
				<-release
				return "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n", nil
			})
	}()
	<-started

	res := applyPatch(context.Background(), gw, configDir,
		BatchOperation{TenantID: "db-b", Patch: map[string]string{"_silent_mode": "warning"}},
		"op@example.com")

	close(release)
	<-done

	if res.Status != "error" {
		t.Fatalf("status = %q, want error while the write plane is saturated", res.Status)
	}
	if res.Message != "write plane busy: retry shortly" {
		t.Errorf("message = %q, want the canonical overload retry hint", res.Message)
	}
}

// installBatchExternalCommitHook: a one-shot pre-commit hook that, while a
// batch commits in its staging worktree (#2900), moves the SERVING branch
// with an external commit — so the batch's compare-and-swap publish fails.
func installBatchExternalCommitHook(t *testing.T, dir string) {
	t.Helper()
	hook := filepath.Join(dir, ".git", "hooks", "pre-commit")
	const script = `#!/bin/sh
set -e
common="$(git rev-parse --git-common-dir)"
marker="$common/external-commit-done"
[ -e "$marker" ] && exit 0
: > "$marker"
ref=$(git --git-dir="$common" symbolic-ref HEAD)
tip=$(git rev-parse "$ref")
new=$(git commit-tree "$tip^{tree}" -p "$tip" -m "external commit")
git update-ref "$ref" "$new"
`
	if err := os.WriteFile(hook, []byte(script), 0o755); err != nil {
		t.Fatalf("install pre-commit hook: %v", err)
	}
}

func TestApplyPatch_ConflictMapsToRefreshMessage(t *testing.T) {
	t.Parallel()
	// Reach the batch's ErrConflict deterministically (#2900): while it
	// commits in its staging worktree, a hook moves the serving branch, so
	// the compare-and-swap publish finds it moved — and nothing is written.
	const base = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	patch := map[string]string{"_silent_mode": "critical"}

	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir) // commit #1 (init)
	if err := os.WriteFile(filepath.Join(configDir, "db-a.yaml"), []byte(base), 0o644); err != nil {
		t.Fatal(err)
	}
	runGit(t, configDir, "add", "db-a.yaml")
	runGit(t, configDir, "commit", "-m", "base content") // commit #2 → HEAD~1 exists
	installBatchExternalCommitHook(t, configDir)

	gw := newTestWriter(configDir)
	res := applyPatch(context.Background(), gw, configDir,
		BatchOperation{TenantID: "db-a", Patch: patch}, "op@example.com")

	if res.Status != "error" {
		t.Fatalf("status = %q, want error for the moved-branch conflict", res.Status)
	}
	if res.Message != "conflict: retry after refresh" {
		t.Errorf("message = %q, want the canonical conflict retry hint", res.Message)
	}
	// Nothing was written: the serving file is the base, the branch tip is
	// the external commit, which does not carry the patch.
	if got, _ := os.ReadFile(filepath.Join(configDir, "db-a.yaml")); string(got) != base {
		t.Errorf("serving db-a.yaml = %q, want the base", got)
	}
	out, err := exec.Command("git", "-C", configDir, "show", "HEAD:db-a.yaml").Output()
	if err != nil || strings.Contains(string(out), "critical") {
		t.Errorf("HEAD:db-a.yaml = %q (err %v), want no batch content", out, err)
	}
}

// TestMergePatchYAML_StructuralErrorBranches covers the remaining structural
// rejections (root not a mapping / no tenants mapping) and the add-new-section
// positive twin: all cases where the on-disk file exists but is not the shape
// a partial patch can merge into — falling back to an overwrite here would be
// the exact silent data loss #1097 forbids.
func TestMergePatchYAML_StructuralErrorBranches(t *testing.T) {
	t.Parallel()
	patch := map[string]string{"_silent_mode": "warning"}

	errCases := []struct {
		name     string
		existing string
		wantErr  string
	}{
		// #2373 (review F1): a shape threshold-exporter cannot load is refused
		// by checkPartialWriteBase before the structural checks below it.
		{"root is a sequence, not a mapping", "- a\n- b\n", "config_error: invalid_config"},
		{"no tenants mapping", "defaults:\n  mysql_threads_running: 80\n", "no `tenants:` mapping"},
		{"tenants is a scalar", "tenants: oops\n", "config_error: invalid_config"},
	}
	for _, tc := range errCases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, err := mergePatchYAML([]byte(tc.existing), "db-a", patch, nil, nil)
			if err == nil {
				t.Fatal("expected a structural error, got nil (would clobber)")
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("error = %q, want it to contain %q", err.Error(), tc.wantErr)
			}
		})
	}

	t.Run("tenant section absent → added, siblings preserved", func(t *testing.T) {
		t.Parallel()
		existing := "tenants:\n  other-db:\n    mysql_threads_running: \"40\"\n"
		out, err := mergePatchYAML([]byte(existing), "db-a", patch, nil, nil)
		if err != nil {
			t.Fatalf("mergePatchYAML: %v", err)
		}
		for _, want := range []string{"other-db", "mysql_threads_running", "db-a", "_silent_mode"} {
			if !strings.Contains(out, want) {
				t.Errorf("merged doc missing %q:\n%s", want, out)
			}
		}
	})
}
