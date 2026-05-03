package handler

// ============================================================
// Tests for v2.8.0 Phase B Track C (B-6) PR-1 middleware:
//   * RateLimit
//   * RequestIDResponse
//   * RateLimitConfigFromEnv
// ============================================================

import (
	"bytes"
	"encoding/json"
	"log/slog"
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
	handler := RateLimit(cfg, make(chan struct{}))(http.HandlerFunc(
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
	handler := RateLimit(cfg, make(chan struct{}))(http.HandlerFunc(
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
	handler := RateLimit(cfg, make(chan struct{}))(http.HandlerFunc(
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
	handler := RateLimit(cfg, make(chan struct{}))(http.HandlerFunc(
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
	handler := RateLimit(cfg, make(chan struct{}))(http.HandlerFunc(
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
	handler := RateLimit(cfg, make(chan struct{}))(http.HandlerFunc(
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
	cfg, malformed := RateLimitConfigFromEnv("")
	if cfg.RequestsPerMinute != 100 {
		t.Errorf("empty env: RequestsPerMinute = %d, want 100", cfg.RequestsPerMinute)
	}
	if malformed {
		t.Error("empty env should NOT be flagged malformed (it's the legitimate 'unset' case)")
	}
	if !cfg.SkipPaths["/health"] {
		t.Error("empty env: /health should be in skip paths")
	}
}

func TestRateLimitConfigFromEnv_PositiveOverride(t *testing.T) {
	cfg, malformed := RateLimitConfigFromEnv("250")
	if cfg.RequestsPerMinute != 250 {
		t.Errorf("env=250: RequestsPerMinute = %d, want 250", cfg.RequestsPerMinute)
	}
	if malformed {
		t.Error("explicit valid integer should NOT be flagged malformed")
	}
}

func TestRateLimitConfigFromEnv_Zero(t *testing.T) {
	cfg, malformed := RateLimitConfigFromEnv("0")
	if cfg.RequestsPerMinute != 0 {
		t.Errorf("env=0: RequestsPerMinute = %d, want 0 (disabled)", cfg.RequestsPerMinute)
	}
	if malformed {
		t.Error("explicit 0 (disable) should NOT be flagged malformed")
	}
}

func TestRateLimitConfigFromEnv_Malformed(t *testing.T) {
	// Garbage → fall back to default AND flag malformed so caller can warn.
	cfg, malformed := RateLimitConfigFromEnv("nonsense")
	if cfg.RequestsPerMinute != 100 {
		t.Errorf("malformed env: RequestsPerMinute = %d, want 100 (fallback)", cfg.RequestsPerMinute)
	}
	if !malformed {
		t.Error("'nonsense' must be flagged malformed so operators don't ship typo'd env vars silently")
	}
}

func TestRateLimitConfigFromEnv_Negative(t *testing.T) {
	// Negative → fall back to default AND flag malformed.
	cfg, malformed := RateLimitConfigFromEnv("-1")
	if cfg.RequestsPerMinute != 100 {
		t.Errorf("negative env: RequestsPerMinute = %d, want 100 (fallback)", cfg.RequestsPerMinute)
	}
	if !malformed {
		t.Error("negative number must be flagged malformed")
	}
}


// ─────────────────────────────────────────────────────────────────
// SlogRequestLogger tests (PR-10/11)
// ─────────────────────────────────────────────────────────────────

// TestSlogRequestLogger_EmitsStructuredLine verifies the middleware
// drives slog with the expected attribute keys. We capture slog
// output by swapping in a JSON handler over a bytes.Buffer for the
// duration of the test.
func TestSlogRequestLogger_EmitsStructuredLine(t *testing.T) {
	t.Helper()
	// Save + restore default logger.
	origLogger := slog.Default()
	defer slog.SetDefault(origLogger)

	var buf bytes.Buffer
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	// Wrap with chi RequestID + our logger so request_id is populated.
	handler := middleware.RequestID(SlogRequestLogger(inner))

	req := httptest.NewRequest("GET", "/api/v1/tenants", nil)
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("inner status = %d, want 200", w.Code)
	}

	// Parse the structured log line.
	var entry map[string]any
	if err := json.Unmarshal(buf.Bytes(), &entry); err != nil {
		t.Fatalf("log line not JSON: %v\n%s", err, buf.String())
	}
	if entry["msg"] != "request" {
		t.Errorf("msg = %q, want \"request\"", entry["msg"])
	}
	if entry["method"] != "GET" {
		t.Errorf("method = %q, want GET", entry["method"])
	}
	if entry["path"] != "/api/v1/tenants" {
		t.Errorf("path = %q", entry["path"])
	}
	if v, _ := entry["status"].(float64); v != 200 {
		t.Errorf("status = %v, want 200", entry["status"])
	}
	if entry["caller"] != "alice@example.com" {
		t.Errorf("caller = %q, want alice@example.com", entry["caller"])
	}
	if entry["request_id"] == "" || entry["request_id"] == nil {
		t.Errorf("request_id missing or empty: %v", entry["request_id"])
	}
}

// TestSlogRequestLogger_5xxLogsAtWarn ensures server errors get
// elevated to WARN so log aggregators can alert on level alone.
func TestSlogRequestLogger_5xxLogsAtWarn(t *testing.T) {
	origLogger := slog.Default()
	defer slog.SetDefault(origLogger)

	var buf bytes.Buffer
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	handler := middleware.RequestID(SlogRequestLogger(inner))

	req := httptest.NewRequest("GET", "/x", nil)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)

	var entry map[string]any
	if err := json.Unmarshal(buf.Bytes(), &entry); err != nil {
		t.Fatalf("log line not JSON: %v", err)
	}
	if entry["level"] != "WARN" {
		t.Errorf("5xx log level = %q, want WARN", entry["level"])
	}
}

// ─────────────────────────────────────────────────────────────────
// PR-11/11: rate-limiter polish (rejections counter, sweeper)
// ─────────────────────────────────────────────────────────────────

// TestRateLimit_RejectionsCounter verifies the package-level
// rejection counter increments once per blocked request and is
// readable via RateLimitMetrics().
func TestRateLimit_RejectionsCounter(t *testing.T) {
	cfg := RateLimitConfig{RequestsPerMinute: 1}
	stop := make(chan struct{})
	defer close(stop)
	mw := RateLimit(cfg, stop)

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	wrapped := mw(inner)

	// Snapshot baseline so the test is independent of any earlier
	// limiter activity in the same process.
	baselineRejections, _ := RateLimitMetrics()

	// First request passes; next 3 are rejected.
	for i := 0; i < 4; i++ {
		req := httptest.NewRequest("GET", "/x", nil)
		req.Header.Set("X-Forwarded-Email", "ratelimit-counter-test@example.com")
		w := httptest.NewRecorder()
		wrapped.ServeHTTP(w, req)
	}

	gotRejections, gotActive := RateLimitMetrics()
	delta := gotRejections - baselineRejections
	if delta != 3 {
		t.Errorf("rejections delta = %d, want 3", delta)
	}
	if gotActive < 1 {
		t.Errorf("active callers = %d, want >= 1", gotActive)
	}
}

// TestRateLimit_SweepEvictsExpiredBucket exercises the bucket
// sweeper directly: a caller's bucket is created via allow(),
// then the wall-clock advances past the rolling window, then
// sweep() with the future timestamp drops the now-empty bucket.
func TestRateLimit_SweepEvictsExpiredBucket(t *testing.T) {
	l := newRateLimiter(RateLimitConfig{RequestsPerMinute: 100})

	// Caller registers an old timestamp.
	now := time.Now()
	if ok, _ := l.allow("ghost@example.com", now); !ok {
		t.Fatal("first request should be allowed")
	}
	if got := l.activeCallers(); got != 1 {
		t.Fatalf("activeCallers = %d, want 1", got)
	}

	// Advance past the rolling window and sweep.
	l.sweep(now.Add(2 * time.Minute))

	if got := l.activeCallers(); got != 0 {
		t.Errorf("after sweep, activeCallers = %d, want 0", got)
	}
}

// TestRateLimit_SweepLoopExitsOnStop runs the sweep loop with a
// short interval, closes stopCh, and asserts the goroutine exits
// promptly. Guards against the sweeper outliving server shutdown.
func TestRateLimit_SweepLoopExitsOnStop(t *testing.T) {
	stop := make(chan struct{})
	l := newRateLimiterWithSweep(RateLimitConfig{RequestsPerMinute: 100}, stop)
	_ = l

	done := make(chan struct{})
	go func() {
		// In production, sweepLoop runs on the goroutine launched
		// inside newRateLimiterWithSweep — we re-launch a duplicate
		// here to assert the exit signal works on a goroutine WE own.
		l.sweepLoop(20*time.Millisecond, stop)
		close(done)
	}()

	close(stop)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Error("sweepLoop did not exit within 1s of close(stop)")
	}
}

// TestMetricsHandler_IncludesRateLimitMetrics asserts the new
// PR-11 metrics render in the /metrics text output with HELP +
// TYPE lines.
func TestMetricsHandler_IncludesRateLimitMetrics(t *testing.T) {
	w := httptest.NewRecorder()
	r := httptest.NewRequest("GET", "/metrics", nil)
	MetricsHandler(w, r)

	body := w.Body.String()
	for _, want := range []string{
		"# HELP tenant_api_rate_limit_rejections_total",
		"# TYPE tenant_api_rate_limit_rejections_total counter",
		"tenant_api_rate_limit_rejections_total ",
		"# HELP tenant_api_rate_limit_active_callers",
		"# TYPE tenant_api_rate_limit_active_callers gauge",
		"tenant_api_rate_limit_active_callers ",
	} {
		if !bytes.Contains([]byte(body), []byte(want)) {
			t.Errorf("metrics output missing %q\nbody:\n%s", want, body)
		}
	}
}
