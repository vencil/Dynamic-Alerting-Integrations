//go:build unix

package handler

// #2477: a tenant whose conf.d file is not a regular file (a FIFO here).
//
//   - The write gate (WriteScopeMeta / RequireOrgWrite) resolves it to the
//     unlabeled pair at once, the same fail-soft as any unreadable file.
//   - GET /tenants/{id}, PUT /tenants/{id} and PUT …/custom-alerts answer at
//     once with 409 TENANT_CONFIG_NOT_LOADABLE, config_error
//     not_regular_file, and a message naming the reason.
//   - The refusal starts no conf.d walk, so it cannot leave one blocked:
//     once the entry is moved out of conf.d's names, a write to another
//     tenant succeeds at once. (While the entry is still there, other
//     writes fail closed at the Writer's walk bound — the #2078 design,
//     pinned by writer_declared_elsewhere_fifo_unix_test.go.)
//
// Every call is bounded, so a regression fails the test instead of hanging
// it, and cleanup releases anything still waiting on the FIFO.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
)

const (
	nonRegularTenantID = "tenant-nonregular"
	regularTenantID    = "tenant-regular"
	// immediate is the bound for "answered at once": far below the Writer's
	// 5s conf.d walk bound, far above a handler's normal latency.
	immediate = time.Second
)

// mkfifoTenantFile puts a FIFO at <dir>/<id>.yaml and releases any reader
// still waiting on it at cleanup (open for write, then close → EOF). The
// returned func moves it to a name no conf.d reader picks up — what an
// operator's repair amounts to — keeping it releasable at cleanup.
func mkfifoTenantFile(t *testing.T, dir, tenantID string) (moveOut func()) {
	t.Helper()
	fifo := filepath.Join(dir, tenantID+".yaml")
	if err := syscall.Mkfifo(fifo, 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	t.Cleanup(func() {
		if f, err := os.OpenFile(fifo, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
			_ = f.Close()
		}
	})
	return func() {
		moved := filepath.Join(dir, tenantID+".moved-out")
		if err := os.Rename(fifo, moved); err != nil {
			t.Fatalf("move the FIFO out of conf.d's names: %v", err)
		}
		fifo = moved
	}
}

// withinBound runs fn and fails the test if it has not returned in time.
// It returns how long fn took.
func withinBound(t *testing.T, what string, fn func()) time.Duration {
	t.Helper()
	start := time.Now()
	done := make(chan struct{})
	go func() { fn(); close(done) }()
	select {
	case <-done:
		return time.Since(start)
	case <-time.After(5 * time.Second):
		t.Fatalf("%s did not return within 5s on a non-regular tenant file", what)
		return 0
	}
}

func TestWriteScopeMeta_NonRegularTenantFileResolvesUnlabeled(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, nil)
	mkfifoTenantFile(t, dir, nonRegularTenantID)

	var env, domain string
	withinBound(t, "WriteScopeMeta", func() {
		env, domain = WriteScopeMeta(dir)(nonRegularTenantID)
	})
	if env != "" || domain != "" {
		t.Errorf("WriteScopeMeta = (%q, %q), want the unlabeled pair", env, domain)
	}
}

func TestRequireOrgWrite_NonRegularTenantFileCompletes(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, nil)
	mkfifoTenantFile(t, dir, nonRegularTenantID)

	mgr := newRBACManager(t, `groups:
  - name: writers
    tenants: ["*"]
    permissions: [read, write]
`)
	d := &Deps{ConfigDir: dir, RBAC: mgr}
	allowed := false
	h := wrapWithRBACMiddleware(func(w http.ResponseWriter, r *http.Request) {
		allowed = RequireOrgWrite(w, r, d, nonRegularTenantID, rbac.PermWrite)
	}, mgr, rbac.PermWrite, TenantIDFromPath)

	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+nonRegularTenantID, "id", nonRegularTenantID, nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "writers")
	w := httptest.NewRecorder()
	withinBound(t, "RequireOrgWrite", func() { h.ServeHTTP(w, req) })
	if !allowed {
		t.Errorf("RequireOrgWrite denied an unrestricted writer: %d %s", w.Code, w.Body.String())
	}
}

// nonRegularFixture is a git-backed conf.d holding one regular tenant and one
// tenant whose file is a FIFO, served through the RBAC middleware (the writer
// needs the request's author email) in direct write mode.
type nonRegularFixture struct {
	dir     string
	d       *Deps
	mgr     *rbac.Manager
	moveOut func()
}

func newNonRegularFixture(t *testing.T) *nonRegularFixture {
	t.Helper()
	dir := setupConfigDir(t, map[string]string{
		regularTenantID + ".yaml": "tenants:\n  " + regularTenantID + ":\n    _silent_mode: \"warning\"\n",
	})
	initGitRepo(t, dir)
	moveOut := mkfifoTenantFile(t, dir, nonRegularTenantID)
	mgr := newRBACManager(t, `groups:
  - name: admins
    tenants: ["*"]
    permissions: [read, write, admin]
`)
	d := &Deps{
		Writer:    gitops.NewWriter(dir, dir),
		WriteMode: WriteModeDirect,
		ConfigDir: dir,
		RBAC:      mgr,
	}
	return &nonRegularFixture{dir: dir, d: d, mgr: mgr, moveOut: moveOut}
}

func (f *nonRegularFixture) serve(t *testing.T, h http.HandlerFunc, perm rbac.Permission,
	method, path, tenantID, body string) (*httptest.ResponseRecorder, time.Duration) {
	t.Helper()
	req := newRequestWithChiParam(method, path, "id", tenantID, bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	took := withinBound(t, method+" "+path, func() {
		wrapWithRBACMiddleware(h, f.mgr, perm, TenantIDFromPath).ServeHTTP(w, req)
	})
	return w, took
}

// assertNotRegularRefusal checks the immediate 409 that names the reason.
func assertNotRegularRefusal(t *testing.T, w *httptest.ResponseRecorder, took time.Duration) {
	t.Helper()
	if took >= immediate {
		t.Errorf("answered after %v, want under %v", took, immediate)
	}
	if w.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409: %s", w.Code, w.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %v: %s", err, w.Body.String())
	}
	if body["code"] != CodeTenantConfigNotLoadable {
		t.Errorf("code = %v, want %s", body["code"], CodeTenantConfigNotLoadable)
	}
	if body["config_error"] != "not_regular_file" || body["tenant_id"] != nonRegularTenantID {
		t.Errorf("config_error/tenant_id = %v/%v, want not_regular_file/%s",
			body["config_error"], body["tenant_id"], nonRegularTenantID)
	}
	if msg, _ := body["error"].(string); !strings.Contains(msg, "not a regular file") {
		t.Errorf("error message does not name the reason: %q", msg)
	}
}

func putTenantBody(tenantID string) string {
	return "tenants:\n  " + tenantID + ":\n    _silent_mode: \"critical\"\n"
}

func TestPutTenant_NonRegularTenantFileRefusedAtOnce(t *testing.T) {
	t.Parallel()
	f := newNonRegularFixture(t)

	w, took := f.serve(t, PutTenant(f.d), rbac.PermWrite,
		"PUT", "/api/v1/tenants/"+nonRegularTenantID, nonRegularTenantID, putTenantBody(nonRegularTenantID))
	assertNotRegularRefusal(t, w, took)

	// The refusal left no conf.d walk blocked behind it: with the entry
	// moved out, a write to another tenant succeeds at once.
	f.moveOut()
	w, _ = f.serve(t, PutTenant(f.d), rbac.PermWrite,
		"PUT", "/api/v1/tenants/"+regularTenantID, regularTenantID, putTenantBody(regularTenantID))
	if w.Code != http.StatusOK {
		t.Fatalf("PUT to another tenant after the refusal = %d, want 200: %s", w.Code, w.Body.String())
	}
	assertFile(t, filepath.Join(f.dir, regularTenantID+".yaml"), putTenantBody(regularTenantID))
}

func TestPutTenantCustomAlerts_NonRegularTenantFileRefusedAtOnce(t *testing.T) {
	t.Parallel()
	f := newNonRegularFixture(t)

	w, took := f.serve(t, PutTenantCustomAlerts(f.d), rbac.PermWrite,
		"PUT", "/api/v1/tenants/"+nonRegularTenantID+"/custom-alerts", nonRegularTenantID,
		`{"custom_alerts":[],"base_hash":"0000000000000000"}`)
	assertNotRegularRefusal(t, w, took)

	f.moveOut()
	w, _ = f.serve(t, PutTenant(f.d), rbac.PermWrite,
		"PUT", "/api/v1/tenants/"+regularTenantID, regularTenantID, putTenantBody(regularTenantID))
	if w.Code != http.StatusOK {
		t.Fatalf("PUT to another tenant after the refusal = %d, want 200: %s", w.Code, w.Body.String())
	}
}

func TestGetTenant_NonRegularTenantFileRefusedAtOnce(t *testing.T) {
	t.Parallel()
	f := newNonRegularFixture(t)

	w, took := f.serve(t, GetTenant(f.d), rbac.PermRead,
		"GET", "/api/v1/tenants/"+nonRegularTenantID, nonRegularTenantID, "")
	assertNotRegularRefusal(t, w, took)

	w, _ = f.serve(t, GetTenant(f.d), rbac.PermRead,
		"GET", "/api/v1/tenants/"+regularTenantID, regularTenantID, "")
	if w.Code != http.StatusOK {
		t.Fatalf("GET of another tenant = %d, want 200: %s", w.Code, w.Body.String())
	}
}
