package main

// ============================================================
// Hierarchical scan + reload metrics (v2.7.0 Phase 4)
// ============================================================
//
// Three metrics exposed in addition to the per-scrape collector output:
//
//   da_config_scan_duration_seconds        (Histogram)
//     buckets 1ms, 5ms, 10ms, 50ms, 100ms, 500ms, 1s, 5s
//     observed by scanDirHierarchical via prometheus.NewTimer(...).ObserveDuration()
//
//   da_config_reload_trigger_total         (CounterVec, labels=[reason])
//     reason ∈ {source, defaults, new, delete, forced}
//     incremented by diffAndReload per-tenant classification.
//
//   da_config_defaults_change_noop_total   (Counter)
//     incremented when a defaults file changed but no dependent tenant's
//     merged_hash moved (ADR-018 "quiet defaults edit").
//
// These metrics are defined as package-level state (not emitted by the
// ThresholdCollector.Collect path) because they carry cumulative state —
// a scrape reads the running totals. The custom collector is still needed
// for the dynamic-label user_threshold etc.; the standard registry
// handles the counters/histograms below.
//
// Why a separate registration function instead of init():
//   - Makes the registration explicit at the call site (MetricsHandler)
//     which is the sole owner of the prometheus.Registry lifecycle.
//   - Lets tests freely create+discard metrics without polluting
//     DefaultRegisterer with duplicate-registration panics.
//   - Survives parallel t.Run (the "unique registry per test" pattern is
//     the documented escape hatch for TestMain-free packages).

import (
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// configMetrics bundles the three Phase 4 metrics. A single-instance
// singleton is allocated lazily on first MustRegister so tests can reset
// state by re-instantiating via newConfigMetrics.
type configMetrics struct {
	scanDuration     prometheus.Histogram
	reloadTriggers   *prometheus.CounterVec
	defaultsNoop     prometheus.Counter
	parseFailures    *prometheus.CounterVec // v2.8.0 A-8d: per-file YAML parse failures
}

// Default metric instance used by the production server. Tests that want
// isolation construct a fresh instance via newConfigMetrics() and install
// it via setConfigMetrics before exercising scan paths.
var (
	configMetricsOnce sync.Once
	configMetricsInst *configMetrics
	configMetricsMu   sync.RWMutex
)

// newConfigMetrics builds a fresh set of metrics without registering them.
// Callers must MustRegister on an isolated prometheus.Registry.
func newConfigMetrics() *configMetrics {
	return &configMetrics{
		scanDuration: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name: "da_config_scan_duration_seconds",
			Help: "Duration of a hierarchical conf.d scan (v2.7.0, ADR-017). Observed once per scanDirHierarchical call.",
			// Buckets tuned for 1000-tenant scans on ext4 (p50 ~20ms, p99
			// ~150ms in the benchmark) plus slack for FUSE/NFS mounts.
			Buckets: []float64{0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5},
		}),
		reloadTriggers: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "da_config_reload_trigger_total",
			Help: "Count of hierarchical reloads, labeled by the change that triggered them (source, defaults, new, delete, forced).",
		}, []string{"reason"}),
		defaultsNoop: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "da_config_defaults_change_noop_total",
			Help: "Count of _defaults.yaml changes that did NOT move any dependent tenant's merged_hash (ADR-018 'quiet defaults edit').",
		}),
		parseFailures: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "da_config_parse_failure_total",
			Help: "Count of per-file YAML parse failures during hierarchical scan (v2.8.0 A-8d). Label 'file_basename' lets ops pin down which tenant or defaults file is broken. Alert: >5/h for any single basename = page ops.",
		}, []string{"file_basename"}),
	}
}

// getConfigMetrics returns the active instance, allocating the default on
// first use. Safe for concurrent access.
func getConfigMetrics() *configMetrics {
	configMetricsMu.RLock()
	inst := configMetricsInst
	configMetricsMu.RUnlock()
	if inst != nil {
		return inst
	}
	configMetricsOnce.Do(func() {
		configMetricsMu.Lock()
		if configMetricsInst == nil {
			configMetricsInst = newConfigMetrics()
		}
		configMetricsMu.Unlock()
	})
	configMetricsMu.RLock()
	inst = configMetricsInst
	configMetricsMu.RUnlock()
	return inst
}

// setConfigMetrics swaps the active instance. Intended for tests that
// need a fresh counter/histogram set per parallel run. Production code
// should not call this.
func setConfigMetrics(m *configMetrics) {
	configMetricsMu.Lock()
	configMetricsInst = m
	configMetricsMu.Unlock()
}

// registerConfigMetrics installs all three metrics on the given registry.
// Called by MetricsHandler during /metrics wiring.
func registerConfigMetrics(reg prometheus.Registerer, m *configMetrics) {
	reg.MustRegister(m.scanDuration)
	reg.MustRegister(m.reloadTriggers)
	reg.MustRegister(m.defaultsNoop)
	reg.MustRegister(m.parseFailures)
}

// IncParseFailure bumps the parse-failure counter for a specific file
// basename. Called from scanDirHierarchical whenever yaml.Unmarshal
// returns an error for a non-_-prefixed tenant file. file_basename
// (not full path) is used as the label to keep cardinality bounded
// in practice — same tenant name across domains sums to one series.
// v2.8.0 A-8d (Issue #52-adjacent observability gap from Gemini R3).
func IncParseFailure(fileBasename string) {
	getConfigMetrics().parseFailures.WithLabelValues(fileBasename).Inc()
}

// ObserveScanDuration starts a timer and returns a stop function that
// records the elapsed time into da_config_scan_duration_seconds. Idiomatic
// use:
//
//	defer ObserveScanDuration()()
//
// Returns the "stop" closure so the caller can also record duration
// manually when needed (e.g., for log correlation). Using time.Since
// directly (vs. prometheus.NewTimer) lets us share the t0 for both the
// metric and the debug log without double-observing.
func ObserveScanDuration() func() {
	t0 := time.Now()
	return func() {
		getConfigMetrics().scanDuration.Observe(time.Since(t0).Seconds())
	}
}

// IncReloadTrigger bumps the reload counter for the given reason. Safe
// to call with reasons not in the canonical set — Prometheus CounterVec
// will happily create a new label value (operator can watch for drift
// from the documented set in config_debounce.go).
func IncReloadTrigger(reason string) {
	getConfigMetrics().reloadTriggers.WithLabelValues(reason).Inc()
}

// IncReloadTriggerBy bumps the counter by N (for the batch case where
// diffAndReload reloads k tenants all with the same reason).
func IncReloadTriggerBy(reason string, n int) {
	if n <= 0 {
		return
	}
	getConfigMetrics().reloadTriggers.WithLabelValues(reason).Add(float64(n))
}

// IncDefaultsNoop bumps the no-op counter. Called once per dependent
// tenant whose merged_hash didn't move after a defaults file changed.
func IncDefaultsNoop() {
	getConfigMetrics().defaultsNoop.Inc()
}

// IncDefaultsNoopBy bumps the no-op counter by N. Used by diffAndReload
// which computes the batch size without per-tenant allocations.
func IncDefaultsNoopBy(n int) {
	if n <= 0 {
		return
	}
	getConfigMetrics().defaultsNoop.Add(float64(n))
}
