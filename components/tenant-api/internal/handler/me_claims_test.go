package handler

// ADR-027 / LD-6 P2 — /api/v1/me claims exposure + principal-first drift fix.
//
//	(a) claim-configured chain → response carries the claims;
//	(b) no claims → the JSON body must not contain a "claims" key at all
//	    (byte-level: omitempty keeps zero-config deployments byte-identical);
//	(c) mutation-proof: the handler must read identity off the request
//	    PRINCIPAL, not the legacy RequestEmail/RequestGroups context keys —
//	    proven by mutating the principal after the middleware attached it
//	    (the legacy keys keep the original values, so any drift back to
//	    RequestGroups would surface the stale identity).
//
// The pre-existing no-middleware tests (TestMeEmptyEmailDirect etc.) pin the
// p == nil fallback path.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

const meClaimsRBACYaml = `
groups:
  - name: team-readers
    tenants: ["*"]
    permissions: [read]
`

// (a) A claim-configured manager exposes the request's named claims in the
// /me response.
func TestMe_ClaimsExposed(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManagerWithClaims(t, meClaimsRBACYaml, map[string]string{
		"org":    "X-Auth-Request-Org",
		"region": "X-Auth-Request-Region",
	})
	handler := Me(&Deps{RBAC: rbacMgr})

	req := httptest.NewRequest("GET", "/api/v1/me", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "team-readers")
	req.Header.Set("X-Auth-Request-Org", "org-alpha")
	// X-Auth-Request-Region deliberately absent → no region claim.

	w := httptest.NewRecorder()
	wrapped := rbacMgr.Middleware(rbac.PermRead, nil)(handler)
	wrapped.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var resp MeResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal response: %v", err)
	}
	want := map[string]string{"org": "org-alpha"}
	if !reflect.DeepEqual(resp.Claims, want) {
		t.Errorf("Claims = %v, want %v", resp.Claims, want)
	}
	if !strings.Contains(w.Body.String(), `"claims"`) {
		t.Errorf("expected a claims key in the body; got: %s", w.Body.String())
	}
}

// (b) With no claims on the principal the body must not contain a "claims"
// key AT ALL — byte-level, so a zero-config deployment's JSON stays
// byte-identical to pre-P2. Pinned for both "configured but no header on the
// request" and "no claim config at all".
func TestMe_NoClaims_BodyOmitsClaimsKey(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name         string
		claimHeaders map[string]string
	}{
		{"no claim config", nil},
		{"configured axis but no header on request", map[string]string{"org": "X-Auth-Request-Org"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rbacMgr := newRBACManagerWithClaims(t, meClaimsRBACYaml, tc.claimHeaders)
			handler := Me(&Deps{RBAC: rbacMgr})

			req := httptest.NewRequest("GET", "/api/v1/me", nil)
			req.Header.Set("X-Forwarded-Email", "op@example.com")
			req.Header.Set("X-Forwarded-Groups", "team-readers")

			w := httptest.NewRecorder()
			wrapped := rbacMgr.Middleware(rbac.PermRead, nil)(handler)
			wrapped.ServeHTTP(w, req)

			if w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
			}
			if strings.Contains(w.Body.String(), `"claims"`) {
				t.Errorf("body must not contain a claims key when the principal has none; got: %s", w.Body.String())
			}
		})
	}
}

// (c) Mutation-proof drift fix: Me must read email/groups/claims off the
// request PRINCIPAL. An interposed handler mutates the principal AFTER the
// middleware attached it — the legacy context keys still hold the original
// header identity, so if Me ever drifted back to RequestEmail/RequestGroups
// the response would show the stale values and this test would fail.
func TestMe_ReadsPrincipalNotLegacyContext(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, `
groups:
  - name: legacy-group
    tenants: ["*"]
    permissions: [read]
`)
	handler := Me(&Deps{RBAC: rbacMgr})

	// Interposed between the middleware and Me: diverge the principal from
	// the legacy context keys.
	diverge := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := rbac.RequestPrincipal(r)
		if p == nil {
			t.Fatal("RequestPrincipal = nil; middleware did not attach a principal")
		}
		p.Email = "principal@example.com"
		p.Groups = []string{"principal-group"}
		p.Claims = map[string]string{"org": "org-alpha"}
		// Precondition: the legacy keys still carry the ORIGINAL header
		// identity — the divergence this test depends on is established.
		if got := rbac.RequestEmail(r); got != "legacy@example.com" {
			t.Fatalf("precondition failed: RequestEmail = %q, want legacy@example.com", got)
		}
		if got := rbac.RequestGroups(r); len(got) != 1 || got[0] != "legacy-group" {
			t.Fatalf("precondition failed: RequestGroups = %v, want [legacy-group]", got)
		}
		handler.ServeHTTP(w, r)
	})

	req := httptest.NewRequest("GET", "/api/v1/me", nil)
	req.Header.Set("X-Forwarded-Email", "legacy@example.com")
	req.Header.Set("X-Forwarded-Groups", "legacy-group")

	w := httptest.NewRecorder()
	wrapped := rbacMgr.Middleware(rbac.PermRead, nil)(diverge)
	wrapped.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var resp MeResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal response: %v", err)
	}
	if resp.Email != "principal@example.com" {
		t.Errorf("Email = %q, want the principal's (drift back to RequestEmail?)", resp.Email)
	}
	if resp.User != "principal" {
		t.Errorf("User = %q, want principal", resp.User)
	}
	if len(resp.Groups) != 1 || resp.Groups[0] != "principal-group" {
		t.Errorf("Groups = %v, want [principal-group] (drift back to RequestGroups?)", resp.Groups)
	}
	if !reflect.DeepEqual(resp.Claims, map[string]string{"org": "org-alpha"}) {
		t.Errorf("Claims = %v, want the principal's claims", resp.Claims)
	}
}
