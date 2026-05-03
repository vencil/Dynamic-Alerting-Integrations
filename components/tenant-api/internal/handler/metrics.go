package handler

import (
	"fmt"
	"net/http"
	"sync/atomic"
	"time"
)

// Metrics tracks basic request counters exposed at /metrics.
// Uses atomic counters — no external dependency (no prometheus/client_golang).
var Metrics = &metricsState{}

type metricsState struct {
	requestsTotal atomic.Int64
	errorsTotal   atomic.Int64
	writesTotal   atomic.Int64
	startTime     time.Time
}

func init() {
	Metrics.startTime = time.Now()
}

// IncRequests increments the total request counter.
func (m *metricsState) IncRequests() { m.requestsTotal.Add(1) }

// IncErrors increments the total error counter.
func (m *metricsState) IncErrors() { m.errorsTotal.Add(1) }

// IncWrites increments the total write counter.
func (m *metricsState) IncWrites() { m.writesTotal.Add(1) }

// MetricsMiddleware is a chi middleware that increments request/error counters.
func MetricsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		Metrics.IncRequests()
		ww := &statusWriter{ResponseWriter: w}
		next.ServeHTTP(ww, r)
		if ww.status >= 400 {
			Metrics.IncErrors()
		}
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

// MetricsHandler handles GET /metrics in Prometheus exposition format.
//
// PR-11/11 added two rate-limiter metrics (`rejections_total` counter
// + `active_callers` gauge) sourced from the package-level
// activeLimiter pointer. When no limiter is installed (cfg
// disabled or pre-wiring) both render as 0.
func MetricsHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	uptime := time.Since(Metrics.startTime).Seconds()
	rejections, activeCallers := RateLimitMetrics()
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_up Whether the tenant-api is up.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_up gauge\n")
	_, _ = fmt.Fprintf(w, "tenant_api_up 1\n")
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_uptime_seconds Seconds since tenant-api started.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_uptime_seconds gauge\n")
	_, _ = fmt.Fprintf(w, "tenant_api_uptime_seconds %.1f\n", uptime)
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_requests_total Total API requests.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_requests_total counter\n")
	_, _ = fmt.Fprintf(w, "tenant_api_requests_total %d\n", Metrics.requestsTotal.Load())
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_errors_total Total API errors.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_errors_total counter\n")
	_, _ = fmt.Fprintf(w, "tenant_api_errors_total %d\n", Metrics.errorsTotal.Load())
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_writes_total Total write operations (git commits).\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_writes_total counter\n")
	_, _ = fmt.Fprintf(w, "tenant_api_writes_total %d\n", Metrics.writesTotal.Load())
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_rate_limit_rejections_total Total requests denied by the per-caller rate limiter.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_rate_limit_rejections_total counter\n")
	_, _ = fmt.Fprintf(w, "tenant_api_rate_limit_rejections_total %d\n", rejections)
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_rate_limit_active_callers Number of callers with at least one request inside the rolling window.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_rate_limit_active_callers gauge\n")
	_, _ = fmt.Fprintf(w, "tenant_api_rate_limit_active_callers %d\n", activeCallers)
}
