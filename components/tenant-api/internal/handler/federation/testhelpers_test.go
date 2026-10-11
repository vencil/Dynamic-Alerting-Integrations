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
	"sync"
	"testing"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/statestore"
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

// newDirState is a directory state store over dir: tests that wrote the old
// conf.d state files there find the records where they always were.
func newDirState(t *testing.T, dir string) statestore.Store {
	t.Helper()
	s, err := statestore.NewDir(dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	return s
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
// federationSubsetPath is where a directory state store over stateDir keeps
// tenantID's subset record (#2856: a record, no longer conf.d/_federation/).
func federationSubsetPath(stateDir, tenantID string) string {
	return filepath.Join(stateDir, statestore.SubsetKey(tenantID))
}

// recordingState wraps a Store and records every write's audit notes, so a
// test can see what a write recorded (the --force bypass reason rides there,
// #2856; it used to be a commit trailer).
type recordingState struct {
	statestore.Store
	mu  sync.Mutex
	log []string
}

func (r *recordingState) Mutate(ctx context.Context, key string, m statestore.Meta, t statestore.Transform) (bool, error) {
	r.mu.Lock()
	r.log = append(r.log, m.Notes...)
	r.mu.Unlock()
	return r.Store.Mutate(ctx, key, m, t)
}

func (r *recordingState) notes() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.log...)
}
