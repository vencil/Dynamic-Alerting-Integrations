package handler

// Config hot-reload failure metric (Gemini #1056 external-review disposition 3a).
//
// Every tenant-api YAML config manager (rbac / groups / views / policy /
// federation-policy) embeds configwatcher.Watcher, whose WatchLoop keeps
// serving the LAST-GOOD snapshot when a hot-reload parse fails — so an admin's
// typo in _rbac.yaml / _domain_policy.yaml / etc. silently stops taking effect,
// traceable today only via a WARN log. The Watcher records each such failure
// through the configwatcher.ReloadFailureRecorder interface; the concrete
// counter store lives here (the handler package owns /metrics exposition) and is
// injected into each Manager at wiring time via SetReloadFailureRecorder —
// instance-method DI, mirroring scope_metrics.go / the identity-audit recorder,
// so metric state is not a bare package singleton and tests can assert against
// their own instance.
//
// Import direction: handler → configwatcher (a leaf package). configwatcher
// never imports handler, so the recorder INTERFACE is declared in configwatcher
// and IMPLEMENTED here.

import (
	"fmt"
	"io"
	"sort"
	"sync/atomic"

	"github.com/vencil/tenant-api/internal/configwatcher"
)

// configReloadComponents is the fixed, known set of component labels for
// tenant_api_config_reload_failures_total{component}. Fixing it means every
// series is emitted from process start (value 0) so a dashboard/alert never
// sees a missing series, and it bounds cardinality (no user-controlled label
// values). The values MUST match the label each Watcher carries (the string
// passed to configwatcher.New) — that string is what IncReloadFailure receives.
//
// Membership = exactly the managers that run a periodic WatchLoop, because the
// counter records the SILENT masking path: WatchLoop logs a WARN and keeps
// last-good on a parse error, so the edited config quietly does not take
// effect. groups / views are intentionally EXCLUDED — they never WatchLoop;
// they Reload() only after a write, and that error is returned to the handler
// (surfaced to the caller, not silently masked), a different class this metric
// does not claim to cover. A new WatchLoop-driven manager appends its label
// here in the same commit that adds it (mirrors scope_metrics.go's
// scopeWouldDenyAxes; tenantorg — the LD-6 P4a admin-org manager — follows the
// policy WatchLoop pattern and is included below).
//
// Note the casing wart: rbac's label is "RBAC" (upper) while the others are
// lower — this mirrors the existing log-tag values verbatim rather than
// introducing a metric-vs-log drift.
var configReloadComponents = []string{
	"RBAC",
	"policy",
	"tenantorg",
	"federation-policy",
}

// ConfigReloadFailureMetrics holds the per-component reload-failure counters.
// It satisfies configwatcher.ReloadFailureRecorder. Counters are atomic so
// recording (which runs on each Watcher's WatchLoop goroutine) is lock-free.
type ConfigReloadFailureMetrics struct {
	// counters is a fixed-size parallel array to configReloadComponents.
	counters [4]atomic.Int64
}

// IncReloadFailure implements configwatcher.ReloadFailureRecorder. An
// unrecognized component label is ignored (defensive — a manager whose label
// is not in configReloadComponents would be a wiring bug, but we never want a
// stray label to panic the reload goroutine).
func (m *ConfigReloadFailureMetrics) IncReloadFailure(component string) {
	for i, c := range configReloadComponents {
		if c == component {
			m.counters[i].Add(1)
			return
		}
	}
}

// Snapshot returns the current counter values keyed by component label. Used by
// /metrics exposition and by tests asserting on their own instance.
func (m *ConfigReloadFailureMetrics) Snapshot() map[string]int64 {
	out := make(map[string]int64, len(configReloadComponents))
	for i, c := range configReloadComponents {
		out[c] = m.counters[i].Load()
	}
	return out
}

// Compile-time assertion that ConfigReloadFailureMetrics satisfies the sink.
var _ configwatcher.ReloadFailureRecorder = (*ConfigReloadFailureMetrics)(nil)

// activeConfigReloadFailure holds the most-recently installed store so /metrics
// can render it without threading it through Deps. Mirrors activeScopeWouldDeny.
// There is one recorder in production (shared across all managers); tests that
// want isolation construct their own ConfigReloadFailureMetrics and read it via
// Snapshot().
var activeConfigReloadFailure atomic.Pointer[ConfigReloadFailureMetrics]

// NewConfigReloadFailureRecorder constructs a fresh reload-failure store,
// registers it as the one /metrics renders, and returns it as a
// configwatcher.ReloadFailureRecorder for injection into every config manager
// via SetReloadFailureRecorder. Called once at startup, unconditionally (the
// metric is part of the base hot-reload path, not an opt-in feature), so the
// counter family is always present.
func NewConfigReloadFailureRecorder() configwatcher.ReloadFailureRecorder {
	m := &ConfigReloadFailureMetrics{}
	activeConfigReloadFailure.Store(m)
	return m
}

// writeConfigReloadFailureMetrics renders the
// tenant_api_config_reload_failures_total counter family in Prometheus
// exposition format. When no recorder is installed all series are still emitted
// at 0 so the metric's presence is stable.
func writeConfigReloadFailureMetrics(w io.Writer) {
	var snap map[string]int64
	if m := activeConfigReloadFailure.Load(); m != nil {
		snap = m.Snapshot()
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_config_reload_failures_total Hot-reload (WatchLoop) parse/read failures by config manager. On failure the manager keeps serving the LAST-GOOD snapshot, so the edited config silently does not take effect — alert on increase()>0 (monotonic counter).\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_config_reload_failures_total counter\n")
	// Deterministic order for stable exposition / golden tests.
	comps := make([]string, len(configReloadComponents))
	copy(comps, configReloadComponents)
	sort.Strings(comps)
	for _, c := range comps {
		_, _ = fmt.Fprintf(w, "tenant_api_config_reload_failures_total{component=%q} %d\n", c, snap[c])
	}
}
