//go:build unix

package handler

// #2477: a tenant whose conf.d file is not a regular file — a FIFO, a unix
// socket, or a symlink to a socket.
//
//   - The write gate (WriteScopeMeta / RequireOrgWrite) resolves it to the
//     unlabeled pair at once, the same fail-soft as any unreadable file.
//   - GET /tenants/{id}, PUT /tenants/{id} and PUT …/custom-alerts answer at
//     once (under `immediate`) with 409 TENANT_CONFIG_NOT_LOADABLE,
//     config_error not_regular_file, a message naming the reason and no
//     server path; the refused writes leave git HEAD and the entry as they
//     were.
//
// Every call is bounded, so a regression fails the test instead of hanging
// it, and cleanup releases anything still waiting on a FIFO.

import (
	"bytes"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
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

// nonRegularKinds are the entries each endpoint test runs against. Each
// builder puts a non-regular entry at <dir>/<id>.yaml and registers its own
// cleanup.
var nonRegularKinds = []struct {
	name string
	make func(t *testing.T, dir, tenantID string)
}{
	{"fifo", mkfifoTenantFile},
	{"socket", func(t *testing.T, dir, tenantID string) {
		mksocket(t, filepath.Join(dir, tenantID+".yaml"))
	}},
	{"symlink to socket", func(t *testing.T, dir, tenantID string) {
		target := filepath.Join(dir, tenantID+".sock")
		mksocket(t, target)
		if err := os.Symlink(target, filepath.Join(dir, tenantID+".yaml")); err != nil {
			t.Skipf("symlink: %v", err)
		}
	}},
}

// mkfifoTenantFile puts a FIFO at <dir>/<id>.yaml and releases any reader
// still waiting on it at cleanup (open for write, then close → EOF).
func mkfifoTenantFile(t *testing.T, dir, tenantID string) {
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
}

// mksocket binds a unix socket at path, closed at cleanup.
func mksocket(t *testing.T, path string) {
	t.Helper()
	l, err := net.Listen("unix", path)
	if err != nil {
		t.Skipf("unix socket: %v", err)
	}
	t.Cleanup(func() { _ = l.Close() })
}

// shortTempDir is a conf.d directory with a short absolute path: a unix
// socket path is capped near 108 bytes, which t.TempDir() under a long
// subtest name can exceed.
func shortTempDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "ta2477")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return dir
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
// tenant whose file is a non-regular entry, served through the RBAC
// middleware (the writer needs the request's author email) in direct write
// mode.
type nonRegularFixture struct {
	dir string
	d   *Deps
	mgr *rbac.Manager
}

func newNonRegularFixture(t *testing.T, makeEntry func(t *testing.T, dir, tenantID string)) *nonRegularFixture {
	t.Helper()
	dir := shortTempDir(t)
	if err := os.WriteFile(filepath.Join(dir, regularTenantID+".yaml"),
		[]byte("tenants:\n  "+regularTenantID+":\n    _silent_mode: \"warning\"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	initGitRepo(t, dir)
	makeEntry(t, dir, nonRegularTenantID)
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
	return &nonRegularFixture{dir: dir, d: d, mgr: mgr}
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

// state is what a refused write must leave untouched: git HEAD and the
// entry's own (lstat) type.
func (f *nonRegularFixture) state(t *testing.T) (head string, mode os.FileMode) {
	t.Helper()
	out, err := exec.Command("git", "-C", f.dir, "rev-parse", "HEAD").Output()
	if err != nil {
		t.Fatalf("git rev-parse HEAD: %v", err)
	}
	fi, err := os.Lstat(filepath.Join(f.dir, nonRegularTenantID+".yaml"))
	if err != nil {
		t.Fatalf("lstat the entry: %v", err)
	}
	return strings.TrimSpace(string(out)), fi.Mode().Type()
}

func (f *nonRegularFixture) assertNothingWritten(t *testing.T, head string, mode os.FileMode) {
	t.Helper()
	if h, m := f.state(t); h != head || m != mode {
		t.Errorf("refused write changed state: HEAD %s→%s, entry type %v→%v", head, h, mode, m)
	}
}

// assertNotRegularRefusal checks the immediate 409 that names the reason.
func (f *nonRegularFixture) assertNotRegularRefusal(t *testing.T, w *httptest.ResponseRecorder, took time.Duration) {
	t.Helper()
	if took >= immediate {
		t.Errorf("answered after %v, want under %v", took, immediate)
	}
	if w.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409: %s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), f.dir) {
		t.Errorf("error body carries the server path: %s", w.Body.String())
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

// assertOtherTenantWritable is a PUT to another tenant on the same Writer.
// It guards only that no blocked conf.d walk was left behind on the shared
// Writer — which the refusal's `immediate` bound already implies. It is not
// an independent check of the fix: on a tree without it this PUT is also 200
// once the refused request has returned.
func (f *nonRegularFixture) assertOtherTenantWritable(t *testing.T) {
	t.Helper()
	w, _ := f.serve(t, PutTenant(f.d), rbac.PermWrite,
		"PUT", "/api/v1/tenants/"+regularTenantID, regularTenantID, putTenantBody(regularTenantID))
	if w.Code != http.StatusOK {
		t.Fatalf("PUT to another tenant after the refusal = %d, want 200: %s", w.Code, w.Body.String())
	}
}

func TestPutTenant_NonRegularTenantFileRefusedAtOnce(t *testing.T) {
	t.Parallel()
	for _, kind := range nonRegularKinds {
		t.Run(kind.name, func(t *testing.T) {
			t.Parallel()
			f := newNonRegularFixture(t, kind.make)
			head, mode := f.state(t)

			w, took := f.serve(t, PutTenant(f.d), rbac.PermWrite,
				"PUT", "/api/v1/tenants/"+nonRegularTenantID, nonRegularTenantID, putTenantBody(nonRegularTenantID))
			f.assertNotRegularRefusal(t, w, took)
			f.assertNothingWritten(t, head, mode)
			if kind.name != "fifo" {
				// With a FIFO still in conf.d every write's walk fails closed
				// at its bound (#2078), so this runs for the other kinds only.
				f.assertOtherTenantWritable(t)
			}
		})
	}
}

func TestPutTenantCustomAlerts_NonRegularTenantFileRefusedAtOnce(t *testing.T) {
	t.Parallel()
	for _, kind := range nonRegularKinds {
		t.Run(kind.name, func(t *testing.T) {
			t.Parallel()
			f := newNonRegularFixture(t, kind.make)
			head, mode := f.state(t)

			w, took := f.serve(t, PutTenantCustomAlerts(f.d), rbac.PermWrite,
				"PUT", "/api/v1/tenants/"+nonRegularTenantID+"/custom-alerts", nonRegularTenantID,
				`{"custom_alerts":[],"base_hash":"0000000000000000"}`)
			f.assertNotRegularRefusal(t, w, took)
			f.assertNothingWritten(t, head, mode)
		})
	}
}

func TestGetTenant_NonRegularTenantFileRefusedAtOnce(t *testing.T) {
	t.Parallel()
	for _, kind := range nonRegularKinds {
		t.Run(kind.name, func(t *testing.T) {
			t.Parallel()
			f := newNonRegularFixture(t, kind.make)

			w, took := f.serve(t, GetTenant(f.d), rbac.PermRead,
				"GET", "/api/v1/tenants/"+nonRegularTenantID, nonRegularTenantID, "")
			f.assertNotRegularRefusal(t, w, took)
		})
	}
}
