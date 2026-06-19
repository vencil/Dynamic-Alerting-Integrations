package handler

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/rbac"
)

// accessRouter wires GET /api/v1/tenants/{id}/access behind the same read
// middleware the real server uses, so these tests exercise the actual
// authorization decision (not a stub).
func accessRouter(rbacMgr *rbac.Manager) chi.Router {
	r := chi.NewRouter()
	r.With(rbacMgr.Middleware(rbac.PermRead, TenantIDFromPath)).
		Get("/api/v1/tenants/{id}/access", CheckTenantAccess())
	return r
}

func TestCheckTenantAccess_Allow(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, `groups:
  - name: admins
    tenants: ["*"]
    permissions: [read, write, admin]
`)

	req := httptest.NewRequest("GET", "/api/v1/tenants/db-a/access", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	accessRouter(rbacMgr).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}
	var resp AccessResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if !resp.Allow || resp.Tenant != "db-a" || resp.Permission != "read" {
		t.Errorf("resp = %+v, want {Allow:true Tenant:db-a Permission:read}", resp)
	}
}

func TestCheckTenantAccess_Forbidden(t *testing.T) {
	t.Parallel()
	// db-ops can read db-b-* only; asking about db-a must be denied.
	rbacMgr := newRBACManager(t, `groups:
  - name: db-ops
    tenants: ["db-b-*"]
    permissions: [read]
`)

	req := httptest.NewRequest("GET", "/api/v1/tenants/db-a/access", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "db-ops")
	w := httptest.NewRecorder()
	accessRouter(rbacMgr).ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Errorf("status = %d, want %d, body: %s", w.Code, http.StatusForbidden, w.Body.String())
	}
}

func TestCheckTenantAccess_Unauthorized(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, `groups:
  - name: admins
    tenants: ["*"]
    permissions: [read, write, admin]
`)

	// No identity headers → middleware returns 401 before the handler.
	req := httptest.NewRequest("GET", "/api/v1/tenants/db-a/access", nil)
	w := httptest.NewRecorder()
	accessRouter(rbacMgr).ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want %d", w.Code, http.StatusUnauthorized)
	}
}

func TestCheckTenantAccess_OpenMode(t *testing.T) {
	t.Parallel()
	// Empty groups = open mode: any authenticated user has read. This is the
	// case an /api/v1/me-based check gets WRONG (me returns accessible_tenants
	// [] in open mode), which is why the probe reuses the read middleware.
	rbacMgr := newRBACManager(t, "groups: []\n")

	req := httptest.NewRequest("GET", "/api/v1/tenants/db-a/access", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	w := httptest.NewRecorder()
	accessRouter(rbacMgr).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("open-mode status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}
}
