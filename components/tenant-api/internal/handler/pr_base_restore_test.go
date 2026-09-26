package handler

// #2070 handler mapping: when a PR-mode write pushes its branch but cannot
// return conf.d to the base branch, the answer must be a 500 — not the 200
// pending_review it used to be, not a 503 that invites a retry (the branch is
// already on origin; a retry cuts a second one), not a 400 and not no_changes.
//
// The fault is structural and seam-free: a pre-push git hook creates
// .git/index.lock and exits 0, so the push succeeds and both of the writer's
// return-to-base attempts then fail on the held index lock — the exact
// contention shape gitErr maps to ErrWriteOverloaded, which is what makes this
// a real check of the "not a 503" half.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
)

// repoWithLockingPrePush builds a git repo on main with the given committed
// files, an origin bare remote, and a pre-push hook that leaves
// .git/index.lock behind. It returns the repo path.
func repoWithLockingPrePush(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	remote := t.TempDir()
	hooks := t.TempDir()
	lockPath := filepath.Join(dir, ".git", "index.lock")
	hook := "#!/bin/sh\n: > '" + lockPath + "'\nexit 0\n"
	if err := os.WriteFile(filepath.Join(hooks, "pre-push"), []byte(hook), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Remove(lockPath) })
	run := func(args ...string) {
		t.Helper()
		if out, err := exec.Command("git", args...).CombinedOutput(); err != nil {
			t.Skipf("git %v: %v\n%s", args, err, out)
		}
	}
	run("init", "--bare", remote)
	for _, args := range [][]string{
		{"init"}, {"config", "user.email", "t@t.com"}, {"config", "user.name", "T"},
		// Local hooksPath beats any global one, so the hook runs wherever the test does.
		{"config", "core.hooksPath", hooks},
		{"add", "-A"}, {"commit", "--allow-empty", "-m", "seed"}, {"branch", "-M", "main"},
		{"remote", "add", "origin", remote},
	} {
		run(append([]string{"-C", dir}, args...)...)
	}
	return dir
}

// assertBaseRestore500 checks the response is the generic 500 carrying the
// branch name, and that the tree really was left off base (so the 500 is this
// failure and not some other one).
func assertBaseRestore500(t *testing.T, rec *httptest.ResponseRecorder, dir, prefix, branchPrefix string) {
	t.Helper()
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500; body: %s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("Retry-After") != "" {
		t.Errorf("Retry-After = %q on a failed restore — the pushed write must not be retried", rec.Header().Get("Retry-After"))
	}
	body := rec.Body.String()
	for _, want := range []string{prefix, branchPrefix, "pushed to origin: true"} {
		if !strings.Contains(body, want) {
			t.Errorf("body missing %q: %s", want, body)
		}
	}
	head, err := exec.Command("git", "-C", dir, "rev-parse", "--abbrev-ref", "HEAD").Output()
	if err != nil {
		t.Fatalf("rev-parse HEAD: %v", err)
	}
	if !strings.HasPrefix(strings.TrimSpace(string(head)), branchPrefix) {
		t.Errorf("HEAD = %q, want the stranded %s* branch — the fault did not fire as designed", head, branchPrefix)
	}
}

func TestPutTenant_PRMode_BaseRestoreFailureIs500(t *testing.T) {
	t.Parallel()
	dir := repoWithLockingPrePush(t, map[string]string{
		"db-a.yaml": "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n",
	})
	prCreated := false
	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, head string, labels []string) (*platform.PRInfo, error) {
			prCreated = true
			return &platform.PRInfo{Number: 1, State: "open"}, nil
		},
	}
	rbacMgr := adminRBAC(t)
	h := PutTenant(&Deps{Writer: newTestWriter(dir), WriteMode: WriteModePR, PRClient: mockClient, PRTracker: &mockPlatformTracker{}, RBAC: rbacMgr})
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a",
		bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n"))
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	rec := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(rec, req)

	assertBaseRestore500(t, rec, dir, "PR write failed", "tenant-api/db-a/")
	if prCreated {
		t.Error("a PR/MR was opened although the write failed")
	}
}

func TestBatchTenants_PRMode_BaseRestoreFailureIs500(t *testing.T) {
	t.Parallel()
	dir := repoWithLockingPrePush(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_threads_running: 90\n",
		"db-a.yaml":      existingTenantYAML,
	})
	rbacMgr := adminRBAC(t)
	prCreated := false
	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, head string, labels []string) (*platform.PRInfo, error) {
			prCreated = true
			return &platform.PRInfo{Number: 7, State: "open"}, nil
		},
	}
	h := BatchTenants(&Deps{
		Writer: newTestWriter(dir), ConfigDir: dir, RBAC: rbacMgr,
		WriteMode: WriteModePR, PRClient: mockClient, PRTracker: &mockPlatformTracker{},
	})
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch",
		bytes.NewBufferString(`{"operations":[{"tenant_id":"db-a","patch":{"_silent_mode":"warning"}}]}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	rec := httptest.NewRecorder()
	rbacMgr.Middleware(rbac.PermRead, nil)(h).ServeHTTP(rec, req)

	assertBaseRestore500(t, rec, dir, "PR/MR batch write failed", "tenant-api/batch/")
	var resp map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err == nil {
		if s, _ := resp["status"].(string); s == "pending_review" || s == "completed" {
			t.Errorf("status %q on a failed restore", s)
		}
	}
	if prCreated {
		t.Error("a PR/MR was opened although the write failed")
	}
}
