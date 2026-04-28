package handler

// ============================================================
// Tests for v2.8.0 Phase B Track C (B-6) PR-1 middleware:
//   * RateLimit
//   * RequestIDResponse
//   * RateLimitConfigFromEnv
// ============================================================

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/go-chi/chi/v5/middleware"
)

// ─────────────────────────────────────────────────────────────────
// RequestIDResponse tests
// ─────────────────────────────────────────────────────────────────

func TestRequestIDResponse_EchoesIDFromContext(t *testing.T) {
	// Compose: chi.RequestID populates the context, our
	// RequestIDResponse copies it to the response header.
	handler := middleware.RequestID(RequestIDResponse(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		})))

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	got := rec.Header().Get("X-Request-ID")
	if got == "" {
		t.Fatal("X-Request-ID response header missing; RequestIDResponse did not echo")
	}
}

func TestRequestIDResponse_PreservesIncomingHeader(t *testing.T) {
	// chi.RequestID prefers an existing X-Request-ID header on
	// the request — RequestIDResponse should echo that same
	// value (so a caller's correlation ID round-trips).
	handler := middleware.RequestID(RequestIDResponse(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		})))

	const incoming = "client-supplied-correlation-id"
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-Request-ID", incoming)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	got := rec.Header().Get("X-Request-ID")
	if got != incoming {
		t.Errorf("X-Request-ID = %q, want %q (caller's incoming ID must round-trip)", got, incoming)
	}
}

func TestRequestIDResponse_NoOpWhenContextEmpty(t *testing.T) {
	// Defensive: if RequestIDResponse runs without chi.RequestID
	// upstream (misconfigured chain), it should not crash and
	// should not set a header.
	handler := RequestIDResponse(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		}))

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	got := rec.Header().Get("X-Request-ID")
	if got != "" {
		t.Errorf("X-Request-ID = %q on empty context, want empty (no defensive crash, no random ID)", got)
	}
}

// ─────────────────────────────────────────────────────────────────
// RateLimit tests
// ─────────────────────────────────────────────────────────────────

func TestRateLimit_AllowsUnderCap(t *testing.T) {
	cfg := RateLimitConfig{RequestsPerMinute: 3}
	handler := RateLimit(cfg)(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		}))

	for i := 0; i < 3; i++ {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
		req.Header.Set("X-Forwarded-Email", "alice@example.com")
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Errorf("request %d: expected 200, got %d", i+1, rec.Code)
		}
	}
}

func TestRateLimit_BlocksAtCap(t *testing.T) {
	cfg := RateLimitConfig{RequestsPerMinute: 2}
	handler := RateLimit(cfg)(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		}))

	// First two should pass.
	for i := 0; i < 2; i++ {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
		req.Header.Set("X-Forwarded-Email", "alice@example.com")
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("request %d should pass; got %d", i+1, rec.Code)
		}
	}

	// Third must be 429.
	req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusTooManyRequests {
		t.Errorf("3rd request should return 429, got %d", rec.Code)
	}

	// Response shape: JSON with error / code / retry_after_s.
	var body map[string]any
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("response body not JSON: %v", err)
	}
	if body["code"] != "RATE_LIMITED" {
		t.Errorf("code = %v, want RATE_LIMITED", body["code"])
	}
	if _, ok := body["retry_after_s"]; !ok {
		t.Error("retry_after_s field missing")
	}
	if body["error"] == "" || body["error"] == nil {
		t.Error("error message missing")
	}

	// Retry-After header set, integer >= 1
	if rec.Header().Get("Retry-After") == "" {
		t.Error("Retry-After header missing")
	}

	// Content-Type properly set so clients parse correctly.
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
}

func TestRateLimit_PerCallerIsolation(t *testing.T) {
	// Alice's bucket overflowing must NOT affect Bob's bucket.
	cfg := RateLimitConfig{RequestsPerMinute: 1}
	handler := RateLimit(cfg)(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		}))

	// Alice exhausts her budget.
	reqA := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	reqA.Header.Set("X-Forwarded-Email", "alice@example.com")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, reqA)
	if rec.Code != http.StatusOK {
		t.Fatalf("alice 1st request: got %d", rec.Code)
	}

	// Alice's 2nd is blocked.
	reqA2 := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	reqA2.Header.Set("X-Forwarded-Email", "alice@example.com")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, reqA2)
	if rec.Code != http.StatusTooManyRequests {
		t.Errorf("alice 2nd should be 429, got %d", rec.Code)
	}

	// Bob's first request should still pass — buckets are
	// independent.
	reqB := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	reqB.Header.Set("X-Forwarded-Email", "bob@example.com")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, reqB)
	if rec.Code != http.StatusOK {
		t.Errorf("bob's 1st request should pass independent of alice; got %d", rec.Code)
	}
}

func TestRateLimit_SkipPathsExempt(t *testing.T) {
	// `/health` and `/metrics` are exempt by default; even at
	// cap=1 they should return 200 indefinitely.
	cfg := DefaultRateLimit()
	cfg.RequestsPerMinute = 1
	handler := RateLimit(cfg)(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		}))

	for i := 0; i < 5; i++ {
		req := httptest.NewRequest(http.MethodGet, "/health", nil)
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Errorf("/health request %d: got %d, want 200 (skip path exempt)", i+1, rec.Code)
		}
	}
}

func TestRateLimit_DisabledWhenZero(t *testing.T) {
	// requestsPerMinute=0 → middleware degrades to a no-op pass-
	// through. The same identity can call any number of times.
	cfg := RateLimitConfig{RequestsPerMinute: 0}
	handler := RateLimit(cfg)(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		}))

	for i := 0; i < 200; i++ {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
		req.Header.Set("X-Forwarded-Email", "burst@example.com")
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("disabled limiter blocked request %d (got %d)", i+1, rec.Code)
		}
	}
}

func TestRateLimit_FallbackToIPWhenNoEmail(t *testing.T) {
	// No X-Forwarded-Email → bucket by X-Real-IP.
	cfg := RateLimitConfig{RequestsPerMinute: 1}
	handler := RateLimit(cfg)(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		}))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	req.Header.Set("X-Real-IP", "10.0.0.1")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("first IP request: got %d", rec.Code)
	}

	req2 := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	req2.Header.Set("X-Real-IP", "10.0.0.1")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req2)
	if rec.Code != http.StatusTooManyRequests {
		t.Errorf("second IP request should be 429 (same IP, cap=1); got %d", rec.Code)
	}

	// Different IP → fresh bucket.
	req3 := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	req3.Header.Set("X-Real-IP", "10.0.0.2")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req3)
	if rec.Code != http.StatusOK {
		t.Errorf("different IP should pass independently; got %d", rec.Code)
	}
}

func TestRateLimit_SlidingWindowEviction(t *testing.T) {
	// Use the lower-level allow() to test the sliding window
	// without sleeping — pass our own clock values. This locks
	// the eviction semantics: a timestamp older than 60s falls
	// out of the bucket.
	cfg := RateLimitConfig{RequestsPerMinute: 2}
	limiter := newRateLimiter(cfg)

	t0 := time.Date(2026, 4, 28, 10, 0, 0, 0, time.UTC)
	caller := "alice@example.com"

	// Two hits at t0 → both allowed (cap=2).
	if ok, _ := limiter.allow(caller, t0); !ok {
		t.Fatal("hit 1 at t0 should be allowed")
	}
	if ok, _ := limiter.allow(caller, t0.Add(1*time.Second)); !ok {
		t.Fatal("hit 2 at t0+1s should be allowed")
	}
	// Third at t0+30s → blocked (still inside 60s window).
	if ok, retry := limiter.allow(caller, t0.Add(30*time.Second)); ok {
		t.Error("hit 3 at t0+30s should be blocked")
	} else if retry < 30 {
		// retry should be ~30s (60s window - 30s elapsed = 30s)
		t.Errorf("retry_after = %d, want ~30s", retry)
	}
	// At t0+61s → first timestamp evicted, room for one more.
	if ok, _ := limiter.allow(caller, t0.Add(61*time.Second)); !ok {
		t.Error("hit at t0+61s should be allowed (oldest timestamp evicted)")
	}
}

// ─────────────────────────────────────────────────────────────────
// RateLimitConfigFromEnv tests
// ─────────────────────────────────────────────────────────────────

func TestRateLimitConfigFromEnv_Default(t *testing.T) {
	cfg := RateLimitConfigFromEnv("")
	if cfg.RequestsPerMinute != 100 {
		t.Errorf("empty env: RequestsPerMinute = %d, want 100", cfg.RequestsPerMinute)
	}
	if !cfg.SkipPaths["/health"] {
		t.Error("empty env: /health should be in skip paths")
	}
}

func TestRateLimitConfigFromEnv_PositiveOverride(t *testing.T) {
	cfg := RateLimitConfigFromEnv("250")
	if cfg.RequestsPerMinute != 250 {
		t.Errorf("env=250: RequestsPerMinute = %d, want 250", cfg.RequestsPerMinute)
	}
}

func TestRateLimitConfigFromEnv_Zero(t *testing.T) {
	cfg := RateLimitConfigFromEnv("0")
	if cfg.RequestsPerMinute != 0 {
		t.Errorf("env=0: RequestsPerMinute = %d, want 0 (disabled)", cfg.RequestsPerMinute)
	}
}

func TestRateLimitConfigFromEnv_Malformed(t *testing.T) {
	// Garbage → fall back to default.
	cfg := RateLimitConfigFromEnv("nonsense")
	if cfg.RequestsPerMinute != 100 {
		t.Errorf("malformed env: RequestsPerMinute = %d, want 100 (fallback)", cfg.RequestsPerMinute)
	}
}

func TestRateLimitConfigFromEnv_Negative(t *testing.T) {
	// Negative → fall back to default.
	cfg := RateLimitConfigFromEnv("-1")
	if cfg.RequestsPerMinute != 100 {
		t.Errorf("negative env: RequestsPerMinute = %d, want 100 (fallback)", cfg.RequestsPerMinute)
	}
}
