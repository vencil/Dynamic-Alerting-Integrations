package federation

// Test helpers duplicated from internal/handler/{handler,group}_test.go.
// They are kept here (rather than exported from internal/handler) so the
// handler package's production API stays unaffected by sub-package test
// scaffolding. If a helper diverges, that is acceptable — these are test
// fixtures, not production contracts.

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/testutil"
)

// initGitRepo initializes a git repo in the given directory with an initial commit.
func initGitRepo(t *testing.T, dir string) {
	t.Helper()
	cmds := [][]string{
		{"git", "init"},
		{"git", "config", "user.email", "test@test.com"},
		{"git", "config", "user.name", "Test"},
		{"git", "add", "."},
		{"git", "commit", "--allow-empty", "-m", "init"},
	}
	for _, args := range cmds {
		cmd := exec.Command(args[0], args[1:]...)
		cmd.Dir = dir
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git command %v failed: %v\n%s", args, err, out)
		}
	}
}

func newRequestWithChiParam(method, path, paramName, paramValue string, body *bytes.Buffer) *http.Request {
	if body == nil {
		body = bytes.NewBuffer(nil)
	}
	req := httptest.NewRequest(method, path, body)
	rctx := chi.NewRouteContext()
	rctx.URLParams.Add(paramName, paramValue)
	req = req.WithContext(context.WithValue(req.Context(), chi.RouteCtxKey, rctx))
	return req
}

func setupConfigDir(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for name, content := range files {
		testutil.WriteYAML(t, dir, name, content)
	}
	return dir
}

func newTestWriter(configDir string) *gitops.Writer {
	return gitops.NewWriter(configDir, "")
}

func newRBACManager(t *testing.T, yaml string) *rbac.Manager {
	t.Helper()
	if yaml == "" {
		mgr, err := rbac.NewManager("", nil)
		if err != nil {
			t.Fatalf("rbac.NewManager: %v", err)
		}
		return mgr
	}
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", yaml)
	mgr, err := rbac.NewManager(rbacFile, nil)
	if err != nil {
		t.Fatalf("rbac.NewManager: %v", err)
	}
	return mgr
}

func wrapWithRBACMiddleware(handler http.HandlerFunc, mgr *rbac.Manager, perm rbac.Permission, tenantIDFn func(*http.Request) string) http.Handler {
	return mgr.Middleware(perm, tenantIDFn)(handler)
}

// setRequestIdentity wraps a request through the RBAC middleware to set context identity.
// This simulates what oauth2-proxy + RBAC middleware would do in production.
func setRequestIdentity(req *http.Request, email string) *http.Request {
	req.Header.Set("X-Forwarded-Email", email)
	req.Header.Set("X-Forwarded-Groups", "platform-admins")
	return req
}

func executeWithRBAC(t *testing.T, handler http.HandlerFunc, req *http.Request) *httptest.ResponseRecorder {
	t.Helper()
	mgr := newRBACManager(t, "")
	wrapped := wrapWithRBACMiddleware(handler, mgr, rbac.PermRead, nil)
	w := httptest.NewRecorder()
	wrapped.ServeHTTP(w, req)
	return w
}

// federationSubsetPath is the path a NEW subset for tenantID gets — the
// default `<id>.yaml` spelling inside the _federation/ directory the
// production resolver scans. Tests plant fixtures through it so the
// directory and the default name are derived from the same confd helpers
// production uses, rather than hand-copied. It is a test helper since
// #1698: production no longer joins a fixed name on the read side — it
// resolves the tenant's actual file (confd.ResolveTenantFile) — so a file
// planted here is exactly what the reader picks for an addressable id.
// For an unaddressable id it is simply the join a predicate-less reader
// would have opened, which is what the sink-guard tests need.
func federationSubsetPath(configDir, tenantID string) string {
	return filepath.Join(confd.FederationSubsetDir(configDir), confd.DefaultTenantFileName(tenantID))
}
