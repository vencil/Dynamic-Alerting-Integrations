package handler

// ============================================================
// HTTP middleware for the tenant-api request chain:
//
//   * RequestIDResponse — pulls the chi RequestID out of the
//                       request context and writes it back as an
//                       `X-Request-ID` response header so callers
//                       can correlate to logs.
//   * SlogRequestLogger — one structured slog line per request
//                       (method, path, status, latency, request_id).
//
// The per-caller sliding-window rate limiter (RateLimit / rateLimiter)
// lives in rate_limiter.go; the request-body size config
// (DefaultMaxBodyBytes / MaxBodyBytesFromEnv) is at the bottom of this
// file. Both are deliberately homegrown (no new module dependency) so
// the tenant-api go.mod surface stays minimal.
// ============================================================

import (
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5/middleware"
)

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

// ─────────────────────────────────────────────────────────────────
// Request body size limit (issue #144)
// ─────────────────────────────────────────────────────────────────

// DefaultMaxBodyBytes is the request-body cap applied by every
// write handler via `io.LimitReader(r.Body, d.MaxBodyBytes)`. 1 MiB
// fits even the largest realistic tenant YAML (deeply-nested rule
// pack with hundreds of thresholds) with order-of-magnitude
// headroom, while keeping a single oversize POST from holding
// 100 MB in memory long enough to OOM the pod. Operators tuning
// for atypical payloads override via `TA_MAX_BODY_BYTES`.
const DefaultMaxBodyBytes int64 = 1 << 20

// DefaultMaxBatchBodyBytes is the request-body cap for the BATCH write endpoints
// (POST /tenants/batch, POST /groups/{id}/batch), which before #1722 read their
// body with a bare json.NewDecoder and so had no cap at all — DefaultMaxBodyBytes
// only ever reached handlers calling readLimitedBody, and there is no body-size
// middleware.
//
// It is tighter than DefaultMaxBodyBytes because the cost profile differs in
// kind: WritePRBatch takes the single-writer token FIRST and then validates every
// op TWICE inside it, and GroupBatch applies one patch to EVERY member — so an
// oversize batch body delays every OTHER tenant's write, not just its sender's.
//
// ⛔ NO CAP HERE BOUNDS THE PRODUCT. Cost is (ops) x (per-document parse cost);
// this bounds the first factor and TA_MAX_TENANT_DOC_BYTES the second, so a
// request at the 1000-op ceiling with each document just under that cap
// satisfies both and still occupies the write plane for minutes — tracked
// separately, and it needs a new policy number rather than a tighter byte cap.
//
// The pre-merge bound that byte caps genuinely could not express — patch key
// count, which mergePatchYAML is quadratic in, inside the lock, before either
// byte gate — IS closed: BatchOperation.Patch and GroupBatchRequest.Patch both
// carry `validate:"max=1000"`.
//
// ⚠️ No wall-clock figure is quoted here on purpose: it cannot be verified
// mechanically and rots. Run gitops' BenchmarkValidateAtCap for the per-document
// half. Sizing against real batches is asserted by
// TestRealisticBulkBatchFitsTheBudget — including the counter-example: the
// key-count and per-key length limits still leave a batch whose every field is
// legal free to exceed this budget and get a 413.
// Override via `TA_MAX_BATCH_BODY_BYTES`.
const DefaultMaxBatchBodyBytes int64 = 256 << 10

// MaxBatchBodyBytesFromEnv reads `TA_MAX_BATCH_BODY_BYTES`, mirroring
// MaxBodyBytesFromEnv exactly (strict full-string parse; "0"/negative/
// unparseable → default + malformed=true so the caller can WARN at startup).
func MaxBatchBodyBytesFromEnv(envValue string) (n int64, malformed bool) {
	v := strings.TrimSpace(envValue)
	if v == "" {
		return DefaultMaxBatchBodyBytes, false
	}
	parsed, err := strconv.ParseInt(v, 10, 64)
	if err != nil || parsed <= 0 {
		return DefaultMaxBatchBodyBytes, true
	}
	return parsed, false
}

// MaxBodyBytesFromEnv reads `TA_MAX_BODY_BYTES` and returns
// (bytes, malformed). Mirrors RateLimitConfigFromEnv: returns the
// default fallback on any out-of-range / unparseable input AND
// flags malformed so the caller can WARN at startup. Zero is NOT
// a valid value — a 0-byte cap would reject every write and is
// almost certainly a config error, so it's treated as malformed.
//
// Recognised values:
//   - empty / absent → 1<<20 (default), malformed=false
//   - any positive integer → that integer, malformed=false
//   - "0", negative, or unparseable → default, **malformed=true**
func MaxBodyBytesFromEnv(envValue string) (n int64, malformed bool) {
	v := strings.TrimSpace(envValue)
	if v == "" {
		return DefaultMaxBodyBytes, false
	}
	// #795 F4: strict full-string parse (see RateLimitConfigFromEnv) — Sscanf
	// accepted numeric prefixes like "1048576x", silently using a wrong cap.
	parsed, err := strconv.ParseInt(v, 10, 64)
	if err != nil || parsed <= 0 {
		return DefaultMaxBodyBytes, true
	}
	return parsed, false
}
