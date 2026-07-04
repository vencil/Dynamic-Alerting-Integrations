package handler

import (
	"net/http/httptest"
	"testing"
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
	if got := rateLimitCaller(req); got != "alice@example.com" {
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
	got := rateLimitCaller(req)
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
	if got := rateLimitCaller(req); got != "ip:10.0.0.5" {
		t.Errorf("rateLimitCaller = %q, want ip:10.0.0.5 (X-Forwarded-For ignored)", got)
	}
}
