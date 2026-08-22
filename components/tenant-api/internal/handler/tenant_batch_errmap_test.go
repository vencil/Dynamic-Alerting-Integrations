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
//   - conflict: commitFileChange's parent check — a pre-commit hook advances
//     the branch ref behind the writer's back, so the commit it then creates
//     hangs off that external commit instead of the HEAD it recorded.

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
	// On-disk file unparseable → the merge fails and must NOT fall back to an
	// overwrite; the per-tenant message carries the merge failure.
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": "{{not yaml",
	})
	gw := newTestWriter(configDir)

	res := applyPatch(context.Background(), gw, configDir,
		BatchOperation{TenantID: "db-a", Patch: map[string]string{"_silent_mode": "warning"}},
		"op@example.com")
	if res.Status != "error" {
		t.Fatalf("status = %q, want error for an unparseable on-disk file", res.Status)
	}
	if !strings.Contains(res.Message, "merge tenant config for db-a") {
		t.Errorf("message = %q, want the merge failure naming the tenant", res.Message)
	}
	// The corrupt file must be untouched (no overwrite fallback).
	got, err := os.ReadFile(filepath.Join(configDir, "db-a.yaml"))
	if err != nil || string(got) != "{{not yaml" {
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

// installExternalCommitHook makes the NEXT index update advance the current
// branch by one commit behind the writer's back. That is the honest shape of
// the conflict commitFileChange looks for: something else committed between
// the HEAD it recorded and the commit it creates, so that commit's parent is
// no longer the recorded HEAD.
//
// Two details are load-bearing:
//   - post-index-change, not pre-commit. The hook has to land in the window
//     between currentHEAD() and `git commit`; `git add` writes the index and
//     opens exactly that window. Moving the ref from pre-commit instead is too
//     late — git has already resolved HEAD for the commit it is building and
//     aborts with "cannot lock ref 'HEAD'" rather than committing onto the new
//     tip, so the conflict arm is never reached.
//   - one-shot. `git commit` writes the index too and would re-fire the hook
//     mid-commit, hitting the same ref-lock abort; the marker file makes the
//     external commit happen exactly once, on the `git add`.
//
// commit-tree + update-ref (rather than `git commit`) leaves the index and
// working tree alone, so the writer's own staged change survives and still
// becomes a real commit afterwards. Needs git >= 2.22 for post-index-change.
func installExternalCommitHook(t *testing.T, dir string) {
	t.Helper()
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
}

func TestApplyPatch_ConflictMapsToRefreshMessage(t *testing.T) {
	t.Parallel()
	// Reach commitFileChange's ErrConflict arm deterministically: the writer
	// records HEAD, a pre-commit hook moves the branch ref, and the writer's
	// commit then hangs off that external commit → parent != recorded HEAD.
	const base = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	patch := map[string]string{"_silent_mode": "critical"}

	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir) // commit #1 (init)
	if err := os.WriteFile(filepath.Join(configDir, "db-a.yaml"), []byte(base), 0o644); err != nil {
		t.Fatal(err)
	}
	runGit(t, configDir, "add", "db-a.yaml")
	runGit(t, configDir, "commit", "-m", "base content") // commit #2 → HEAD~1 exists
	installExternalCommitHook(t, configDir)

	gw := newTestWriter(configDir)
	res := applyPatch(context.Background(), gw, configDir,
		BatchOperation{TenantID: "db-a", Patch: patch}, "op@example.com")

	if res.Status != "error" {
		t.Fatalf("status = %q, want error for the stale-HEAD conflict shape", res.Status)
	}
	if res.Message != "conflict: retry after refresh" {
		t.Errorf("message = %q, want the canonical conflict retry hint", res.Message)
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
		{"root is a sequence, not a mapping", "- a\n- b\n", "root is not a mapping"},
		{"no tenants mapping", "defaults:\n  mysql_threads_running: 80\n", "no `tenants:` mapping"},
		{"tenants is a scalar", "tenants: oops\n", "no `tenants:` mapping"},
	}
	for _, tc := range errCases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, err := mergePatchYAML([]byte(tc.existing), "db-a", patch)
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
		out, err := mergePatchYAML([]byte(existing), "db-a", patch)
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
