package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sort"
	"testing"

	"github.com/go-chi/chi/v5"
	chimw "github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/rbac"
)

// accessRouter wires GET /api/v1/tenants/{id}/access behind the same read
// middleware the real server uses, so these tests exercise the actual
// authorization decision (not a stub). RequestID mirrors the router-wide
// production chain (cmd/server/routes.go) so the envelope's request_id is
// populated — the exact-key-set pins below cover it.
func accessRouter(rbacMgr *rbac.Manager) chi.Router {
	r := chi.NewRouter()
	r.Use(chimw.RequestID)
	r.With(rbacMgr.Middleware(rbac.PermRead, TenantIDFromPath)).
		Get("/api/v1/tenants/{id}/access", CheckTenantAccess())
	return r
}

// assertExactEnvelopeKeys pins the FULL JSON key set of an error body — not
// subset-presence. The rbac middleware mirrors handler.ErrorResponse by value
// (depguard forbids rbac → handler) and the contract fuzz cannot observe
// middleware responses (wildcard-RBAC fixture), so a universally-emitted
// field added to the handler envelope (or request_id flipping to required)
// with the rbac mirror silently missing it must turn a test red HERE.
func assertExactEnvelopeKeys(t *testing.T, body []byte, want ...string) {
	t.Helper()
	var m map[string]json.RawMessage
	if err := json.Unmarshal(body, &m); err != nil {
		t.Fatalf("decode error body: %v", err)
	}
	got := make([]string, 0, len(m))
	for k := range m {
		got = append(got, k)
	}
	sort.Strings(got)
	sort.Strings(want)
	if !reflect.DeepEqual(got, want) {
		t.Errorf("envelope key set = %v, want exactly %v", got, want)
	}
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
	// Unified-envelope alignment pin (depguard forbids rbac → handler, so the
	// rbac middleware mirrors the envelope shape/codes BY VALUE — this test,
	// running the REAL middleware from the allowed direction, is what keeps
	// the copy honest): the 403 key set is pinned EXACTLY (see
	// assertExactEnvelopeKeys) and must carry the canonical error/code plus
	// the pre-envelope help/action operator guidance.
	assertExactEnvelopeKeys(t, w.Body.Bytes(), "error", "code", "request_id", "help", "action")
	var resp map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode 403 body: %v", err)
	}
	if resp["code"] != CodeForbidden {
		t.Errorf("403 code = %q, want %q (rbac middleware drifted from handler.ErrorResponse)", resp["code"], CodeForbidden)
	}
	if e, _ := resp["error"].(string); e == "" {
		t.Error("403 error message missing")
	}
	if h, _ := resp["help"].(string); h == "" {
		t.Error("403 help missing (pre-envelope field must be preserved)")
	}
	if a, _ := resp["action"].(string); a == "" {
		t.Error("403 action missing (pre-envelope field must be preserved)")
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
	// Envelope alignment pin for the middleware's 401 (see the 403 sibling
	// above): exact key set + canonical code + unchanged human-readable
	// message.
	assertExactEnvelopeKeys(t, w.Body.Bytes(), "error", "code", "request_id")
	var resp map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode 401 body: %v", err)
	}
	if resp["code"] != CodeUnauthorized {
		t.Errorf("401 code = %q, want %q (rbac middleware drifted from handler.ErrorResponse)", resp["code"], CodeUnauthorized)
	}
	if resp["error"] != "missing identity: X-Forwarded-Email header required" {
		t.Errorf("401 error = %q, want the pre-envelope message unchanged", resp["error"])
	}
}

func TestCheckTenantAccess_OpenMode(t *testing.T) {
	t.Parallel()
	// Path-less open mode (no --rbac configured): any authenticated user has
	// read. This is the case an /api/v1/me-based check gets WRONG (me returns
	// accessible_tenants [] in open mode), which is why the probe reuses the
	// read middleware. (ADR-027 MED-8: a *configured-but-empty* _rbac.yaml now
	// fails closed instead — see TestCheckTenantAccess_ConfiguredEmptyFailsClosed.)
	rbacMgr := newRBACManager(t, "") // path-less = open mode

	req := httptest.NewRequest("GET", "/api/v1/tenants/db-a/access", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	w := httptest.NewRecorder()
	accessRouter(rbacMgr).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("open-mode status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}
}

// ADR-027 MED-8: a configured --rbac path that parses to zero groups is a
// misconfiguration and must fail closed — the /access probe returns 403, not
// the legacy open-read 200.
func TestCheckTenantAccess_ConfiguredEmptyFailsClosed(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, "groups: []\n") // configured but empty → deny

	req := httptest.NewRequest("GET", "/api/v1/tenants/db-a/access", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	w := httptest.NewRecorder()
	accessRouter(rbacMgr).ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Fatalf("configured-empty status = %d, want %d (fail-closed), body: %s", w.Code, http.StatusForbidden, w.Body.String())
	}
}

func TestCheckTenantAccess_EmptyID_FailsClosed(t *testing.T) {
	t.Parallel()
	// The RBAC middleware authorizes an EMPTY id under open-mode / a "*" grant
	// (and chi routes /tenants//access to id=""), but "" is not a real tenant.
	// Reaching the handler with id="" must fail closed (400) — never allow:true
	// — mirroring GetTenant's ValidateTenantID. Invoke the handler directly with
	// id="" so the test is independent of router empty-segment matching.
	req := httptest.NewRequest("GET", "/api/v1/tenants//access", nil)
	rctx := chi.NewRouteContext()
	rctx.URLParams.Add("id", "")
	req = req.WithContext(context.WithValue(req.Context(), chi.RouteCtxKey, rctx))
	w := httptest.NewRecorder()
	CheckTenantAccess().ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("empty-id status = %d, want %d (must fail closed), body: %s",
			w.Code, http.StatusBadRequest, w.Body.String())
	}
	// Unified envelope (was a bare {"error": ...} map before the migration).
	// Direct handler invocation has no RequestID middleware → request_id is
	// legitimately absent; the exact set is error+code only.
	assertExactEnvelopeKeys(t, w.Body.Bytes(), "error", "code")
	var resp map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode 400 body: %v", err)
	}
	if resp["code"] != CodeBadRequest {
		t.Errorf("400 code = %q, want %q", resp["code"], CodeBadRequest)
	}
	if e, _ := resp["error"].(string); e == "" {
		t.Error("400 error message missing")
	}
}
