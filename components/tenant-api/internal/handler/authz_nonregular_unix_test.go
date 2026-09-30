//go:build unix

package handler

// #2477: the write gate reads the tenant's file on disk (WriteScopeMeta) to
// resolve its environment/domain. When that path is not a regular file — a
// FIFO here — the read must return at once and resolve to the unlabeled pair,
// the same fail-soft as any other unreadable file, instead of never
// completing. Every test bounds its call so a regression fails, not hangs.

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/rbac"
)

const nonRegularTenantID = "tenant-nonregular"

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

// withinBound runs fn and fails the test if it has not returned in time.
func withinBound(t *testing.T, what string, fn func()) {
	t.Helper()
	done := make(chan struct{})
	go func() { fn(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatalf("%s did not return within 5s on a non-regular tenant file", what)
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
