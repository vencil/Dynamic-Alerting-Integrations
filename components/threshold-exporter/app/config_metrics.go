package main

// ============================================================
// Hierarchical scan + reload metrics (v2.7.0 Phase 4)
// ============================================================
//
// Three metrics exposed in addition to the per-scrape collector output:
//
//   da_config_scan_duration_seconds        (Histogram)
//     buckets 1ms, 5ms, 10ms, 50ms, 100ms, 500ms, 1s, 5s,
//             10s, 30s, 60s, 120s (the last four since #2153)
//     observed once per conf.d tree scan (scanDirTree, both planes; #1568)
//
//   da_config_reload_trigger_total         (CounterVec, labels=[reason])
//     reason ∈ {source, defaults, new, delete, forced}
//     incremented by diffAndReload per-tenant classification.
//
//   da_config_scan_failures_total          (CounterVec, labels=[reason])  [#2452]
//     reason ∈ {duplicate_tenant, walk_error} (closed; classifyScanFailure)
//     incremented when a watch-path scan fails — the case that applies
//     nothing and so never moves da_config_reload_trigger_total.
//
//   da_config_defaults_change_noop_total   (Counter)
//     incremented when a defaults file changed but no dependent tenant's
//     merged_hash moved (ADR-017 "quiet defaults edit"). v2.8.0 Issue #61
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
	"errors"
	"log"
	"strings"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/internal/scrape"
)

// configMetrics bundles the three Phase 4 metrics. A single-instance
// singleton is allocated lazily on first MustRegister so tests can reset
// state by re-instantiating via newConfigMetrics.
type configMetrics struct {
	// set is the same metrics as the fields below, owned by internal/scrape
	// (#2115); the fields alias its members for the methods and tests here.
	set *scrape.ConfigMetrics

	scanDuration       prometheus.Histogram
	reloadTriggers     *prometheus.CounterVec
	defaultsNoop       prometheus.Counter
	parseFailures      *prometheus.CounterVec   // v2.8.0 A-8d: per-file YAML parse failures
	defaultsShadowed   prometheus.Counter       // v2.8.0 Issue #61: shadowed defaults change (split from defaultsNoop)
	blastRadius        *prometheus.HistogramVec // v2.8.0 Issue #61: per-tick (reason,scope,effect) tenants-affected distribution
	reloadDuration     prometheus.Histogram     // v2.8.0 B-3: end-to-end diffAndReload elapsed (debounce window → atomic swap done)
	debounceBatch      prometheus.Histogram     // v2.8.0 B-3: count of triggers coalesced per fired window (debounce effectiveness)
	lastScanComplete   prometheus.Gauge         // v2.8.0 B-1.P2-a: wall-clock unix seconds at most-recent successful conf.d tree scan completion (e2e harness anchor T1; production stuck-detection)
	lastReloadComplete prometheus.Gauge         // v2.8.0 B-1.P2-a: wall-clock unix seconds at most-recent successful diffAndReload completion (e2e harness anchor T2; production stuck-detection)
	freeOSMemory       prometheus.Counter       // #459: count of explicit runtime/debug.FreeOSMemory() calls after reload (opt-in -free-os-mem-after-reload; 0 when lever disabled)
	// State-coded gauge: tenants that inherit a key existing ONLY in a
	// subtree `_defaults.yaml`, which /effective reports and the collector
	// cannot emit (#1976; was the #1521 divergence gauge until #1957 removed
	// its other cause). Gauge, not counter: the value is the CURRENT size of
	// the set, so declaring the key at the root drives it back to 0. A
	// counter could only ever say "it happened N times", where N tracks
	// reload frequency rather than misconfiguration severity. Set on every
	// commitConfig — see config_subtree_undeliverable.go.
	subtreeUndeliverableTenants prometheus.Gauge
	// #2153 (D): the two size axes that drive the cold-load parse cost —
	// the widest `tenants:` block in any one file, and the largest key
	// count of any one mapping the flat plane decodes. Whole-tree maxima,
	// NOT per-file series: a conf.d tree can hold thousands of files, and
	// a file-name label would make the gauge's cardinality track the tree.
	// Re-Set on every config commit (see config_shape.go).
	maxTenantsPerFile prometheus.Gauge
	maxMappingKeys    prometheus.Gauge
	// #2153 (D): wall-clock seconds of the startup load (LoadInitial). A
	// gauge, not a sample in reloadDuration: the startup load happens once
	// per process and is a different operation (cold scan, every
	// merged_hash) from the debounced reload that histogram's p99 describes.
	initialLoadDuration prometheus.Gauge
	// #2452: failed conf.d tree scans on the watch path, labelled by
	// ScanFailureReason*. See IncScanFailure.
	scanFailures *prometheus.CounterVec
}

// da_config_scan_failures_total{reason} label values (#2452). ⛔ A CLOSED
// SET — classifyScanFailure maps every scan error onto one of these two, so
// the counter has at most two series whatever the error text says (the
// error names files and tenant ids; none of that reaches a label).
//
// The split follows the only two ways config.ScanDirTree can fail:
//   - duplicate_tenant: the walk succeeded but one tenant id is declared in
//     two files (*config.DuplicateTenantError, TreeScan.Conflict). Counted
//     in BOTH directory modes. With a root _defaults.yaml it is detectChange
//     that fails, every tick (tickOnce logs `WARN: cannot check config …:
//     hierarchical scan: duplicate tenant ID …`). In flat mode detectChange
//     ignores the Conflict, sees the tree as changed and schedules a reload,
//     and it is the reload's scan (scanAndCheckHierarchical) that rejects
//     the Conflict before the flat branch — logging `ERROR: hierarchical
//     scan failed: duplicate tenant ID …` then `ERROR: debounced reload
//     failed: …` (`synchronous reload failed` at -scan-debounce=0). Either
//     way the tree is frozen at the last good config. (Making the flat
//     plane's own IncrementalLoad run that check is #1577.)
//   - walk_error: every other error ScanDirTree returns — the root cannot
//     be statted, is not a directory, or the walk itself errors. Per-file
//     stat / read / parse problems are NOT scan failures: the walker logs
//     them and drops the file (parse failures have their own counter,
//     da_config_parse_failure_total).
const (
	ScanFailureReasonDuplicateTenant = "duplicate_tenant"
	ScanFailureReasonWalkError       = "walk_error"
)

// classifyScanFailure maps a scan error onto the closed reason set above.
func classifyScanFailure(err error) string {
	var dup *DuplicateTenantError
	if errors.As(err, &dup) {
		return ScanFailureReasonDuplicateTenant
	}
	return ScanFailureReasonWalkError
}

// Default metric instance used by the production server. Tests that want
// isolation construct a fresh instance via newConfigMetrics() (or the
// freshMetrics test helper) and inject it on the consumer via
// ConfigManager.SetMetrics or scanDirTree's metrics parameter — see #4a
// for the global-swap migration. setConfigMetrics was removed; reading
// from the singleton via getConfigMetrics is the only legitimate
// production access path.
var (
	configMetricsOnce sync.Once
	configMetricsInst *configMetrics
)

// newConfigMetrics builds a fresh set of metrics without registering them.
// Callers must MustRegister on an isolated prometheus.Registry.
func newConfigMetrics() *configMetrics {
	s := scrape.NewConfigMetrics()
	// #2452: both reasons exist at 0 from the first scrape, so the first
	// failure is an increase() Prometheus can see (a series born at 1 has
	// no earlier sample to rise from) and a healthy exporter shows 0.
	for _, r := range []string{ScanFailureReasonDuplicateTenant, ScanFailureReasonWalkError} {
		s.ScanFailures.WithLabelValues(r)
	}
	return &configMetrics{
		set:                         s,
		scanDuration:                s.ScanDuration,
		reloadTriggers:              s.ReloadTriggers,
		defaultsNoop:                s.DefaultsNoop,
		parseFailures:               s.ParseFailures,
		defaultsShadowed:            s.DefaultsShadowed,
		blastRadius:                 s.BlastRadius,
		reloadDuration:              s.ReloadDuration,
		debounceBatch:               s.DebounceBatch,
		lastScanComplete:            s.LastScanComplete,
		lastReloadComplete:          s.LastReloadComplete,
		freeOSMemory:                s.FreeOSMemory,
		subtreeUndeliverableTenants: s.SubtreeUndeliverableTenants,
		maxTenantsPerFile:           s.MaxTenantsPerFile,
		maxMappingKeys:              s.MaxMappingKeys,
		initialLoadDuration:         s.InitialLoadDuration,
		scanFailures:                s.ScanFailures,
	}
}

// getConfigMetrics returns the active instance, allocating the default on
// first use. Safe for concurrent access: sync.Once guarantees the write in
// Do happens-before every return, so no additional mutex is needed (the
// instance is never reassigned — setConfigMetrics was removed in #4a).
func getConfigMetrics() *configMetrics {
	configMetricsOnce.Do(func() {
		configMetricsInst = newConfigMetrics()
	})
	return configMetricsInst
}

// registerConfigMetrics installs all metrics on the given registry.
// Called by MetricsHandler during /metrics wiring.
func registerConfigMetrics(reg prometheus.Registerer, m *configMetrics) {
	for _, x := range m.set.Collectors() {
		reg.MustRegister(x)
	}
}

// ─────────────────────────────────────────────────────────────────────
// Method-form helpers on *configMetrics (#4a).
//
// Every metric mutation goes through a method on the receiver so the
// caller bumps a specific instance's counter with no global indirection.
// The two consumers are: (1) ConfigManager, which holds its own
// *configMetrics field and reaches it via m.getMetrics() — tests inject a
// fresh instance via SetMetrics and assert against it without racing the
// package-level singleton; (2) the tree walker (scanDirTree) and the flat
// parse helpers, which receive a *configMetrics parameter that production
// wiring fills from m.getMetrics() and tests fill with freshMetrics.
//
// There are deliberately NO top-level singleton wrapper functions here.
// An earlier revision kept a `func Name(...) { getConfigMetrics().Name() }`
// twin per method; they were removed in #4a because writing to the global
// singleton bypasses the injection seam — collector_test.go documents a
// real bug where the first Collect used the top-level PublishTenantMetrics-
// OverLimit form and silently wrote past the injected instance. Reading the
// singleton is confined to the wiring call sites above; mutation is
// method-only so the footgun cannot recur.
// ─────────────────────────────────────────────────────────────────────

// IncParseFailure bumps the parse-failure counter for a specific file
// basename. Call sites and the resulting unit (#1957):
//   - the tree scan (pkg/config parseTenantDecls, via config.ScanObserver):
//     a non-_-prefixed tenant file the one decode (config.ParseConfigFile)
//     rejects — ONCE per scan; the flat plane reuses that verdict and does
//     not count again;
//   - the flat parse (parsePartialConfig) or the nested syntax probe
//     (reportUnparseableNestedPlatformFile): a `_`-prefixed file, which the
//     walker never parses — once per flat commit;
//   - emitParseFailureSignal (config_debounce.go): a broken defaults file in
//     a tenant's chain, ONCE PER AFFECTED TENANT on every merged_hash
//     recompute — intentional, the count is the blast radius (a broken root
//     `_defaults.yaml` above 3 tenants reads 4 after one cold Load; pinned by
//     TestADefaultsParseFailureCountsOncePlusOncePerTenant).
//
// ⚠️ "Per scan" is not "per reload": a watch tick that detects a change scans
// twice (detectChange, then the debounced reload), so a broken tenant file
// moves the counter by 2 on such a tick. file_basename
// (not full path) is used as the label to keep cardinality bounded
// in practice — same tenant name across domains sums to one series.
// v2.8.0 A-8d (Issue #52-adjacent observability gap from Gemini R3).
//
// ⛔ NIL-RECEIVER SAFE, like the other two config.ScanObserver methods
// (ObserveScanElapsed, SetLastScanComplete). It is the second line of
// defence against the typed-nil trap: scanObserverFor already turns a nil
// *configMetrics into a true nil interface, but pkg/config is imported by
// other modules and a caller that skips that conversion must get "nothing
// counted", not a panic. Pinned by TestScanDirTree_NilConfigMetrics.
func (cm *configMetrics) IncParseFailure(fileBasename string) {
	if cm == nil {
		return
	}
	// A conf.d file name is data, and Linux allows any bytes in it; a label
	// value that is not valid UTF-8 makes WithLabelValues panic (#2266). The
	// sanitised name still counts the failure; U+FFFD marks the bad bytes.
	c, err := cm.parseFailures.GetMetricWithLabelValues(strings.ToValidUTF8(fileBasename, "\uFFFD"))
	if err != nil {
		log.Printf("WARN: da_config_parse_failure_total not incremented for file %q: %v", fileBasename, err)
		return
	}
	c.Inc()
}

// ObserveScanElapsed records one already-measured scan duration into
// da_config_scan_duration_seconds. It is the config.ScanObserver method the
// conf.d walker (pkg/config.ScanDirTree, #1941) calls: the walker takes its
// own t0 so no closure crosses the interface — a returned closure escaped
// to the heap there and cost one alloc per scan on the fast path.
// Nil-receiver safe (see IncParseFailure).
func (cm *configMetrics) ObserveScanElapsed(d time.Duration) {
	if cm == nil {
		return
	}
	cm.scanDuration.Observe(d.Seconds())
}

// IncReloadTrigger bumps the reload counter for the given reason. Safe
// to call with reasons not in the canonical set — Prometheus CounterVec
// will happily create a new label value (operator can watch for drift
// from the documented set in config_debounce.go).
func (cm *configMetrics) IncReloadTrigger(reason string) {
	cm.reloadTriggers.WithLabelValues(reason).Inc()
}

// IncScanFailure counts one failed conf.d tree scan on the watch path
// (#2452), under the reason classifyScanFailure gives err. Called where the
// watch pipeline gives up on a scan: tickOnce (detectChange failed, so no
// reload is scheduled) and scanAndCheckHierarchical (the debounced reload's
// own scan failed). A tick reaches at most one of the two, so a lasting
// failure moves the counter by one per tick. It never bumps
// da_config_reload_trigger_total: nothing was reloaded.
// Nil-receiver safe (see IncParseFailure).
func (cm *configMetrics) IncScanFailure(err error) {
	if cm == nil {
		return
	}
	cm.scanFailures.WithLabelValues(classifyScanFailure(err)).Inc()
}

// IncReloadTriggerBy bumps the counter by N (for the batch case where
// diffAndReload reloads k tenants all with the same reason).
func (cm *configMetrics) IncReloadTriggerBy(reason string, n int) {
	if n <= 0 {
		return
	}
	cm.reloadTriggers.WithLabelValues(reason).Add(float64(n))
}

// IncDefaultsNoop bumps the no-op counter. Called once per dependent
// tenant whose merged_hash didn't move after a defaults file changed.
func (cm *configMetrics) IncDefaultsNoop() {
	cm.defaultsNoop.Inc()
}

// IncDefaultsNoopBy bumps the no-op counter by N. Used by diffAndReload
// which computes the batch size without per-tenant allocations.
func (cm *configMetrics) IncDefaultsNoopBy(n int) {
	if n <= 0 {
		return
	}
	cm.defaultsNoop.Add(float64(n))
}

// IncDefaultsShadowed bumps the shadowed-defaults counter — called once
// per dependent tenant whose merged_hash didn't move because every
// changed defaults key is overridden by that tenant's source YAML
// (v2.8.0 Issue #61). Distinct from IncDefaultsNoop, which now counts
// only cosmetic edits.
func (cm *configMetrics) IncDefaultsShadowed() {
	cm.defaultsShadowed.Inc()
}

// IncDefaultsShadowedBy bumps the shadowed counter by N for the batch
// case (mirror of IncDefaultsNoopBy).
func (cm *configMetrics) IncDefaultsShadowedBy(n int) {
	if n <= 0 {
		return
	}
	cm.defaultsShadowed.Add(float64(n))
}

// ObserveBlastRadius records one (reason, scope, effect) bucket
// observation for the blast-radius histogram. n is the count of
// tenants in this bucket for the current diffAndReload tick.
//
// n <= 0 is silently no-op (caller can pass an empty bucket without
// guarding) so the per-tick group-by emission loop in diffAndReload
// can iterate over a sparse map without conditional logic.
func (cm *configMetrics) ObserveBlastRadius(reason, scope, effect string, n int) {
	if n <= 0 {
		return
	}
	cm.blastRadius.WithLabelValues(reason, scope, effect).Observe(float64(n))
}

// ObserveReloadDuration records one diffAndReload elapsed-time sample
// (v2.8.0 B-3). Called from fireDebounced wrapper around diffAndReload
// and from the synchronous-fallback path in triggerDebouncedReload so
// every reload contributes one sample regardless of debounce mode.
func (cm *configMetrics) ObserveReloadDuration(d time.Duration) {
	cm.reloadDuration.Observe(d.Seconds())
}

// ObserveDebounceBatch records one debounce-window batch-size sample
// (v2.8.0 B-3). Called once per fireDebounced with the count of triggers
// collapsed into the window. Synchronous fallback (debounceWindow=0)
// does NOT contribute to this histogram — it has no batching semantics
// to observe, and folding "1" samples in would skew the p50 baseline
// that ops use to detect debounce regressions.
func (cm *configMetrics) ObserveDebounceBatch(n int) {
	if n < 0 {
		return
	}
	cm.debounceBatch.Observe(float64(n))
}

// SetLastScanComplete records the wall-clock unix seconds at successful
// conf.d tree scan completion (v2.8.0 B-1.P2-a; stamped by scanDirTree on
// every clean scan, both planes, every tick). The e2e harness reads
// this gauge as anchor T1 in the 5-anchor measurement model; production
// uses `time() - <gauge>` for stuck-scanner alerting.
//
// Called only on success — error paths leave the gauge at its previous
// value so a transient scan failure does not look like a successful
// completion. Tests that want a clean baseline observe via freshMetrics
// + m.SetMetrics injection (see config_metrics_test.go).
// Nil-receiver safe (see IncParseFailure).
func (cm *configMetrics) SetLastScanComplete(t time.Time) {
	if cm == nil {
		return
	}
	cm.lastScanComplete.Set(float64(t.Unix()))
}

// SetLastReloadComplete records the wall-clock unix seconds at the
// successful diffAndReload completion (post atomic-swap, v2.8.0 B-1.P2-a).
// E2E harness reads this gauge as anchor T2.
//
// Called only on success path — see SetLastScanComplete docstring.
func (cm *configMetrics) SetLastReloadComplete(t time.Time) {
	cm.lastReloadComplete.Set(float64(t.Unix()))
}

// IncFreeOSMemory bumps the FreeOSMemory-call counter — called once per
// reload cycle when the -free-os-mem-after-reload lever is enabled (#459).
// The counter stays at 0 for the default (lever-off) deployment, so a
// non-zero value is itself the signal that the experimental return-to-OS
// path is active.
func (cm *configMetrics) IncFreeOSMemory() {
	cm.freeOSMemory.Inc()
}

// SetSubtreeUndeliverableTenants publishes the current number of tenants
// inheriting a subtree-only key the collector cannot emit (#1976). Called
// from ConfigManager.auditSubtreeUndeliverable on every config commit,
// including the healthy case (n == 0) — the zero write is what lets the
// gauge recover once the key is declared at the root. See
// config_subtree_undeliverable.go for why this is a gauge rather than a
// counter, and why the condition does not fail the load.
func (cm *configMetrics) SetSubtreeUndeliverableTenants(n int) {
	cm.subtreeUndeliverableTenants.Set(float64(n))
}

// SetConfigShape publishes the two whole-tree size maxima of the config
// just committed (#2153): da_config_max_tenants_per_file and
// da_config_max_mapping_keys. Called on every commit, including one that
// shrinks the tree, so the gauges follow the config down as well as up.
func (cm *configMetrics) SetConfigShape(s configShape) {
	cm.maxTenantsPerFile.Set(float64(s.maxTenantsPerFile))
	cm.maxMappingKeys.Set(float64(s.maxMappingKeys))
}

// SetInitialLoadDuration records how long the startup load took (#2153).
// Called once, by LoadInitial, when that load succeeds.
func (cm *configMetrics) SetInitialLoadDuration(d time.Duration) {
	cm.initialLoadDuration.Set(d.Seconds())
}
