package rbac

// Middleware HTTP wiring: status mapping (401/403/200), tenant-fn plumbing,
// header parsing into context accessors, the claims seam attachment (ADR-027
// / LD-6 P2), writeError, and one full P2+P3 end-to-end chain (trusted-hop
// headers → HeaderResolver claims → match authorization).
//
// Layering rule for the rbac test pyramid: evaluation semantics are
// exhaustively unit-tested in match_eval / metadata_scope / org_scope; this
// file smoke-tests the HTTP layer only; handler-level tests (outside this
// package) verify their own wiring, not rbac semantics — the apparent overlap
// across layers is deliberate, not redundant. The machine-identity audit
// side-channel lives in middleware_audit_test.go.

import (
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
)

func TestMiddleware_MissingEmail(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{})

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermRead, nil)(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	// No X-Forwarded-Email header
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("Middleware status = %d, want %d", w.Code, http.StatusUnauthorized)
	}
}

func TestMiddleware_OpenModeAllowsRead(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{}) // empty = open mode

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		email := RequestEmail(r)
		if email != "test@example.com" {
			t.Errorf("RequestEmail = %q, want %q", email, "test@example.com")
		}
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermRead, nil)(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("Middleware open-mode read status = %d, want %d", w.Code, http.StatusOK)
	}
}

func TestMiddleware_OpenModeDeniesWrite(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{}) // empty = open mode

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermWrite, nil)(inner)

	req := httptest.NewRequest("PUT", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Errorf("Middleware open-mode write status = %d, want %d", w.Code, http.StatusForbidden)
	}
}

func TestMiddleware_WithTenantIDFn(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{
		Groups: []GroupRule{
			{Name: "db-ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}},
		},
	})

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	tenantFn := func(r *http.Request) string { return "db-a" }
	mw := m.Middleware(PermWrite, tenantFn)(inner)

	req := httptest.NewRequest("PUT", "/api/v1/tenants/db-a", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "db-ops")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("Middleware with tenantFn status = %d, want %d", w.Code, http.StatusOK)
	}
}

func TestMiddleware_DeniedForWrongTenant(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{
		Groups: []GroupRule{
			{Name: "db-ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}},
		},
	})

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	tenantFn := func(r *http.Request) string { return "db-b" }
	mw := m.Middleware(PermWrite, tenantFn)(inner)

	req := httptest.NewRequest("PUT", "/api/v1/tenants/db-b", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "db-ops")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Errorf("Middleware wrong tenant status = %d, want %d", w.Code, http.StatusForbidden)
	}
}

func TestMiddleware_GroupsParsing(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{
		Groups: []GroupRule{
			{Name: "team-b", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		},
	})

	var gotGroups []string
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotGroups = RequestGroups(r)
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermRead, nil)(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "team-a, team-b, team-c")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", w.Code, http.StatusOK)
	}
	if len(gotGroups) != 3 {
		t.Fatalf("expected 3 groups, got %d: %v", len(gotGroups), gotGroups)
	}
	if gotGroups[0] != "team-a" || gotGroups[1] != "team-b" || gotGroups[2] != "team-c" {
		t.Errorf("unexpected groups: %v", gotGroups)
	}
}

func TestMiddleware_EmptyGroups(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{}) // open mode

	var gotGroups []string
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotGroups = RequestGroups(r)
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermRead, nil)(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	// No X-Forwarded-Groups header
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", w.Code, http.StatusOK)
	}
	// Empty header should result in no groups (all empty strings filtered out)
	if len(gotGroups) != 0 {
		t.Errorf("expected 0 groups from empty header, got %d: %v", len(gotGroups), gotGroups)
	}
}

// --- claims seam attachment (ADR-027 / LD-6 P2) ---
// A claim-configured Manager (SetClaimHeaders) must attach a principal whose
// Claims are populated, while the legacy accessors (RequestEmail /
// RequestGroups) stay byte-identical — the seam is additive, exactly like the
// PR-1b-i principal seam it extends.

func TestMiddleware_ClaimHeaders_PopulatesPrincipalClaims(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{
		Groups: []GroupRule{
			{Name: "team-b", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		},
	})
	m.SetClaimHeaders(map[string]string{"org": "X-Auth-Request-Org"})

	var gotEmail string
	var gotGroups []string
	var gotPrincipal *VerifiedPrincipal
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotEmail = RequestEmail(r)
		gotGroups = RequestGroups(r)
		gotPrincipal = RequestPrincipal(r)
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermRead, nil)(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "team-a, team-b")
	req.Header.Set("X-Auth-Request-Org", "org-alpha")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}
	// New: the principal carries the declared claim.
	if gotPrincipal == nil {
		t.Fatal("RequestPrincipal = nil, want a populated principal")
	}
	wantClaims := map[string]string{"org": "org-alpha"}
	if !reflect.DeepEqual(gotPrincipal.Claims, wantClaims) {
		t.Errorf("principal.Claims = %v, want %v", gotPrincipal.Claims, wantClaims)
	}
	// Unchanged: legacy accessors are untouched by the claims seam.
	if gotEmail != "test@example.com" {
		t.Errorf("RequestEmail = %q, want test@example.com", gotEmail)
	}
	if len(gotGroups) != 2 || gotGroups[0] != "team-a" || gotGroups[1] != "team-b" {
		t.Errorf("RequestGroups = %v, want [team-a team-b]", gotGroups)
	}
}

// Without SetClaimHeaders (the default), the attached principal's Claims is
// nil — the zero-config pre-P2 behavior, even when a claim-looking header is
// on the wire.
func TestMiddleware_NoClaimConfig_PrincipalClaimsNil(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{
		Groups: []GroupRule{
			{Name: "team-b", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		},
	})
	// No SetClaimHeaders call → claimHeaders is nil (default).

	var gotPrincipal *VerifiedPrincipal
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPrincipal = RequestPrincipal(r)
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermRead, nil)(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "team-b")
	req.Header.Set("X-Auth-Request-Org", "org-alpha") // undeclared → ignored
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}
	if gotPrincipal == nil {
		t.Fatal("RequestPrincipal = nil, want a populated principal")
	}
	if gotPrincipal.Claims != nil {
		t.Errorf("principal.Claims = %v, want nil with no claim config", gotPrincipal.Claims)
	}
}

// End-to-end through the real middleware: trusted-hop headers → HeaderResolver
// claims → match evaluation. The full P2+P3 chain, no synthetic principals.
func TestMiddleware_MatchRule_EndToEnd(t *testing.T) {
	t.Parallel()
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", matchLoadYAML)
	m, err := NewManager(rbacFile, map[string]string{"org": "X-Auth-Request-Org"})
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	mw := m.Middleware(PermWrite, func(*http.Request) string { return "any-tenant" })(inner)

	serve := func(orgHeader string) int {
		req := httptest.NewRequest("PUT", "/api/v1/tenants/any-tenant", nil)
		req.Header.Set("X-Forwarded-Email", "op@example.com")
		req.Header.Set("X-Forwarded-Groups", "operators")
		if orgHeader != "" {
			req.Header.Set("X-Auth-Request-Org", orgHeader)
		}
		w := httptest.NewRecorder()
		mw.ServeHTTP(w, req)
		return w.Code
	}

	if got := serve("ORG-A"); got != http.StatusOK {
		t.Errorf("matching claim header: status = %d, want 200", got)
	}
	if got := serve(""); got != http.StatusForbidden {
		t.Errorf("missing claim header: status = %d, want 403 (missing claim fail-closed)", got)
	}
	if got := serve("ORG-Z"); got != http.StatusForbidden {
		t.Errorf("mismatched claim value: status = %d, want 403", got)
	}
}

// --- Context helpers tests ---

func TestRequestEmail_NoContext(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("GET", "/", nil)
	email := RequestEmail(req)
	if email != "" {
		t.Errorf("expected empty email, got %q", email)
	}
}

func TestRequestGroups_NoContext(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("GET", "/", nil)
	groups := RequestGroups(req)
	if groups != nil {
		t.Errorf("expected nil groups, got %v", groups)
	}
}

// --- writeError tests ---

func TestWriteError(t *testing.T) {
	t.Parallel()
	w := httptest.NewRecorder()
	writeError(w, http.StatusForbidden, "access denied")

	if w.Code != http.StatusForbidden {
		t.Errorf("status = %d, want %d", w.Code, http.StatusForbidden)
	}
	if ct := w.Header().Get("Content-Type"); ct != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
}
