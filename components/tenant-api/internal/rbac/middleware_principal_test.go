package rbac

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// The middleware attaches a VerifiedPrincipal AND keeps
// RequestEmail/RequestGroups working.
func TestMiddleware_AttachesPrincipalAndKeepsLegacyAccessors(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{
		Groups: []GroupRule{
			{Name: "team-b", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		},
	})

	var gotEmail string
	var gotGroups []string
	var gotPrincipal *VerifiedPrincipal
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotEmail = RequestEmail(r)
		gotGroups = RequestGroups(r)
		gotPrincipal = RequestPrincipal(r)
		w.WriteHeader(http.StatusOK)
	})
	mw := ResolveIdentity(m.HeaderResolver())(m.Middleware(PermRead, nil)(inner))

	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "team-a, team-b")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}
	// Legacy accessors unchanged.
	if gotEmail != "test@example.com" {
		t.Errorf("RequestEmail = %q, want test@example.com", gotEmail)
	}
	if len(gotGroups) != 2 || gotGroups[0] != "team-a" || gotGroups[1] != "team-b" {
		t.Errorf("RequestGroups = %v, want [team-a team-b]", gotGroups)
	}
	// New accessor populated with matching provenance.
	if gotPrincipal == nil {
		t.Fatal("RequestPrincipal = nil, want a populated principal")
	}
	if gotPrincipal.Email != "test@example.com" || gotPrincipal.Source != SourceHumanHopB {
		t.Errorf("principal = %+v, want email=test@example.com source=%s", gotPrincipal, SourceHumanHopB)
	}
	if len(gotPrincipal.Groups) != 2 {
		t.Errorf("principal.Groups = %v, want 2 groups", gotPrincipal.Groups)
	}
}
