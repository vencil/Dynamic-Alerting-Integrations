package handler

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// ADR-027 D2-B §2.4 probe carve-out. The kubelet cannot present a Bearer or
// oauth2-proxy header, and it probes the TCP plane. The /health, /ready and
// /metrics handlers therefore MUST answer an unauthenticated request without an
// auth rejection (200/503, never 401/403). They are registered OUTSIDE the rbac
// middleware group in main.go so a future enforce middleware inside that group
// cannot reach them; this test pins the handler-level half of that invariant
// (an unauthenticated call is never treated as an auth failure), so wrapping one
// of these in auth would fail CI.
//
// NOTE (§2.4 denominator): a TCP request with NO oauth2-proxy header 401s at the
// rbac Middleware BEFORE the audit runs (middleware.go — HeaderResolver empty
// email → 401 pre-Observe), so an anonymous probe never enters the machine-
// identity audit denominator. The audit only ever counts header-carrying
// requests. These probe routes don't even go through that middleware.
func TestProbeCarveOut_UnauthenticatedNeverAuthRejected(t *testing.T) {
	t.Parallel()

	deps := &Deps{ConfigDir: t.TempDir()} // no human socket → Ready does the plain stat path

	cases := []struct {
		name string
		h    http.HandlerFunc
	}{
		{"health", Health},
		{"ready", Ready(deps)},
		{"metrics", MetricsHandler},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			// A bare request: no X-Forwarded-Email, no Authorization Bearer.
			req := httptest.NewRequest("GET", "/"+tc.name, nil)
			rec := httptest.NewRecorder()
			tc.h(rec, req)
			if rec.Code == http.StatusUnauthorized || rec.Code == http.StatusForbidden {
				t.Errorf("%s answered an unauthenticated probe with %d — probe routes must never auth-reject (kubelet carries no credentials)", tc.name, rec.Code)
			}
		})
	}
}
