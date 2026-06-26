package fedpolicy

// Admission validator (ADR-020 IV-2e) — the data-layer label-enrichment
// gate for the federation whitelist.
//
// When a metric is added to the platform whitelist, the validator
// checks whether that metric will actually work via federation: every
// series the proxy returns must already carry the tenant label, or the
// proxy's injected `{tenant="<X>"}` matcher yields an empty vector and
// the tenant's dashboard goes silently blank (#505 audit).
//
// Three-state verdict:
//
//   - Pass       — the metric has samples and every series carries the
//                  tenant label.
//   - HardBlock  — the metric has samples but some series LACK the
//                  tenant label. Whitelisting it would bury an
//                  empty-vector landmine, so this is a true-positive
//                  block.
//   - Warn       — the metric has no samples in the lookback window.
//                  Cold start (a freshly deployed service) and sparse
//                  metrics are legitimate, so this is a soft gate: an
//                  admin may proceed with an explicit --force.
//
// Query design (ADR-020 §前提約束; Gemini metadata-API review):
//
//   - The check uses ONLY the Series metadata API (`/api/v1/series`).
//     A range query like `count_over_time(metric[24h])` would force
//     Prometheus to load 24h of raw samples into memory and OOM on a
//     high-cardinality metric — the validator would become a DoS.
//   - The hard-block probe pushes the filter down to the TSDB index:
//     `metric{tenant=""}` matches only series MISSING the label, which
//     for a healthy metric is the empty set — a near-zero result.
//   - Every call is triple-bounded so the validator can never become a
//     resource sink itself: a `limit=1` query parameter, an
//     io.LimitReader cap on the response body, and a context timeout.
//     A backend that ignores `limit` is still contained by the other
//     two.

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

	"github.com/vencil/tenant-api/internal/platform"
)

// tenantLabel is the per-tenant label the platform's data layer carries
// and the proxy injects (#505 audit locked this to `tenant`, not
// `tenant_id`). The admission validator checks for this exact label.
const tenantLabel = "tenant"

const (
	// admissionWindow is the lookback for the existence / enrichment
	// check. 24h matches ADR-020 §前提約束.
	admissionWindow = 24 * time.Hour
	// defaultQueryTimeout bounds a single Series-API call. The check
	// runs on an interactive admin operation (a whitelist edit); a slow
	// backend should surface a timeout, not hang a tenant-api worker.
	defaultQueryTimeout = 5 * time.Second
	// maxResponseBytes caps how much of a Series-API response is read.
	// `limit=1` keeps the real response tiny; this is the fallback
	// guard for a backend that ignores `limit`.
	maxResponseBytes = 1 << 20
)

// AdmissionState is the outcome bucket of an admission check.
type AdmissionState string

const (
	AdmissionPass      AdmissionState = "pass"
	AdmissionHardBlock AdmissionState = "hard_block"
	AdmissionWarn      AdmissionState = "warn"
)

// AdmissionResult is the validator's verdict for one metric.
type AdmissionResult struct {
	Metric string         `json:"metric"`
	State  AdmissionState `json:"state"`
	Reason string         `json:"reason"`
	// PIILabels are label names on the metric that match a PII-name
	// heuristic. Advisory only — surfaced as a warning for reviewer
	// judgement, never a hard block (the heuristic is imprecise).
	PIILabels []string `json:"pii_labels,omitempty"`
}

// seriesQuerier issues Prometheus Series-metadata-API requests. The
// Series API is index-only — it never reads sample chunks — so it
// cannot OOM the backend the way a range query can.
type seriesQuerier struct {
	baseURL string
	http    *http.Client
}

// matchingSeries returns the label sets of series matching selector
// within [now-window, now], capped at limit. The response body is read
// through an io.LimitReader so a backend that ignores `limit` still
// cannot make tenant-api allocate without bound.
func (q *seriesQuerier) matchingSeries(ctx context.Context, selector string, window time.Duration, limit int) ([]map[string]string, error) {
	now := time.Now()
	u, err := url.Parse(q.baseURL + "/api/v1/series")
	if err != nil {
		return nil, fmt.Errorf("bad prometheus URL: %w", err)
	}
	qv := url.Values{}
	qv.Set("match[]", selector)
	qv.Set("start", strconv.FormatInt(now.Add(-window).Unix(), 10))
	qv.Set("end", strconv.FormatInt(now.Unix(), 10))
	if limit > 0 {
		qv.Set("limit", strconv.Itoa(limit))
	}
	u.RawQuery = qv.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := q.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("prometheus /api/v1/series: HTTP %d", resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes))
	if err != nil {
		return nil, err
	}
	// A body at the cap means the LimitReader truncated mid-document.
	// Short-circuit with a precise error rather than let json.Unmarshal
	// emit a misleading "unexpected end of JSON input" — `limit=1`
	// keeps real responses tiny, so hitting the cap means the backend
	// ignored `limit` on a very-high-cardinality metric.
	if len(body) >= maxResponseBytes {
		return nil, fmt.Errorf("prometheus /api/v1/series response exceeded the %d-byte cap — metric cardinality too high to validate", maxResponseBytes)
	}
	var sr struct {
		Status string              `json:"status"`
		Data   []map[string]string `json:"data"`
	}
	if err := json.Unmarshal(body, &sr); err != nil {
		return nil, fmt.Errorf("parse /api/v1/series response: %w", err)
	}
	if sr.Status != "success" {
		return nil, fmt.Errorf("prometheus /api/v1/series returned status %q", sr.Status)
	}
	return sr.Data, nil
}

// AdmissionValidator runs the data-layer label-enrichment check against
// a Prometheus-compatible backend. Construct it with NewAdmissionValidator;
// a nil *AdmissionValidator means the feature is disabled (no backend
// URL configured) and callers must skip the check.
type AdmissionValidator struct {
	q       *seriesQuerier
	window  time.Duration
	timeout time.Duration
}

// NewAdmissionValidator builds a validator querying prometheusURL. An
// empty prometheusURL returns nil — the caller treats a nil validator
// as "admission checking disabled".
func NewAdmissionValidator(prometheusURL string) *AdmissionValidator {
	if strings.TrimSpace(prometheusURL) == "" {
		return nil
	}
	return &AdmissionValidator{
		q: &seriesQuerier{
			baseURL: strings.TrimRight(prometheusURL, "/"),
			http:    platform.NewHTTPClient(defaultQueryTimeout),
		},
		window:  admissionWindow,
		timeout: defaultQueryTimeout,
	}
}

// Check runs the three-state admission check for one metric. A non-nil
// error means the check could not be completed (backend unreachable,
// timeout, malformed response); the caller decides how to treat that —
// ADR-020 maps an indeterminate result to the soft-gate (Warn) path,
// since an un-queryable metric cannot be proven bad.
// The hard-block test is "does NO series carry the tenant label", not
// "does ANY series lack it". In a shared Kubernetes cluster the same
// metric (`up`, `container_*`, `rest_client_requests_total`) has
// tenant-labelled series for tenant pods AND unlabelled series for
// platform pods (kube-system, the API server). The proxy injects
// `{tenant="<X>"}` and isolates each tenant to its own series, so the
// unlabelled platform series are harmless — blocking a metric just
// because they exist would make every K8s-native metric permanently
// un-whitelistable. The true failure mode is a metric whose series
// carry NO tenant label at all: federation then yields an empty vector
// for everyone.
func (v *AdmissionValidator) Check(ctx context.Context, metric string) (AdmissionResult, error) {
	ctx, cancel := context.WithTimeout(ctx, v.timeout)
	defer cancel()

	// Query A — tenant-labelled series. `metric{tenant!=""}` matches
	// series that DO carry the label; the TSDB index resolves it
	// without touching sample chunks. A non-empty result means the
	// metric is federatable, and the returned series is a real
	// tenant-level sample for the PII scan — so the heuristic sees
	// business labels, not a platform series picked by `limit`'s
	// arbitrary truncation.
	labelled, err := v.q.matchingSeries(ctx, metric+`{`+tenantLabel+`!=""}`, v.window, 1)
	if err != nil {
		return AdmissionResult{}, err
	}
	if len(labelled) > 0 {
		return AdmissionResult{
			Metric:    metric,
			State:     AdmissionPass,
			Reason:    fmt.Sprintf("metric %q has series carrying the %q label", metric, tenantLabel),
			PIILabels: scanPIILabels(labelled[0]),
		}, nil
	}

	// No tenant-labelled series. Query B — does the metric exist at
	// all? `limit=1` caps the response.
	any, err := v.q.matchingSeries(ctx, metric, v.window, 1)
	if err != nil {
		return AdmissionResult{}, err
	}
	if len(any) == 0 {
		return AdmissionResult{
			Metric: metric,
			State:  AdmissionWarn,
			Reason: fmt.Sprintf("metric %q has no samples in the last %s — legitimate for a cold-start or sparse metric, but unverifiable; re-submit with force to proceed", metric, v.window),
		}, nil
	}

	// The metric has data but NO series carries the tenant label —
	// federating it would return an empty vector for every tenant.
	return AdmissionResult{
		Metric: metric,
		State:  AdmissionHardBlock,
		Reason: fmt.Sprintf("metric %q has samples but no series carries a %q label; federating it would return an empty vector for every tenant — fix the scrape/relabel config first", metric, tenantLabel),
	}, nil
}

// piiLabelPatterns are case-insensitive substrings that, found in a
// metric's label NAMES, hint the metric may carry end-user PII.
// Federation moves data past the platform boundary, so a hit is
// surfaced for reviewer judgement (ADR-020 round-6 review). The list is
// deliberately conservative — bare `ip` / `user` match too many benign
// labels, so only qualified forms are listed.
var piiLabelPatterns = []string{
	"email", "mail", "phone", "ssn", "passport",
	"ip_addr", "ipaddr", "client_ip", "user_ip", "remote_addr",
	"username", "user_name", "customer", "account_name",
	"session", "token", "secret", "password", "credit_card",
}

// scanPIILabels returns the metric's label names that match the PII
// heuristic, sorted. `__name__` (the metric name itself) is skipped.
func scanPIILabels(labels map[string]string) []string {
	var hits []string
	for name := range labels {
		if name == "__name__" {
			continue
		}
		low := strings.ToLower(name)
		for _, pat := range piiLabelPatterns {
			if strings.Contains(low, pat) {
				hits = append(hits, name)
				break
			}
		}
	}
	sort.Strings(hits)
	return hits
}
