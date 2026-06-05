package handler

// ============================================================
// GET /api/v1/tenants/{id}/metrics — v2.9.0 (ADR-024 §S6, #741)
// ============================================================
//
// Metric discovery catalog: lists the metric NAMES that have at least
// one series carrying {tenant="<id>"} in the last 24h, optionally
// filtered by a name prefix (`?q=`). Backs the portal recipe-authoring
// UX so a tenant can pick a metric to write a custom-alert recipe
// against without knowing PromQL.
//
// Stateless proxy (ADR-024 §S6 "Dumb Pipes, Smart Endpoints"): every
// call re-queries Prometheus; no catalog state is held. Cross-tenant
// isolation is two-layered — route middleware enforces the caller's
// RBAC read permission on {id}, and the discoverer force-builds a
// `{tenant="<id>"}` matcher so the result can only ever contain that
// tenant's own metrics.
//
// Rate limiting: this endpoint hits Prometheus (more expensive than the
// disk-backed reads), but it is already covered by the global per-caller
// sliding-window limiter (TA_RATE_LIMIT_PER_MIN, default 100/min → 429),
// and every backend call is triple-bounded (timeout + io.LimitReader +
// `limit=`). A dedicated, stricter per-endpoint limit is deferred-with-
// trigger (ADR-024 §S6 Reef 1) — add it only if discovery-specific abuse
// is observed; building a second limiter subsystem now would be
// redundant against the abuse vector the global limiter already bounds.

import (
	"encoding/json"
	"net/http"
	"regexp"

	"github.com/go-chi/chi/v5"
)

// metricNameQueryPattern bounds the `?q=` prefix to the Prometheus
// metric-name charset. Validating (not escaping) the input is the
// injection boundary: a value that matches this pattern contains no
// regex metacharacter and no quote, so embedding it into the
// `__name__=~"^<q>.*"` selector cannot break out of the literal or
// alter the match semantics (ADR-024 §S6 security decision).
var metricNameQueryPattern = regexp.MustCompile(`^[a-zA-Z0-9_:]*$`)

// maxQueryPrefixLen caps the ?q= prefix. Real metric names are well
// under this; the bound just stops a pathological multi-KB prefix from
// building an oversized selector string (cheap hygiene — RE2 is linear,
// so this is not a ReDoS guard).
const maxQueryPrefixLen = 256

// DiscoverMetricsResponse is the body of GET /api/v1/tenants/{id}/metrics.
type DiscoverMetricsResponse struct {
	// Metrics is the sorted list of metric names visible to this tenant.
	Metrics []string `json:"metrics"`
	// Truncated is true when the result hit the server-side cap; the
	// caller should narrow with a longer prefix.
	Truncated bool `json:"truncated"`
}

// DiscoverMetrics handles GET /api/v1/tenants/{id}/metrics.
//
// @Summary     Discover a tenant's metric names
// @Description Lists the metric names that have at least one series
// @Description carrying {tenant="<id>"} in the last 24h, optionally
// @Description filtered by a name prefix. Backs the portal recipe-
// @Description authoring UX (ADR-024 Capability B). Stateless proxy over
// @Description Prometheus; the result can only contain the tenant's own
// @Description metrics.
// @Tags        tenants
// @Produce     json
// @Param       id  path     string true  "Tenant ID"
// @Param       q   query    string false "Metric-name prefix filter ([a-zA-Z0-9_:]*)"
// @Success     200 {object} DiscoverMetricsResponse
// @Failure     400 {object} map[string]string
// @Failure     429 {object} map[string]string
// @Failure     502 {object} map[string]string
// @Failure     503 {object} map[string]string
// @Router      /api/v1/tenants/{id}/metrics [get]
func DiscoverMetrics(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// Discovery disabled when --federation-prometheus-url is unset.
		if d.MetricDiscoverer == nil {
			WriteJSONError(w, r, http.StatusServiceUnavailable,
				"metric discovery is disabled — set --federation-prometheus-url to enable")
			return
		}

		tenantID := chi.URLParam(r, "id")
		// tenantID came through TenantIDFromPath + RBAC read enforcement,
		// so the caller is authorised for exactly this tenant.

		q := r.URL.Query().Get("q")
		if len(q) > maxQueryPrefixLen {
			WriteJSONError(w, r, http.StatusBadRequest,
				"invalid q: prefix too long")
			return
		}
		if !metricNameQueryPattern.MatchString(q) {
			WriteJSONError(w, r, http.StatusBadRequest,
				"invalid q: only metric-name characters [a-zA-Z0-9_:] are allowed")
			return
		}

		names, truncated, err := d.MetricDiscoverer.Discover(
			r.Context(), tenantID, q, 0 /* default limit */)
		if err != nil {
			// Upstream Prometheus failure (unreachable / timeout / bad
			// response). 502 = we are a healthy proxy but the backend
			// failed, distinct from the 503 "feature disabled" above.
			WriteJSONError(w, r, http.StatusBadGateway,
				"metric discovery upstream error: "+err.Error())
			return
		}

		resp := DiscoverMetricsResponse{Metrics: names, Truncated: truncated}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}
}
