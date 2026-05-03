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
	"net/http"
	"strings"
	"sync"
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
// the caller identity; entry retention is unbounded across the
// process lifetime, which is fine here because:
//
//   - the per-caller bucket is at most `RequestsPerMinute`
//     timestamps + a handful of bytes for the slice header;
//   - in production the caller set is bounded by the OAuth
//     identity universe (a few hundred to a few thousand);
//   - callers idle for > 1 minute reduce to one timestamp on
//     the next hit, which trim-on-write naturally collapses.
//
// If we ever observe pathological growth (e.g. anonymous calls
// with unique X-Real-IP per request flooding the cache), a
// background sweeper goroutine can be added later without
// changing the public middleware contract.
type rateLimiter struct {
	mu      sync.Mutex
	buckets map[string]*callerBucket
	cfg     RateLimitConfig
}

func newRateLimiter(cfg RateLimitConfig) *rateLimiter {
	return &rateLimiter{
		buckets: make(map[string]*callerBucket),
		cfg:     cfg,
	}
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

// RateLimit returns chi middleware that throttles per-caller
// request rate. Skipped paths are exempt; everything else is
// counted. Over-cap responses return JSON with the same shape
// the rest of the API uses (error key) plus a code and
// retry_after_s field for programmatic clients.
func RateLimit(cfg RateLimitConfig) func(http.Handler) http.Handler {
	if cfg.RequestsPerMinute <= 0 {
		// Limiter disabled: hand back the identity middleware so
		// the chain composes cleanly.
		return func(next http.Handler) http.Handler { return next }
	}
	limiter := newRateLimiter(cfg)
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
