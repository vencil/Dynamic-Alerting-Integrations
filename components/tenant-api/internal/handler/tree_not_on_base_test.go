package handler

// #1723 handler mapping: a direct-commit write that finds the worktree on a
// PR feature branch (left there by a PR-mode write whose return to base
// failed) refuses to commit onto it. Nothing is written, so the answer is a
// retryable 503 — not the 200 it used to be (the commit landed on the
// branch), and not the 400 / 500 fallbacks of each handler's ladder.
//
// The stranded state is built with plain git: a branch in the PR namespace
// whose copies of db-a.yaml and _groups.yaml differ from main's, checked out
// (so a write that slipped through onto either tree would be visible).

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
)

const strandedBranch = platform.BranchPrefix + "db-z/20260101-000000"

// strandedRepo returns a conf.d repo whose HEAD is strandedBranch. With
// lockIndex, .git/index.lock is held as well, so the return to base fails.
func strandedRepo(t *testing.T, lockIndex bool) string {
	t.Helper()
	dir := setupConfigDir(t, map[string]string{
		"db-a.yaml":    "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n",
		"_groups.yaml": testGroupsYAML,
	})
	initGitRepo(t, dir)
	runGit(t, dir, "branch", "-M", "main")
	runGit(t, dir, "checkout", "-b", strandedBranch)
	for name, content := range map[string]string{
		"db-a.yaml":    "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n",
		"_groups.yaml": "groups:\n  other:\n    label: Other\n",
	} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	runGit(t, dir, "commit", "-am", "proposal")
	if lockIndex {
		lock := filepath.Join(dir, ".git", "index.lock")
		if err := os.WriteFile(lock, nil, 0o644); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = os.Remove(lock) })
	}
	return dir
}

func revParse(t *testing.T, dir, ref string) string {
	t.Helper()
	out, err := exec.Command("git", "-C", dir, "rev-parse", ref).Output()
	if err != nil {
		t.Fatalf("rev-parse %s: %v", ref, err)
	}
	return strings.TrimSpace(string(out))
}

// assertRetryable503 checks the 503 shape, that the body does not leak the
// stranded branch (it names another tenant), and that no ref moved.
func assertRetryable503(t *testing.T, rec *httptest.ResponseRecorder, wantCode string, dir string, refs map[string]string) {
	t.Helper()
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503; body: %s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("Retry-After") == "" {
		t.Error("503 without Retry-After")
	}
	var env struct {
		Code string `json:"code"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode body: %v; %s", err, rec.Body.String())
	}
	if env.Code != wantCode {
		t.Errorf("code = %q, want %q; body: %s", env.Code, wantCode, rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), "db-z") {
		t.Errorf("body leaks the stranded branch (another tenant's id): %s", rec.Body.String())
	}
	for ref, before := range refs {
		if got := revParse(t, dir, ref); got != before {
			t.Errorf("%s moved although the write was refused", ref)
		}
	}
}

func snapshotRefs(t *testing.T, dir string) map[string]string {
	t.Helper()
	return map[string]string{"main": revParse(t, dir, "main"), strandedBranch: revParse(t, dir, strandedBranch)}
}

func putTenantDirect(t *testing.T, dir string) *httptest.ResponseRecorder {
	t.Helper()
	h := wrapWithRBACMiddleware(
		PutTenant(&Deps{Writer: gitops.NewWriter(dir, dir), WriteMode: WriteModeDirect}),
		permissiveRBACManager(t), rbac.PermWrite, TenantIDFromPath)
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a",
		bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"disable\"\n"))
	setRequestIdentity(req, "op@example.com")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func putGroup(t *testing.T, dir string) *httptest.ResponseRecorder {
	t.Helper()
	h := PutGroup(&Deps{Groups: groups.NewManager(dir), Writer: gitops.NewWriter(dir, dir), RBAC: permissiveRBACManager(t)})
	req := newRequestWithChiParam("PUT", "/api/v1/groups/g1", "id", "g1",
		bytes.NewBufferString(`{"label":"G1","members":["db-a"]}`))
	req.Header.Set("Content-Type", "application/json")
	setRequestIdentity(req, "op@example.com")
	return executeWithRBAC(t, h, req)
}

// The return to base succeeds, but the request was prepared on the branch:
// 503 TREE_NOT_ON_BASE, and the tree is left on base for the retry, which
// succeeds.
func TestDirectWrite_StrandedOnPRBranch_ReturnsToBase503ThenRetry(t *testing.T) {
	t.Parallel()
	for name, do := range map[string]func(*testing.T, string) *httptest.ResponseRecorder{
		"PutTenant": putTenantDirect,
		"PutGroup":  putGroup,
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := strandedRepo(t, false)
			refs := snapshotRefs(t, dir)
			assertRetryable503(t, do(t, dir), CodeTreeNotOnBase, dir, refs)

			// The retry lands on base.
			if rec := do(t, dir); rec.Code != http.StatusOK {
				t.Fatalf("retry status = %d, want 200; body: %s", rec.Code, rec.Body.String())
			}
			if got := revParse(t, dir, "main~1"); got != refs["main"] {
				t.Errorf("retry did not add exactly one commit on main")
			}
		})
	}
}

// The return to base fails too (index.lock held): still a retryable 503, and
// nothing moves. The lock contention reads as WRITE_OVERLOADED, which every
// ladder checks first.
func TestDirectWrite_StrandedOnPRBranch_ReturnFails503(t *testing.T) {
	t.Parallel()
	for name, do := range map[string]func(*testing.T, string) *httptest.ResponseRecorder{
		"PutTenant": putTenantDirect,
		"PutGroup":  putGroup,
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := strandedRepo(t, true)
			refs := snapshotRefs(t, dir)
			assertRetryable503(t, do(t, dir), CodeWriteOverloaded, dir, refs)
		})
	}
}

// Direct-mode batch reports the refusal per op, with a fixed message.
func TestApplyPatch_StrandedOnPRBranch_PerOpCode(t *testing.T) {
	t.Parallel()
	dir := strandedRepo(t, false)
	refs := snapshotRefs(t, dir)
	res := applyPatch(context.Background(), gitops.NewWriter(dir, dir), dir,
		BatchOperation{TenantID: "db-a", Patch: map[string]string{"_silent_mode": "disable"}},
		"op@example.com")
	if res.Status != "error" || res.Code != CodeTreeNotOnBase {
		t.Fatalf("result = %+v, want status error, code %s", res, CodeTreeNotOnBase)
	}
	if strings.Contains(res.Message, "db-z") {
		t.Errorf("message leaks the stranded branch: %q", res.Message)
	}
	for ref, before := range refs {
		if got := revParse(t, dir, ref); got != before {
			t.Errorf("%s moved although the op was refused", ref)
		}
	}
}
