package fedpolicy

// Metric discovery (ADR-024 §S6 Capability B) — the read-only "what
// metrics do I have?" catalog that backs the portal recipe-authoring UX.
//
// A tenant authoring a custom alert recipe must pick a metric, but they
// have no way to know which app-metric names their workload actually
// exposes. This discoverer answers that: given a tenant ID and an
// optional name prefix, it returns the metric NAMES that have at least
// one series carrying `{tenant="<id>"}` in the lookback window.
//
// It lives in this package — alongside the federation AdmissionValidator —
// purely to reuse the same triple-bounded Prometheus-metadata querying
// discipline (timeout + io.LimitReader + `limit=`) and the same
// `--federation-prometheus-url` backend. It is NOT part of the federation
// 2-tier policy; it is a stateless proxy (ADR-024 §S6 "Dumb Pipes, Smart
// Endpoints").
//
// Query design (mirrors admission.go's safety rules):
//
//   - Uses the label-VALUES metadata API
//     (`/api/v1/label/__name__/values`), which is index-only — it never
//     reads sample chunks, so it cannot OOM the backend the way a range
//     query can.
//   - The `match[]` selector is force-built server-side:
//     `{tenant="<id>",__name__=~"^<q>.*"}`. The tenant label is branded
//     at scrape time by the tenant-exporters Job (not tenant-forgeable),
//     so a tenant can only ever list THEIR OWN metrics — cross-tenant
//     snooping is structurally impossible. The caller's RBAC read
//     permission on `<id>` is already enforced by route middleware.
//   - `q` is validated by the handler against the metric-name charset
//     (`[a-zA-Z0-9_:]*`) BEFORE it reaches here, so it can contain no
//     regex metacharacter and no quote — injection into the selector is
//     impossible by construction (stronger than escaping).
//   - Triple-bounded: a `limit` query parameter, an io.LimitReader cap
//     on the response body, and a context timeout. A backend that
//     ignores `limit` is still contained by the other two.

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	// discoveryWindow is the lookback for "which metrics exist for this
	// tenant". 24h (not a short window) deliberately covers daily batch /
	// CronJob and other intermittent metrics — the authoring pain is "I
	// can't find the metric I just wrote", far worse than seeing an old
	// dead metric (ADR-024 §S6 lookback decision).
	discoveryWindow = 24 * time.Hour
	// DefaultDiscoveryLimit caps how many metric names a single discovery
	// call returns. The portal autocomplete only needs enough to filter;
	// a tenant with more distinct metric names than this gets a truncated
	// list (truncated=true) and should narrow with a prefix.
	DefaultDiscoveryLimit = 200
)

// MetricDiscoverer lists a tenant's own metric names from a
// Prometheus-compatible backend. Construct it with NewMetricDiscoverer;
// a nil *MetricDiscoverer means discovery is disabled (no backend URL
// configured) and callers must surface that as "service unavailable".
type MetricDiscoverer struct {
	baseURL string
	http    *http.Client
	window  time.Duration
}

// NewMetricDiscoverer builds a discoverer querying prometheusURL. An
// empty prometheusURL returns nil — the caller treats a nil discoverer
// as "discovery disabled" (HTTP 503).
func NewMetricDiscoverer(prometheusURL string) *MetricDiscoverer {
	if strings.TrimSpace(prometheusURL) == "" {
		return nil
	}
	return &MetricDiscoverer{
		baseURL: strings.TrimRight(prometheusURL, "/"),
		http:    &http.Client{Timeout: defaultQueryTimeout},
		window:  discoveryWindow,
	}
}

// Discover returns the sorted metric names that have at least one series
// carrying {tenant="<tenant>"} in the lookback window, optionally
// filtered to names starting with prefix. The bool return is true when
// the result hit `limit` (the tenant has more names than were returned).
//
// prefix MUST already be charset-validated by the caller (the handler
// rejects anything outside `[a-zA-Z0-9_:]*` with HTTP 400); this method
// embeds it into the selector regex without escaping, which is safe ONLY
// under that precondition.
func (d *MetricDiscoverer) Discover(ctx context.Context, tenant, prefix string, limit int) (names []string, truncated bool, err error) {
	if limit <= 0 {
		limit = DefaultDiscoveryLimit
	}

	// Force-build the selector server-side. tenant is RBAC-validated +
	// filename-safe; prefix is charset-validated by the handler. Neither
	// can break out of the quoted literal.
	selector := `{` + tenantLabel + `="` + tenant + `"`
	if prefix != "" {
		selector += `,__name__=~"^` + prefix + `.*"`
	}
	selector += `}`

	now := time.Now()
	u, err := url.Parse(d.baseURL + "/api/v1/label/__name__/values")
	if err != nil {
		return nil, false, fmt.Errorf("bad prometheus URL: %w", err)
	}
	qv := url.Values{}
	qv.Set("match[]", selector)
	qv.Set("start", strconv.FormatInt(now.Add(-d.window).Unix(), 10))
	qv.Set("end", strconv.FormatInt(now.Unix(), 10))
	qv.Set("limit", strconv.Itoa(limit))
	u.RawQuery = qv.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, false, err
	}
	resp, err := d.http.Do(req)
	if err != nil {
		return nil, false, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, false, fmt.Errorf("prometheus /api/v1/label/__name__/values: HTTP %d", resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes))
	if err != nil {
		return nil, false, err
	}
	if len(body) >= maxResponseBytes {
		return nil, false, fmt.Errorf("prometheus label-values response exceeded the %d-byte cap — narrow the query with a prefix", maxResponseBytes)
	}
	var lr struct {
		Status string   `json:"status"`
		Data   []string `json:"data"`
	}
	if err := json.Unmarshal(body, &lr); err != nil {
		return nil, false, fmt.Errorf("parse label-values response: %w", err)
	}
	if lr.Status != "success" {
		return nil, false, fmt.Errorf("prometheus label-values returned status %q", lr.Status)
	}

	names = lr.Data
	if names == nil {
		names = []string{}
	}
	sort.Strings(names)
	// A full page means the backend likely had more names to give.
	truncated = len(names) >= limit
	return names, truncated, nil
}
