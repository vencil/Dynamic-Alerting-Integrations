package main

// ADR-027 §5: the portal's simulate-preview widget is answered by tenant-api
// (POST only), with the code the widget keys on; the other methods on the
// same path still address a tenant named "simulate".

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
)

// adr-check: ADR-027/9.4b
func TestSimulateNotProvided(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read, write]\n")
	ops := caller{group: "ops"}

	w := f.do(t, ops, http.MethodPost, "/api/v1/tenants/simulate", "dGVuYW50czoge30K")
	var env struct {
		Code  string `json:"code"`
		Error string `json:"error"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &env)
	if w.Code != http.StatusNotImplemented || env.Code != string(errcode.SimulateNotProvided) {
		t.Fatalf("POST simulate: %d %s; want 501 %s", w.Code, w.Body.String(), errcode.SimulateNotProvided)
	}

	// A tenant named "simulate" is still a tenant: GET reaches GetTenant
	// (no such tenant here), not the simulate route's 405.
	if w := f.do(t, ops, http.MethodGet, "/api/v1/tenants/simulate", ""); w.Code != http.StatusNotFound {
		t.Errorf("GET /tenants/simulate: %d %s; want GetTenant's 404 for a tenant that does not exist", w.Code, w.Body.String())
	}

	// No identity, no answer: the route is behind authentication.
	anon := httptest.NewRecorder()
	f.router.ServeHTTP(anon, httptest.NewRequest(http.MethodPost, "/api/v1/tenants/simulate", nil))
	if anon.Code != http.StatusUnauthorized {
		t.Errorf("POST simulate without an identity: %d %s; want 401", anon.Code, anon.Body.String())
	}
}

// ADR-027 §5: every answer of the API router carries the security headers,
// the authentication refusal and a 404 included.
func TestAPISecurityHeaders(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n")
	anon := httptest.NewRecorder()
	f.router.ServeHTTP(anon, httptest.NewRequest(http.MethodGet, "/api/v1/me", nil))
	for _, w := range []*httptest.ResponseRecorder{
		f.do(t, caller{group: "ops"}, http.MethodGet, "/api/v1/me", ""),
		f.do(t, caller{group: "ops"}, http.MethodGet, "/api/v1/no-such-route", ""),
		anon,
	} {
		for name, want := range map[string]string{
			"X-Content-Type-Options":    "nosniff",
			"X-Frame-Options":           "DENY",
			"Content-Security-Policy":   "default-src 'none'; frame-ancestors 'none'",
			"Strict-Transport-Security": "max-age=31536000; includeSubDomains",
			"Referrer-Policy":           "strict-origin-when-cross-origin",
		} {
			if got := w.Header().Get(name); got != want {
				t.Errorf("status %d: %s = %q, want %q", w.Code, name, got, want)
			}
		}
	}
}
