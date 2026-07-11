package handler

// Config reload observability (Gemini #1056 external-review disposition 3a +
// its follow-up: the current-state gauge).
//
// Every tenant-api YAML config manager (rbac / groups / views / policy /
// tenantorg / federation-policy) embeds configwatcher.Watcher, which keeps
// serving the LAST-GOOD snapshot when a reload parse fails — so an admin's typo
// in _rbac.yaml / _domain_policy.yaml / etc. silently stops taking effect. Both
// reload paths are silent (WatchLoop only WARN-logs; the post-write Reload's
// error is discarded by every caller before answering 200 OK).
//
// The Watcher reports the outcome of EVERY load through the
// configwatcher.ReloadObserver interface; the concrete store lives here (the
// handler package owns /metrics exposition) and is injected into each Manager at
// wiring time via SetReloadObserver — instance-method DI, mirroring
// scope_metrics.go, so metric state is not a bare package singleton and tests
// can assert against their own instance. It drives TWO complementary metrics:
//
//   - tenant_api_config_reload_failures_total{component} — monotonic COUNTER,
//     reload failure RATE (dashboards / trend).
//   - tenant_api_config_last_reload_successful{component} — 0/1 GAUGE, current
//     STATE ("is this manager serving stale config right now?"). This is what
//     the counter structurally cannot answer: a single-shot Reload failure
//     (groups/views never retry via a WatchLoop) leaves the counter at 1, below
//     any `increase()>N` threshold, yet the config IS stale until the next
//     successful reload. The gauge flips to 0 and STAYS 0 until a load succeeds,
//     so `== 0` alerts regardless of retry cadence. Mirrors Prometheus's own
//     prometheus_config_last_reload_successful.
//
// Import direction: handler → configwatcher (a leaf package). configwatcher
// never imports handler, so the observer INTERFACE is declared in configwatcher
// and IMPLEMENTED here.

import (
	"fmt"
	"io"
	"sort"
	"sync/atomic"

	"github.com/vencil/tenant-api/internal/configwatcher"
)

// configReloadComponents is the fixed, known set of component labels for both
// config-reload metric families. Fixing it means every series is emitted from
// process start so a dashboard/alert never sees a missing series, and it bounds
// cardinality (no user-controlled label values). The values MUST match the label
// each Watcher carries (the string passed to configwatcher.New) — that string is
// what RecordReload receives.
//
// Membership = EVERY config manager, because BOTH reload paths mask a failure
// silently and the Watcher records both (configwatcher.WatchLoop and
// configwatcher.Reload):
//   - rbac / policy / tenantorg / federation-policy run a periodic WatchLoop.
//   - groups / views (and federation-policy again) are Reload()ed after a write,
//     and every production caller DISCARDS that error — `_ = mgr.Reload()` in
//     handler/group.go:236,317, handler/view.go:175,239 and
//     handler/federation/policy.go:157 — then answers 200 OK. Equally silent.
//
// A new manager appends its label here in the same commit that adds it. This is
// an ARRAY, not a slice, so the `counters` / `states` stores below are sized FROM
// it at compile time: appending a label here grows both automatically, and they
// can never drift into a silent mis-index or an out-of-range panic.
//
// Note the casing wart: rbac's label is "RBAC" (upper) while the others are
// lower — this mirrors the existing log-tag values verbatim rather than
// introducing a metric-vs-log drift.
var configReloadComponents = [...]string{
	"RBAC",
	"policy",
	"tenantorg",
	"federation-policy",
	"groups",
	"views",
}

// ConfigReloadMetrics holds the per-component reload-failure counter and the
// current last-reload-successful gauge. It satisfies configwatcher.ReloadObserver.
// Both stores are atomic so recording is lock-free and safe from both writer
// goroutines: each Watcher's WatchLoop ticker and the request goroutines that
// call Reload after a write.
type ConfigReloadMetrics struct {
	// counters and states are fixed-size parallel arrays to configReloadComponents,
	// sized FROM it at compile time (len of an array value is a constant
	// expression), so they can never drift out of sync with the label set.
	counters [len(configReloadComponents)]atomic.Int64 // monotonic failure count
	states   [len(configReloadComponents)]atomic.Int64 // 1 = last reload ok, 0 = failed
}

// RecordReload implements configwatcher.ReloadObserver: it records the outcome of
// one reload attempt. On failure it bumps the monotonic counter; on every attempt
// it sets the current-state gauge (1 ok, 0 failed). An unrecognized component
// label is ignored (defensive — a manager whose label is not in
// configReloadComponents would be a wiring bug, but a stray label must never
// panic the reload goroutine).
func (m *ConfigReloadMetrics) RecordReload(component string, ok bool) {
	for i, c := range configReloadComponents {
		if c == component {
			if !ok {
				m.counters[i].Add(1)
				m.states[i].Store(0)
			} else {
				m.states[i].Store(1)
			}
			return
		}
	}
}

// Snapshot returns the current failure-counter values keyed by component label.
func (m *ConfigReloadMetrics) Snapshot() map[string]int64 {
	out := make(map[string]int64, len(configReloadComponents))
	for i, c := range configReloadComponents {
		out[c] = m.counters[i].Load()
	}
	return out
}

// StateSnapshot returns the current last-reload-successful gauge values (1/0)
// keyed by component label.
func (m *ConfigReloadMetrics) StateSnapshot() map[string]int64 {
	out := make(map[string]int64, len(configReloadComponents))
	for i, c := range configReloadComponents {
		out[c] = m.states[i].Load()
	}
	return out
}

// Compile-time assertion that ConfigReloadMetrics satisfies the sink.
var _ configwatcher.ReloadObserver = (*ConfigReloadMetrics)(nil)

// activeConfigReloadMetrics holds the most-recently installed store so /metrics
// can render it without threading it through Deps. Mirrors activeScopeWouldDeny.
// There is one store in production (shared across all managers); tests that want
// isolation construct their own ConfigReloadMetrics and read it via Snapshot().
var activeConfigReloadMetrics atomic.Pointer[ConfigReloadMetrics]

// NewConfigReloadObserver constructs a fresh store, initializes every
// last-reload-successful gauge to 1 (assumed-current until a load says
// otherwise), registers it as the one /metrics renders, and returns it as a
// configwatcher.ReloadObserver for injection into every config manager via
// SetReloadObserver. Called once at startup, unconditionally (the metrics are
// part of the base reload path, not an opt-in feature), so both families are
// always present.
func NewConfigReloadObserver() configwatcher.ReloadObserver {
	m := &ConfigReloadMetrics{}
	for i := range m.states {
		m.states[i].Store(1)
	}
	activeConfigReloadMetrics.Store(m)
	return m
}

// writeConfigReloadFailureMetrics renders the
// tenant_api_config_reload_failures_total counter family. When no store is
// installed all series are still emitted at 0 so the metric's presence is stable.
func writeConfigReloadFailureMetrics(w io.Writer) {
	var snap map[string]int64
	if m := activeConfigReloadMetrics.Load(); m != nil {
		snap = m.Snapshot()
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_config_reload_failures_total Config reload failures by manager, from either reload path (periodic WatchLoop tick or post-write Reload). On failure the manager keeps serving the LAST-GOOD snapshot, so the edited config silently does not take effect — alert on a sustained increase() (monotonic counter).\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_config_reload_failures_total counter\n")
	for _, c := range sortedComponents() {
		_, _ = fmt.Fprintf(w, "tenant_api_config_reload_failures_total{component=%q} %d\n", c, snap[c])
	}
}

// writeConfigReloadStateMetrics renders the
// tenant_api_config_last_reload_successful gauge family. When no store is
// installed all series are emitted at 1 (assumed-current), so the metric's
// presence is stable and a never-reloaded deployment does not false-alarm.
func writeConfigReloadStateMetrics(w io.Writer) {
	snap := map[string]int64{}
	if m := activeConfigReloadMetrics.Load(); m != nil {
		snap = m.StateSnapshot()
	} else {
		for _, c := range configReloadComponents {
			snap[c] = 1
		}
	}
	_, _ = fmt.Fprintf(w, "# HELP tenant_api_config_last_reload_successful Whether each config manager's LAST reload succeeded (1) or failed and is serving stale last-good config (0). Unlike the failure counter this catches a single-shot Reload failure (groups/views do not retry) and stays 0 until a load succeeds — alert on == 0.\n")
	_, _ = fmt.Fprintf(w, "# TYPE tenant_api_config_last_reload_successful gauge\n")
	for _, c := range sortedComponents() {
		_, _ = fmt.Fprintf(w, "tenant_api_config_last_reload_successful{component=%q} %d\n", c, snap[c])
	}
}

// sortedComponents returns the component labels in deterministic order for stable
// exposition / golden tests.
func sortedComponents() []string {
	comps := make([]string, len(configReloadComponents))
	copy(comps, configReloadComponents[:])
	sort.Strings(comps)
	return comps
}
