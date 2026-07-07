package rbac

// ADR-027 / LD-6 P2 — middleware integration for the identity-claims seam:
// a claim-configured Manager (SetClaimHeaders) must attach a principal whose
// Claims are populated, while the legacy accessors (RequestEmail /
// RequestGroups) stay byte-identical — the seam is additive, exactly like the
// PR-1b-i principal seam it extends.

import (
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

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
