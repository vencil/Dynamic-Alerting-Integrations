package handler

import (
	"fmt"
	"net/http"
	"sort"
	"sync/atomic"
	"time"

	"github.com/vencil/tenant-api/internal/federation/orphan"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/ws"
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

// devBypassActive records whether --dev-bypass-auth is enabled, surfaced at
// /metrics as the Layer-2 tripwire gauge so the dangerous local-dev mode is
// detectable by monitoring in ANY environment (ADR-022).
var devBypassActive atomic.Bool

// SetDevBypassActive records the dev-auth-bypass state for /metrics. Called
// once at startup from main when the flag is parsed.
func SetDevBypassActive(on bool) { devBypassActive.Store(on) }

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

	// ADR-020 #521: federation artifacts left by an incomplete tenant
	// offboarding. Non-zero ⇒ work docs/internal/tenant-offboarding-runbook.md.
	orphanTokens, orphanSubsets := orphan.OrphanCounts()
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_federation_orphaned_tokens Live federation token records whose tenant is no longer in conf.d.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_federation_orphaned_tokens gauge\n")
	_, _ = fmt.Fprintf(w, "tenant_api_federation_orphaned_tokens %d\n", orphanTokens)
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_federation_orphaned_subset_files Stale conf.d/_federation/<tenant>.yaml subset files whose tenant is no longer in conf.d.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_federation_orphaned_subset_files gauge\n")
	_, _ = fmt.Fprintf(w, "tenant_api_federation_orphaned_subset_files %d\n", orphanSubsets)

	// ADR-027 PR-1b-i: machine-identity (KSA/TokenReview) audit counters.
	// Audit-only — this family records verification outcomes and NEVER
	// reflects an authz decision. All four result series are emitted (0 when
	// audit is disabled) so the metric's shape is stable across the flag.
	writeIdentityAuditMetrics(w)

	// ADR-022 Layer 2 tripwire: 1 ⇒ --dev-bypass-auth is ON (LOCAL DEV ONLY).
	// MUST be 0 in production; alert if 1 outside a dev/compose environment.
	devBypass := 0
	if devBypassActive.Load() {
		devBypass = 1
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_dev_auth_bypass_active 1 if --dev-bypass-auth is enabled (LOCAL DEV ONLY; must be 0 in production).\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_dev_auth_bypass_active gauge\n")
	_, _ = fmt.Fprintf(w, "tenant_api_dev_auth_bypass_active %d\n", devBypass)

	// #632/#645: forge circuit breaker state per provider. 0=closed (healthy),
	// 1=half-open (probing recovery), 2=open (fast-failing — forge degraded).
	// Empty (no line emitted) in direct write mode where no forge client exists.
	// Alert: state == 2 for >2m ⇒ the forge (GitHub/GitLab) is down and writes
	// are being rejected with 503 FORGE_UNAVAILABLE.
	circuits := platform.CircuitSnapshot()
	if len(circuits) > 0 {
		_, _ = fmt.Fprintf(w, "# HELP tenant_api_forge_circuit_state Forge circuit breaker state (0=closed, 1=half-open, 2=open).\n")
		_, _ = fmt.Fprintf(w, "# TYPE tenant_api_forge_circuit_state gauge\n")
		providers := make([]string, 0, len(circuits))
		for p := range circuits {
			providers = append(providers, p)
		}
		sort.Strings(providers) // deterministic exposition order
		for _, provider := range providers {
			_, _ = fmt.Fprintf(w, "tenant_api_forge_circuit_state{provider=%q} %d\n",
				provider, circuitStateValue(circuits[provider]))
		}
	}

	// #646: count of tracked PRs/MRs in merge conflict at the last tracker
	// sync, per provider. Near-zero by construction (see platform
	// MergeableConflict docs); non-zero ⇒ an out-of-band edit broke a
	// tenant-api PR. Empty in direct write mode (no tracker).
	conflicts := platform.ConflictSnapshot()
	if len(conflicts) > 0 {
		_, _ = fmt.Fprintf(w, "# HELP tenant_api_forge_pr_conflicts Tracked PRs/MRs in merge conflict at the last tracker sync.\n")
		_, _ = fmt.Fprintf(w, "# TYPE tenant_api_forge_pr_conflicts gauge\n")
		cprov := make([]string, 0, len(conflicts))
		for p := range conflicts {
			cprov = append(cprov, p)
		}
		sort.Strings(cprov)
		for _, provider := range cprov {
			_, _ = fmt.Fprintf(w, "tenant_api_forge_pr_conflicts{provider=%q} %d\n", provider, conflicts[provider])
		}
	}

	// #143: number of currently-connected SSE (/api/v1/events) clients. Each is
	// one serving goroutine; a steadily-climbing value under steady client
	// count signals the goroutine leak this gauge exists to detect. Omitted
	// when no hub has been constructed (ok=false).
	if sseClients, ok := ws.ClientCountSnapshot(); ok {
		_, _ = fmt.Fprintf(w, "# HELP tenant_api_sse_clients Currently-connected SSE (/api/v1/events) clients.\n")
		_, _ = fmt.Fprintf(w, "# TYPE tenant_api_sse_clients gauge\n")
		_, _ = fmt.Fprintf(w, "tenant_api_sse_clients %d\n", sseClients)
	}
}

// circuitStateValue maps the gobreaker state string to the gauge encoding.
// Unknown strings map to -1 so a future gobreaker rename surfaces as an
// obviously-wrong value rather than silently looking healthy.
func circuitStateValue(state string) int {
	switch state {
	case "closed":
		return 0
	case "half-open":
		return 1
	case "open":
		return 2
	default:
		return -1
	}
}
