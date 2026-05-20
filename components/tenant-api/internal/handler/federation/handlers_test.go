package federation

import (
	"bytes"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/rbac"
)

const fedAdminRBAC = `groups:
  - name: fed-admins
    tenants: ["*"]
    permissions: [read, write, admin]
`

const fedViewerRBAC = `groups:
  - name: fed-viewers
    tenants: ["*"]
    permissions: [read]
`

// newTestFederation builds a token.Manager backed by a freshly
// generated in-memory RSA key and a store under t.TempDir().
func newTestFederation(t *testing.T) *token.Manager {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	m, err := token.NewManagerForTest(key, filepath.Join(t.TempDir(), "fed-store.json"), time.Hour)
	if err != nil {
		t.Fatalf("NewManagerForTest: %v", err)
	}
	return m
}

// --- CreateFederationToken ---

func TestCreateFederationToken_Success(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, fedAdminRBAC)
	d := &handler.Deps{RBAC: rbacMgr, Federation: newTestFederation(t)}

	req := httptest.NewRequest("POST", "/api/v1/federation/tokens",
		bytes.NewBufferString(`{"tenant_id":"tenant-alpha","description":"grafana pull"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "ops@example.com")
	req.Header.Set("X-Forwarded-Groups", "fed-admins")

	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(CreateFederationToken(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201, body: %s", w.Code, w.Body.String())
	}
	var resp CreateFederationTokenResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Token == "" {
		t.Error("expected a non-empty signed token")
	}
	if resp.Record.TenantID != "tenant-alpha" {
		t.Errorf("record tenant_id = %q, want tenant-alpha", resp.Record.TenantID)
	}
	if resp.Record.IssuedBy != "ops@example.com" {
		t.Errorf("record issued_by = %q, want ops@example.com", resp.Record.IssuedBy)
	}
	if resp.Record.Description != "grafana pull" {
		t.Errorf("record description = %q", resp.Record.Description)
	}
	if !strings.HasPrefix(resp.Record.TokenID, "ftk_") {
		t.Errorf("record token_id = %q, want ftk_ prefix", resp.Record.TokenID)
	}
}

func TestCreateFederationToken_ForbiddenWithoutAdmin(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, fedViewerRBAC)
	d := &handler.Deps{RBAC: rbacMgr, Federation: newTestFederation(t)}

	req := httptest.NewRequest("POST", "/api/v1/federation/tokens",
		bytes.NewBufferString(`{"tenant_id":"tenant-alpha"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "viewer@example.com")
	req.Header.Set("X-Forwarded-Groups", "fed-viewers")

	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(CreateFederationToken(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403, body: %s", w.Code, w.Body.String())
	}
}

func TestCreateFederationToken_MissingTenantID(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, fedAdminRBAC)
	d := &handler.Deps{RBAC: rbacMgr, Federation: newTestFederation(t)}

	req := httptest.NewRequest("POST", "/api/v1/federation/tokens",
		bytes.NewBufferString(`{"description":"no tenant"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "ops@example.com")
	req.Header.Set("X-Forwarded-Groups", "fed-admins")

	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(CreateFederationToken(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", w.Code)
	}
}

func TestCreateFederationToken_InvalidJSON(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, fedAdminRBAC)
	d := &handler.Deps{RBAC: rbacMgr, Federation: newTestFederation(t)}

	req := httptest.NewRequest("POST", "/api/v1/federation/tokens",
		bytes.NewBufferString("not json"))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "ops@example.com")
	req.Header.Set("X-Forwarded-Groups", "fed-admins")

	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(CreateFederationToken(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", w.Code)
	}
}

// --- ListFederationTokens ---

func TestListFederationTokens_Success(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, fedAdminRBAC)
	fed := newTestFederation(t)
	if _, _, err := fed.Issue("tenant-a", "ops@example.com", "one"); err != nil {
		t.Fatalf("Issue: %v", err)
	}
	if _, _, err := fed.Issue("tenant-a", "ops@example.com", "two"); err != nil {
		t.Fatalf("Issue: %v", err)
	}
	if _, _, err := fed.Issue("tenant-b", "ops@example.com", "other"); err != nil {
		t.Fatalf("Issue: %v", err)
	}
	d := &handler.Deps{RBAC: rbacMgr, Federation: fed}

	req := httptest.NewRequest("GET", "/api/v1/federation/tokens?tenant_id=tenant-a", nil)
	req.Header.Set("X-Forwarded-Email", "ops@example.com")
	req.Header.Set("X-Forwarded-Groups", "fed-admins")

	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(ListFederationTokens(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	var resp []FederationTokenRecord
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(resp) != 2 {
		t.Fatalf("listed %d tokens for tenant-a, want 2", len(resp))
	}
	for _, rec := range resp {
		if rec.TenantID != "tenant-a" {
			t.Errorf("listing leaked a non-tenant-a record: %+v", rec)
		}
	}
}

func TestListFederationTokens_MissingTenantID(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, fedAdminRBAC)
	d := &handler.Deps{RBAC: rbacMgr, Federation: newTestFederation(t)}

	req := httptest.NewRequest("GET", "/api/v1/federation/tokens", nil)
	req.Header.Set("X-Forwarded-Email", "ops@example.com")
	req.Header.Set("X-Forwarded-Groups", "fed-admins")

	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(ListFederationTokens(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", w.Code)
	}
}

func TestListFederationTokens_ForbiddenWithoutAdmin(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, fedViewerRBAC)
	d := &handler.Deps{RBAC: rbacMgr, Federation: newTestFederation(t)}

	req := httptest.NewRequest("GET", "/api/v1/federation/tokens?tenant_id=tenant-a", nil)
	req.Header.Set("X-Forwarded-Email", "viewer@example.com")
	req.Header.Set("X-Forwarded-Groups", "fed-viewers")

	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(ListFederationTokens(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Errorf("status = %d, want 403", w.Code)
	}
}

// --- DeleteFederationToken ---

func TestDeleteFederationToken_Success(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, fedAdminRBAC)
	fed := newTestFederation(t)
	_, rec, err := fed.Issue("tenant-a", "ops@example.com", "")
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}
	d := &handler.Deps{RBAC: rbacMgr, Federation: fed}

	req := newRequestWithChiParam("DELETE", "/api/v1/federation/tokens/"+rec.TokenID, "id", rec.TokenID, nil)
	req.Header.Set("X-Forwarded-Email", "ops@example.com")
	req.Header.Set("X-Forwarded-Groups", "fed-admins")

	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(DeleteFederationToken(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	if _, ok, _ := fed.Get(rec.TokenID); ok {
		t.Error("token record should be gone after delete")
	}
}

func TestDeleteFederationToken_NotFound(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, fedAdminRBAC)
	d := &handler.Deps{RBAC: rbacMgr, Federation: newTestFederation(t)}

	req := newRequestWithChiParam("DELETE", "/api/v1/federation/tokens/ftk_missing", "id", "ftk_missing", nil)
	req.Header.Set("X-Forwarded-Email", "ops@example.com")
	req.Header.Set("X-Forwarded-Groups", "fed-admins")

	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(DeleteFederationToken(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("status = %d, want 404", w.Code)
	}
}

func TestDeleteFederationToken_ForbiddenWithoutAdmin(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, fedViewerRBAC)
	fed := newTestFederation(t)
	_, rec, err := fed.Issue("tenant-a", "ops@example.com", "")
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}
	d := &handler.Deps{RBAC: rbacMgr, Federation: fed}

	req := newRequestWithChiParam("DELETE", "/api/v1/federation/tokens/"+rec.TokenID, "id", rec.TokenID, nil)
	req.Header.Set("X-Forwarded-Email", "viewer@example.com")
	req.Header.Set("X-Forwarded-Groups", "fed-viewers")

	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(DeleteFederationToken(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Errorf("status = %d, want 403", w.Code)
	}
	if _, ok, _ := fed.Get(rec.TokenID); !ok {
		t.Error("token record should survive a forbidden delete")
	}
}

// --- tenant_id validation ---

func TestCreateFederationToken_RejectsInvalidTenantID(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, fedAdminRBAC)
	d := &handler.Deps{RBAC: rbacMgr, Federation: newTestFederation(t)}

	req := httptest.NewRequest("POST", "/api/v1/federation/tokens",
		bytes.NewBufferString(`{"tenant_id":"../escape"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "ops@example.com")
	req.Header.Set("X-Forwarded-Groups", "fed-admins")

	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(CreateFederationToken(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", w.Code)
	}
}

func TestListFederationTokens_RejectsInvalidTenantID(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, fedAdminRBAC)
	d := &handler.Deps{RBAC: rbacMgr, Federation: newTestFederation(t)}

	req := httptest.NewRequest("GET", "/api/v1/federation/tokens?tenant_id=../escape", nil)
	req.Header.Set("X-Forwarded-Email", "ops@example.com")
	req.Header.Set("X-Forwarded-Groups", "fed-admins")

	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(ListFederationTokens(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", w.Code)
	}
}

// --- mint rate limit ---

func TestCreateFederationToken_RateLimited(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, fedAdminRBAC)
	d := &handler.Deps{RBAC: rbacMgr, Federation: newTestFederation(t)}
	h := wrapWithRBACMiddleware(CreateFederationToken(d), rbacMgr, rbac.PermRead, nil)

	post := func() int {
		req := httptest.NewRequest("POST", "/api/v1/federation/tokens",
			bytes.NewBufferString(`{"tenant_id":"tenant-burst"}`))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Forwarded-Email", "ops@example.com")
		req.Header.Set("X-Forwarded-Groups", "fed-admins")
		w := httptest.NewRecorder()
		h.ServeHTTP(w, req)
		return w.Code
	}

	// federation.maxMintsPerWindow is 5 (unexported); the 6th mint in
	// the window must be rejected with 429.
	for i := 0; i < 5; i++ {
		if code := post(); code != http.StatusCreated {
			t.Fatalf("POST %d within rate limit: status %d, want 201", i, code)
		}
	}
	if code := post(); code != http.StatusTooManyRequests {
		t.Errorf("POST past rate limit: status %d, want 429", code)
	}
}
