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
//     merged_hash moved (ADR-018 "quiet defaults edit"). v2.8.0 Issue #61
//     narrowed the semantics to *cosmetic-only* edits — shadowed cases now
//     leak into da_config_defaults_shadowed_total below.
//
//   da_config_defaults_shadowed_total      (Counter)  [v2.8.0, Issue #61]
//     incremented when a defaults file change *would* have moved a
//     tenant's effective config except every changed key is overridden by
//     that tenant's source YAML. Pre-RFC #61 these events were folded
//     into da_config_defaults_change_noop_total; they're split out so ops
//     can quantify how often the inheritance system blocks would-be blast.
//
//   da_config_blast_radius_tenants_affected (HistogramVec)  [v2.8.0, Issue #61]
//     labels = [reason, scope, effect]
//       reason ∈ {source, defaults, new, delete}    (forced is filtered: it
//                                                   maps to per-tenant
//                                                   reasons inside
//                                                   diffAndReload)
//       scope  ∈ {global, domain, region, env, tenant, unknown}
//       effect ∈ {applied, shadowed, cosmetic}
//     buckets = [1, 5, 25, 100, 500, 1000, 2500, 5000, 10000]
//     observed once per (reason, scope, effect) bucket per
//     diffAndReload tick, with N = tenants in that bucket.
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
	defaultsShadowed prometheus.Counter     // v2.8.0 Issue #61: shadowed defaults change (split from defaultsNoop)
	blastRadius      *prometheus.HistogramVec // v2.8.0 Issue #61: per-tick (reason,scope,effect) tenants-affected distribution
	reloadDuration   prometheus.Histogram   // v2.8.0 B-3: end-to-end diffAndReload elapsed (debounce window → atomic swap done)
	debounceBatch    prometheus.Histogram   // v2.8.0 B-3: count of triggers coalesced per fired window (debounce effectiveness)
	lastScanComplete   prometheus.Gauge // v2.8.0 B-1.P2-a: wall-clock unix seconds at most-recent successful scanDirHierarchical completion (e2e harness anchor T1; production stuck-detection)
	lastReloadComplete prometheus.Gauge // v2.8.0 B-1.P2-a: wall-clock unix seconds at most-recent successful diffAndReload completion (e2e harness anchor T2; production stuck-detection)
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
			Help: "Count of _defaults.yaml changes that did NOT move any dependent tenant's merged_hash AND were not shadowed by a tenant override — i.e. cosmetic edits (comment-only, key reordering, or unrelated-key change). v2.8.0 Issue #61 narrowed the semantics; shadowed cases now go to da_config_defaults_shadowed_total. Pre-2.8.0 dashboards reading this counter for 'how often did the inheritance system block changes' should switch to da_config_defaults_shadowed_total.",
		}),
		parseFailures: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "da_config_parse_failure_total",
			Help: "Count of per-file YAML parse failures during hierarchical scan (v2.8.0 A-8d). Label 'file_basename' lets ops pin down which tenant or defaults file is broken. Alert: >5/h for any single basename = page ops.",
		}, []string{"file_basename"}),
		defaultsShadowed: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "da_config_defaults_shadowed_total",
			Help: "Count of dependent tenants for whom a defaults change was effectively blocked because every changed key is overridden by that tenant's source YAML (v2.8.0 Issue #61, ADR-018 inheritance). Distinct from da_config_defaults_change_noop_total which counts cosmetic edits with no semantic key movement.",
		}),
		blastRadius: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name: "da_config_blast_radius_tenants_affected",
			Help: "Distribution of tenants affected per diffAndReload tick, grouped by (reason, scope, effect) (v2.8.0 Issue #61, RFC). reason=source/defaults/new/delete; scope=global/domain/region/env/tenant/unknown (widest changed defaults level for reason=defaults; tenant for source/new/delete); effect=applied (merged_hash moved) / shadowed (defaults change blocked by tenant override) / cosmetic (no semantic key change). Alert on histogram_quantile(0.99, sum by (le)(rate(...{effect=\"applied\"}_bucket[5m]))) > 500 for high-impact change detection.",
			// Buckets chosen to surface low-impact (1-5 affected) vs
			// catastrophic-blast (5000+) reloads. 2500/10000 added per
			// CHANGELOG sharding-decision: ≤2000 fine; 5000-10000 is the
			// optimization tier; >10000 is sharding territory.
			Buckets: []float64{1, 5, 25, 100, 500, 1000, 2500, 5000, 10000},
		}, []string{"reason", "scope", "effect"}),
		reloadDuration: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name: "da_config_reload_duration_seconds",
			Help: "End-to-end duration of diffAndReload (scan + per-tenant merge + blast-radius emit + fullDirLoad + atomic swap). Observed once per fired debounce window or once per synchronous fallback (debounceWindow=0). v2.8.0 B-3: feeds the empirical p99 used to validate the 300ms debounce floor and inform Phase 2 SLO sign-off.",
			// Buckets cover synthetic 1000-tenant baseline (~200ms p50,
			// ~500ms p99) + 5000-tenant tail (~1.1s) + headroom for
			// degraded FUSE / NFS mounts. 30s top-end exists so a
			// pathological reload does not silently saturate the last
			// bucket — operators want to see the actual tail.
			Buckets: []float64{0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30},
		}),
		debounceBatch: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name: "da_config_debounce_batch_size",
			Help: "Number of triggerDebouncedReload calls collapsed into a single fired window (v2.8.0 B-3 debounce effectiveness). Observed once per fireDebounced; sample count == fire count. p50 == 1 means debounce never coalesces (window may be too short or fsnotify storms are absent); p99 climbing past ~50 signals an event-storm pathology worth investigating.",
			// Bucket boundaries chosen to surface (a) the typical 1-2
			// case (single-file edits), (b) the K8s symlink-rotation
			// case (3-10 fsnotify events per ConfigMap update), and
			// (c) git-sync batch case (10-200 files in one rsync
			// burst — exactly the scenario B-7 stress-tests).
			Buckets: []float64{1, 2, 5, 10, 25, 50, 100, 250, 500},
		}),
		lastScanComplete: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "da_config_last_scan_complete_unixtime_seconds",
			Help: "Wall-clock unix seconds at the most recent successful scanDirHierarchical completion. Set by the scanner; read by the e2e harness as anchor T1 (B-1 Phase 2). Production use: alert on time() - <gauge> > N for stuck-scanner detection. 0 means scanner has not yet completed a successful scan.",
		}),
		lastReloadComplete: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "da_config_last_reload_complete_unixtime_seconds",
			Help: "Wall-clock unix seconds at the most recent successful diffAndReload completion (post atomic-swap). Set by the reload pipeline; read by the e2e harness as anchor T2 (B-1 Phase 2). Production use: alert on time() - <gauge> > N for stuck-reloader detection. 0 means reloader has not yet completed a successful reload.",
		}),
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

// registerConfigMetrics installs all metrics on the given registry.
// Called by MetricsHandler during /metrics wiring.
func registerConfigMetrics(reg prometheus.Registerer, m *configMetrics) {
	reg.MustRegister(m.scanDuration)
	reg.MustRegister(m.reloadTriggers)
	reg.MustRegister(m.defaultsNoop)
	reg.MustRegister(m.parseFailures)
	reg.MustRegister(m.defaultsShadowed)
	reg.MustRegister(m.blastRadius)
	reg.MustRegister(m.reloadDuration)
	reg.MustRegister(m.debounceBatch)
	reg.MustRegister(m.lastScanComplete)
	reg.MustRegister(m.lastReloadComplete)
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

// IncDefaultsShadowed bumps the shadowed-defaults counter — called once
// per dependent tenant whose merged_hash didn't move because every
// changed defaults key is overridden by that tenant's source YAML
// (v2.8.0 Issue #61). Distinct from IncDefaultsNoop, which now counts
// only cosmetic edits.
func IncDefaultsShadowed() {
	getConfigMetrics().defaultsShadowed.Inc()
}

// IncDefaultsShadowedBy bumps the shadowed counter by N for the batch
// case (mirror of IncDefaultsNoopBy).
func IncDefaultsShadowedBy(n int) {
	if n <= 0 {
		return
	}
	getConfigMetrics().defaultsShadowed.Add(float64(n))
}

// ObserveBlastRadius records one (reason, scope, effect) bucket
// observation for the blast-radius histogram. n is the count of
// tenants in this bucket for the current diffAndReload tick.
//
// n <= 0 is silently no-op (caller can pass an empty bucket without
// guarding) so the per-tick group-by emission loop in diffAndReload
// can iterate over a sparse map without conditional logic.
func ObserveBlastRadius(reason, scope, effect string, n int) {
	if n <= 0 {
		return
	}
	getConfigMetrics().blastRadius.WithLabelValues(reason, scope, effect).Observe(float64(n))
}

// ObserveReloadDuration records one diffAndReload elapsed-time sample
// (v2.8.0 B-3). Called from fireDebounced wrapper around diffAndReload
// and from the synchronous-fallback path in triggerDebouncedReload so
// every reload contributes one sample regardless of debounce mode.
func ObserveReloadDuration(d time.Duration) {
	getConfigMetrics().reloadDuration.Observe(d.Seconds())
}

// ObserveDebounceBatch records one debounce-window batch-size sample
// (v2.8.0 B-3). Called once per fireDebounced with the count of triggers
// collapsed into the window. Synchronous fallback (debounceWindow=0)
// does NOT contribute to this histogram — it has no batching semantics
// to observe, and folding "1" samples in would skew the p50 baseline
// that ops use to detect debounce regressions.
func ObserveDebounceBatch(n int) {
	if n < 0 {
		return
	}
	getConfigMetrics().debounceBatch.Observe(float64(n))
}

// SetLastScanComplete records the wall-clock unix seconds at successful
// scanDirHierarchical completion (v2.8.0 B-1.P2-a). The e2e harness reads
// this gauge as anchor T1 in the 5-anchor measurement model; production
// uses `time() - <gauge>` for stuck-scanner alerting.
//
// Called only on success — error paths leave the gauge at its previous
// value so a transient scan failure does not look like a successful
// completion. Tests that want a clean baseline observe via withIsolatedMetrics.
func SetLastScanComplete(t time.Time) {
	getConfigMetrics().lastScanComplete.Set(float64(t.Unix()))
}

// SetLastReloadComplete records the wall-clock unix seconds at the
// successful diffAndReload completion (post atomic-swap, v2.8.0 B-1.P2-a).
// E2E harness reads this gauge as anchor T2.
//
// Called only on success path — see SetLastScanComplete docstring.
func SetLastReloadComplete(t time.Time) {
	getConfigMetrics().lastReloadComplete.Set(float64(t.Unix()))
}
