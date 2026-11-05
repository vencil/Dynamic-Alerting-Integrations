package handler

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

// ADR-027: rateLimitCaller must NOT bucket on the client-supplied
// X-Real-IP / X-Forwarded-For headers (forgeable → bucket evasion/poisoning).
// The IP fallback uses the true TCP peer (r.RemoteAddr); middleware.RealIP is
// intentionally not mounted so RemoteAddr is not header-derived.

func TestRateLimitCaller_EmailIsPrimary(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("GET", "/x", nil)
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Real-IP", "9.9.9.9") // must be ignored
	req.RemoteAddr = "10.0.0.5:4321"
	if got := rateLimitCaller(resolved(req)); got != "alice@example.com" {
		t.Errorf("rateLimitCaller = %q, want email primary", got)
	}
}

func TestRateLimitCaller_IgnoresSpoofedXRealIP(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("GET", "/x", nil)
	// No X-Forwarded-Email → falls back to peer IP. A spoofed X-Real-IP must
	// NOT win over the true RemoteAddr.
	req.Header.Set("X-Real-IP", "9.9.9.9")
	req.RemoteAddr = "10.0.0.5:4321"
	got := rateLimitCaller(resolved(req))
	if got == "ip:9.9.9.9" {
		t.Fatalf("rateLimitCaller trusted spoofed X-Real-IP: %q", got)
	}
	if got != "ip:10.0.0.5" {
		t.Errorf("rateLimitCaller = %q, want ip:10.0.0.5 (true peer, port stripped)", got)
	}
}

func TestRateLimitCaller_IgnoresSpoofedXForwardedFor(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("GET", "/x", nil)
	req.Header.Set("X-Forwarded-For", "9.9.9.9")
	req.RemoteAddr = "10.0.0.5:4321"
	if got := rateLimitCaller(resolved(req)); got != "ip:10.0.0.5" {
		t.Errorf("rateLimitCaller = %q, want ip:10.0.0.5 (X-Forwarded-For ignored)", got)
	}
}

// resolved returns req as rbac.ResolveIdentity hands it on — the UDS router's
// first step, so the rate limiter and the request log see the principal the
// identity headers resolve to (ADR-027 §2: they never read the header).
func resolved(req *http.Request) *http.Request {
	out := req
	rbac.ResolveIdentity(rbac.HeaderResolver{})(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) { out = r })).
		ServeHTTP(httptest.NewRecorder(), req)
	return out
}
