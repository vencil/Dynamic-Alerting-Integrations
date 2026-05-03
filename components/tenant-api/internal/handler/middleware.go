package handler

// ============================================================
// HTTP middleware additions for v2.8.0 Phase B Track C (B-6
// Tenant API hardening, PR-1 of 2):
//
//   * RateLimit       — per-caller sliding-window rate limiter,
//                       returns 429 + JSON error + Retry-After
//                       on cap exceeded.
//   * RequestIDResponse — pulls the chi RequestID out of the
//                       request context and writes it back as an
//                       `X-Request-ID` response header so callers
//                       can correlate to logs.
//
// Both are deliberately homegrown (no new module dependency)
// because the implementations are tiny and easy to audit, and
// the tenant-api go.mod surface stays minimal.
// ============================================================

import (
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/go-chi/chi/v5/middleware"
)

// ─────────────────────────────────────────────────────────────────
// Rate limiting
// ─────────────────────────────────────────────────────────────────

// RateLimitConfig configures the per-caller request budget. A
// requestsPerMinute value of zero disables the limiter (the
// middleware degrades to a no-op, useful for dev / single-tenant
// deployments where rate limiting is not needed).
type RateLimitConfig struct {
	// RequestsPerMinute is the maximum number of requests a single
	// caller is allowed within any rolling 60-second window. Zero
	// disables the limiter entirely.
	RequestsPerMinute int

	// SkipPaths is a set of HTTP paths exempt from rate limiting.
	// Typically health probes (`/health`, `/ready`, `/metrics`)
	// because those need to be hit on every kube-probe interval
	// without burning request budget against `system` callers.
	SkipPaths map[string]bool
}

// DefaultRateLimit returns a baseline configuration:
//
//   - 100 requests/minute per caller (matches the contract pinned
//     in `v2.8.0-planning.md` §C-1 for the search API and reused
//     here for consistency)
//   - skip the standard probe / metric paths
//
// Production deployments override `RequestsPerMinute` via the
// `TA_RATE_LIMIT_PER_MIN` env var; a value of 0 disables the
// limiter entirely (e.g. for single-tenant CI runs).
func DefaultRateLimit() RateLimitConfig {
	return RateLimitConfig{
		RequestsPerMinute: 100,
		SkipPaths: map[string]bool{
			"/health":  true,
			"/ready":   true,
			"/metrics": true,
		},
	}
}

// callerBucket tracks the timestamps of recent requests for one
// caller. The slice is kept sorted oldest-first; a write trims any
// timestamps older than the rolling window.
//
// We keep this minimal (no `sync.Mutex` per bucket) because the
// outer map is guarded by a single mutex — the throughput cost of
// the global lock is well under the network round-trip cost at
// 100 RPM, and the simpler structure is easier to audit.
type callerBucket struct {
	timestamps []time.Time
}

// rateLimiter is the global sliding-window store. The map key is
// the caller identity. PR-11/11 added two memory-bound features
// that the original PR-1 design noted as "follow-up if observed":
//
//   - rejections atomic counter: every blocked request increments
//     `rejections`, exposed via /metrics as
//     `tenant_api_rate_limit_rejections_total`. Operators can alert
//     on rejection rate and per-caller dashboards (when caller
//     dimensionality is added) without instrumenting the call
//     sites individually.
//
//   - bucket sweeper goroutine: every `sweepInterval` (default
//     5 min), walks the map and evicts any bucket whose oldest
//     timestamp is older than the rolling window. Keeps memory
//     bounded against pathological caller-set growth (e.g. a
//     misconfigured oauth2-proxy issuing fresh anonymous IDs
//     per request).
//
// The lifecycle of the sweeper is owned by the RateLimit
// middleware closure: closing the limiter's stopCh terminates
// the sweep loop. Test callers that want a short-lived limiter
// pass a context-bound stop channel via newRateLimiterWithSweep.
type rateLimiter struct {
	mu      sync.Mutex
	buckets map[string]*callerBucket
	cfg     RateLimitConfig

	rejections atomic.Int64

	// stopCh terminates the sweeper goroutine. nil for limiters
	// constructed via newRateLimiter (no sweeper); set when the
	// caller wants the background sweep loop.
	stopCh chan struct{}
}

// sweepInterval is how often the sweeper walks the bucket map and
// evicts callers whose oldest activity has aged out of the rolling
// window. 5 min is a coarse pace — the limiter itself trims
// expired timestamps on every allow() so memory growth between
// sweeps is at most one bucket per active caller per minute.
const sweepInterval = 5 * time.Minute

func newRateLimiter(cfg RateLimitConfig) *rateLimiter {
	return &rateLimiter{
		buckets: make(map[string]*callerBucket),
		cfg:     cfg,
	}
}

// newRateLimiterWithSweep is newRateLimiter plus a background
// sweep loop bound to stopCh. Closing stopCh stops the sweeper.
// Used by RateLimit() for production wiring; tests with explicit
// no-sweeper expectations can keep using newRateLimiter directly.
func newRateLimiterWithSweep(cfg RateLimitConfig, stopCh chan struct{}) *rateLimiter {
	l := newRateLimiter(cfg)
	l.stopCh = stopCh
	go l.sweepLoop(sweepInterval, stopCh)
	return l
}

// sweepLoop periodically evicts buckets with no in-window
// timestamps. Lock-free read of `buckets` is unsafe; the loop
// takes the same mutex allow() does. Sweep cost is O(N callers)
// with cheap per-bucket work (slice trim + len check), well under
// the 5-min cadence in any realistic operator deployment.
func (l *rateLimiter) sweepLoop(interval time.Duration, stopCh <-chan struct{}) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-stopCh:
			return
		case now := <-ticker.C:
			l.sweep(now)
		}
	}
}

// sweep walks every bucket, trims expired timestamps, and deletes
// any whose timestamp slice is empty afterward. Caller MUST NOT
// hold l.mu — sweep takes it itself.
func (l *rateLimiter) sweep(now time.Time) {
	l.mu.Lock()
	defer l.mu.Unlock()
	cutoff := now.Add(-time.Minute)
	for caller, b := range l.buckets {
		dropped := 0
		for _, t := range b.timestamps {
			if t.After(cutoff) {
				break
			}
			dropped++
		}
		if dropped > 0 {
			b.timestamps = b.timestamps[dropped:]
		}
		if len(b.timestamps) == 0 {
			delete(l.buckets, caller)
		}
	}
}

// activeCallers returns the number of callers with at least one
// in-window timestamp. O(1) — just the map size after the most
// recent sweep / allow trim.
func (l *rateLimiter) activeCallers() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.buckets)
}

// Rejections returns the total number of requests denied by the
// limiter since process start. Exported for /metrics.
func (l *rateLimiter) Rejections() int64 {
	return l.rejections.Load()
}

// allow returns (true, 0) if the caller may proceed; (false,
// retryAfter) where retryAfter is the integer seconds until the
// oldest queued timestamp expires the rolling window. The caller
// can put that value directly into a `Retry-After` header.
func (l *rateLimiter) allow(caller string, now time.Time) (bool, int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	b, ok := l.buckets[caller]
	if !ok {
		b = &callerBucket{}
		l.buckets[caller] = b
	}
	cutoff := now.Add(-time.Minute)
	// Trim expired timestamps. Since the slice is sorted
	// oldest-first, a single forward scan suffices.
	dropped := 0
	for _, t := range b.timestamps {
		if t.After(cutoff) {
			break
		}
		dropped++
	}
	if dropped > 0 {
		b.timestamps = b.timestamps[dropped:]
	}
	if len(b.timestamps) >= l.cfg.RequestsPerMinute {
		// At cap. The retry-after is the time until the oldest
		// timestamp falls out of the window, computed against
		// the caller-supplied `now` (NOT wall-clock `time.Now`)
		// so this method stays deterministic under tests that
		// inject simulated clocks. +1s rounding so the caller
		// doesn't fire a retry that rejects again at sub-second
		// precision drift.
		oldest := b.timestamps[0]
		retry := oldest.Add(time.Minute).Sub(now)
		if retry < 0 {
			retry = 0
		}
		secs := int(retry.Seconds()) + 1
		l.rejections.Add(1)
		return false, secs
	}
	b.timestamps = append(b.timestamps, now)
	return true, 0
}

// rateLimitCaller returns the identity used to bucket a request.
// Order of precedence:
//
//  1. `X-Forwarded-Email` (set by oauth2-proxy after auth) —
//     primary caller identity in production
//  2. `X-Real-IP` — fallback for unauthenticated probes /
//     pre-auth requests (rate limit by source IP if no identity)
//  3. literal `_anonymous` — last resort, shouldn't happen in
//     production but keeps the bucket key non-empty
//
// The fallback chain mirrors how `rbac.Middleware` keys identity
// (config.go::Middleware) so the rate limiter and the RBAC layer
// agree on who's calling.
func rateLimitCaller(r *http.Request) string {
	if email := strings.TrimSpace(r.Header.Get("X-Forwarded-Email")); email != "" {
		return email
	}
	if ip := strings.TrimSpace(r.Header.Get("X-Real-IP")); ip != "" {
		return "ip:" + ip
	}
	if ra := strings.TrimSpace(r.RemoteAddr); ra != "" {
		// RemoteAddr typically includes ":port" — strip it so
		// the bucket key is just the IP.
		if i := strings.LastIndex(ra, ":"); i > 0 {
			ra = ra[:i]
		}
		return "ip:" + ra
	}
	return "_anonymous"
}

// activeLimiter holds the most-recently installed rateLimiter so
// /metrics can expose its rejection counter + active-caller gauge
// without threading the limiter through Deps. There's only one
// RateLimit middleware in the chain in production; tests that
// want isolation construct their own limiters via newRateLimiter.
var activeLimiter atomic.Pointer[rateLimiter]

// RateLimitMetrics returns counters from the currently-installed
// limiter for /metrics rendering. Returns (0, 0) when no limiter
// is installed (cfg.RequestsPerMinute <= 0 or pre-wiring).
func RateLimitMetrics() (rejections int64, activeCallers int) {
	if l := activeLimiter.Load(); l != nil {
		return l.Rejections(), l.activeCallers()
	}
	return 0, 0
}

// RateLimit returns chi middleware that throttles per-caller
// request rate. Skipped paths are exempt; everything else is
// counted. Over-cap responses return JSON with the same shape
// the rest of the API uses (error key) plus a code and
// retry_after_s field for programmatic clients.
//
// `stopCh` controls the bucket-sweeper goroutine lifecycle. Pass
// the same stop channel main.go uses to terminate hot-reload
// loops; on shutdown, closing stopCh stops the sweeper cleanly.
// Tests that don't want a background sweeper can pass a fresh
// channel and never close it (the sweeper sleeps until interval
// or stopCh; idle cost is one ticker per process).
func RateLimit(cfg RateLimitConfig, stopCh <-chan struct{}) func(http.Handler) http.Handler {
	if cfg.RequestsPerMinute <= 0 {
		// Limiter disabled: hand back the identity middleware so
		// the chain composes cleanly.
		return func(next http.Handler) http.Handler { return next }
	}
	// Production limiters get the sweeper; the activeLimiter
	// pointer is updated so /metrics finds the right one even
	// across cfg reloads (not currently used but cheap to support).
	sweepStop := make(chan struct{})
	go func() {
		<-stopCh
		close(sweepStop)
	}()
	limiter := newRateLimiterWithSweep(cfg, sweepStop)
	activeLimiter.Store(limiter)
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if cfg.SkipPaths != nil && cfg.SkipPaths[r.URL.Path] {
				next.ServeHTTP(w, r)
				return
			}
			caller := rateLimitCaller(r)
			ok, retryAfter := limiter.allow(caller, time.Now())
			if !ok {
				// Retry-After header is set per RFC 6585 in addition
				// to the body field — clients with HTTP-aware retry
				// libs (e.g. http.Client wrappers) honor the header
				// without parsing JSON.
				w.Header().Set("Retry-After", fmt.Sprintf("%d", retryAfter))
				writeErrorEnvelope(w, r, http.StatusTooManyRequests, ErrorResponse{
					Error:       fmt.Sprintf("rate limit exceeded for %s; try again in %ds", caller, retryAfter),
					Code:        CodeRateLimited,
					RetryAfterS: retryAfter,
				})
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// ─────────────────────────────────────────────────────────────────
// Request ID echoing
// ─────────────────────────────────────────────────────────────────

// RequestIDResponse copies the chi-injected request ID from the
// request context into the `X-Request-ID` response header. chi's
// `middleware.RequestID` only puts the ID into the context for
// downstream handlers and the structured logger; it does not
// echo it to the caller. Without this echo, customers cannot
// correlate their HTTP request to the corresponding log line in
// our backend, which complicates support requests + audit.
//
// Mount this AFTER `middleware.RequestID` so the context already
// carries the value. The middleware is a no-op if the chi
// middleware was somehow not mounted (defensive — never errors).
func RequestIDResponse(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if reqID := middleware.GetReqID(r.Context()); reqID != "" {
			w.Header().Set("X-Request-ID", reqID)
		}
		next.ServeHTTP(w, r)
	})
}

// SlogRequestLogger emits one structured slog line per request, in
// the same shape chi's text-based middleware.Logger does (method,
// path, status, latency) but as JSON with the chi request_id
// attached. PR-10/11: replaces middleware.Logger so request lines
// land on the same structured pipeline as gitops / config / tracker
// logs.
//
// 5xx responses are logged at WARN; everything else at INFO.
// Mount AFTER middleware.RequestID so request_id is available in
// the context.
func SlogRequestLogger(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		ww := middleware.NewWrapResponseWriter(w, r.ProtoMajor)
		next.ServeHTTP(ww, r)

		latencyMs := float64(time.Since(start).Microseconds()) / 1000.0
		attrs := []any{
			"request_id", middleware.GetReqID(r.Context()),
			"method", r.Method,
			"path", r.URL.Path,
			"status", ww.Status(),
			"bytes", ww.BytesWritten(),
			"latency_ms", latencyMs,
			"remote", r.RemoteAddr,
		}
		// Caller identity if oauth2-proxy populated it.
		if email := r.Header.Get("X-Forwarded-Email"); email != "" {
			attrs = append(attrs, "caller", email)
		}

		switch {
		case ww.Status() >= 500:
			slog.Warn("request", attrs...)
		default:
			slog.Info("request", attrs...)
		}
	})
}

// RateLimitConfigFromEnv reads `TA_RATE_LIMIT_PER_MIN` and
// returns (config, malformed). When `malformed` is true, the
// returned config is the default fallback and the caller SHOULD
// emit a startup warning so operators don't ship typo'd env vars
// silently. Logging is left to the caller (typically
// `cmd/server/main.go` startup banner) so this function stays
// pure for unit tests.
//
// Recognised values:
//   - empty / absent → 100 (default), malformed=false
//   - "0" → 0 (limiter disabled), malformed=false
//   - any positive integer → that integer, malformed=false
//   - any other value → 100 (default), **malformed=true**
func RateLimitConfigFromEnv(envValue string) (cfg RateLimitConfig, malformed bool) {
	cfg = DefaultRateLimit()
	v := strings.TrimSpace(envValue)
	if v == "" {
		return cfg, false
	}
	var n int
	if _, err := fmt.Sscanf(v, "%d", &n); err != nil || n < 0 {
		return cfg, true
	}
	cfg.RequestsPerMinute = n
	return cfg, false
}
