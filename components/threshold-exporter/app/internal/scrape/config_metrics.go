package scrape

import "github.com/prometheus/client_golang/prometheus"

// ConfigMetrics is the set of config / reload metrics the exporter registers
// beside its collector. Package main's configMetrics holds one and mutates it
// through its methods; this package owns the definitions and the
// registration order so da-guard served-values registers the same set.
type ConfigMetrics struct {
	ScanDuration       prometheus.Histogram
	ReloadTriggers     *prometheus.CounterVec
	DefaultsNoop       prometheus.Counter
	ParseFailures      *prometheus.CounterVec   // v2.8.0 A-8d: per-file YAML parse failures
	DefaultsShadowed   prometheus.Counter       // v2.8.0 Issue #61: shadowed defaults change (split from defaultsNoop)
	BlastRadius        *prometheus.HistogramVec // v2.8.0 Issue #61: per-tick (reason,scope,effect) tenants-affected distribution
	ReloadDuration     prometheus.Histogram     // v2.8.0 B-3: end-to-end diffAndReload elapsed (debounce window → atomic swap done)
	DebounceBatch      prometheus.Histogram     // v2.8.0 B-3: count of triggers coalesced per fired window (debounce effectiveness)
	LastScanComplete   prometheus.Gauge         // v2.8.0 B-1.P2-a: wall-clock unix seconds at most-recent successful conf.d tree scan completion (e2e harness anchor T1; production stuck-detection)
	LastReloadComplete prometheus.Gauge         // v2.8.0 B-1.P2-a: wall-clock unix seconds at most-recent successful diffAndReload completion (e2e harness anchor T2; production stuck-detection)
	FreeOSMemory       prometheus.Counter       // #459: count of explicit runtime/debug.FreeOSMemory() calls after reload (opt-in -free-os-mem-after-reload; 0 when lever disabled)
	// State-coded gauge: tenants that inherit a key existing ONLY in a
	// subtree `_defaults.yaml`, which /effective reports and the collector
	// cannot emit (#1976; was the #1521 divergence gauge until #1957 removed
	// its other cause). Gauge, not counter: the value is the CURRENT size of
	// the set, so declaring the key at the root drives it back to 0. A
	// counter could only ever say "it happened N times", where N tracks
	// reload frequency rather than misconfiguration severity. Set on every
	// commitConfig — see config_subtree_undeliverable.go.
	SubtreeUndeliverableTenants prometheus.Gauge
	// #2153 (D): the two size axes that drive the cold-load parse cost —
	// the widest `tenants:` block in any one file, and the largest key
	// count of any one mapping the flat plane decodes. Whole-tree maxima,
	// NOT per-file series: a conf.d tree can hold thousands of files, and
	// a file-name label would make the gauge's cardinality track the tree.
	// Re-Set on every config commit (see config_shape.go).
	MaxTenantsPerFile prometheus.Gauge
	MaxMappingKeys    prometheus.Gauge
	// #2153 (D): wall-clock seconds of the startup load (LoadInitial). A
	// gauge, not a sample in reloadDuration: the startup load happens once
	// per process and is a different operation (cold scan, every
	// merged_hash) from the debounced reload that histogram's p99 describes.
	InitialLoadDuration prometheus.Gauge
	// #2452: conf.d tree scans that failed on the watch path, by reason. A
	// failed scan applies nothing and so never reaches
	// da_config_reload_trigger_total; before this counter a running
	// exporter whose tree stopped scanning (every later edit ignored, the
	// last good config still served) had no series to alert on. reason is a
	// closed set — see package main's ScanFailureReason* constants.
	ScanFailures *prometheus.CounterVec
}

// NewConfigMetrics builds a fresh set without registering it.
func NewConfigMetrics() *ConfigMetrics {
	return &ConfigMetrics{
		ScanDuration: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name: "da_config_scan_duration_seconds",
			Help: "Duration of one conf.d tree scan (v2.7.0, ADR-016). Observed once per conf.d tree scan (both planes, every watch tick and load).",
			// Buckets tuned for 1000-tenant scans on ext4 (p50 ~20ms, p99
			// ~150ms in the benchmark) plus slack for FUSE/NFS mounts.
			// #2153: 10/30/60/120 appended — a cold scan of one file whose
			// single mapping holds tens of thousands of keys was measured at
			// 4.8s (33k keys) and 77s (130k keys), which the old 5s top-end
			// folded into +Inf. Appending buckets leaves the value of every
			// existing `le` series unchanged.
			Buckets: []float64{0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 120},
		}),
		ReloadTriggers: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "da_config_reload_trigger_total",
			Help: "Count of hierarchical reloads, labeled by the change that triggered them (source, defaults, new, delete, forced).",
		}, []string{"reason"}),
		DefaultsNoop: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "da_config_defaults_change_noop_total",
			Help: "Count of _defaults.yaml changes that did NOT move any dependent tenant's merged_hash AND were not shadowed by a tenant override — i.e. cosmetic edits (comment-only, key reordering, or unrelated-key change). v2.8.0 Issue #61 narrowed the semantics; shadowed cases now go to da_config_defaults_shadowed_total. Pre-2.8.0 dashboards reading this counter for 'how often did the inheritance system block changes' should switch to da_config_defaults_shadowed_total.",
		}),
		ParseFailures: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "da_config_parse_failure_total",
			Help: "Count of per-file YAML parse failures during hierarchical scan (v2.8.0 A-8d). Label 'file_basename' lets ops pin down which tenant or defaults file is broken. Alert: >5/h for any single basename = page ops.",
		}, []string{"file_basename"}),
		DefaultsShadowed: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "da_config_defaults_shadowed_total",
			Help: "Count of dependent tenants for whom a defaults change was effectively blocked because every changed key is overridden by that tenant's source YAML (v2.8.0 Issue #61, ADR-017 inheritance). Distinct from da_config_defaults_change_noop_total which counts cosmetic edits with no semantic key movement.",
		}),
		BlastRadius: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name: "da_config_blast_radius_tenants_affected",
			Help: "Distribution of tenants affected per diffAndReload tick, grouped by (reason, scope, effect) (v2.8.0 Issue #61, RFC). reason=source/defaults/new/delete; scope=global/domain/region/env/tenant/unknown (widest changed defaults level for reason=defaults; tenant for source/new/delete); effect=applied (merged_hash moved) / shadowed (defaults change blocked by tenant override) / cosmetic (no semantic key change). Alert on histogram_quantile(0.99, sum by (le)(rate(...{effect=\"applied\"}_bucket[5m]))) > 500 for high-impact change detection.",
			// Buckets chosen to surface low-impact (1-5 affected) vs
			// catastrophic-blast (5000+) reloads. 2500/10000 added per
			// CHANGELOG sharding-decision: ≤2000 fine; 5000-10000 is the
			// optimization tier; >10000 is sharding territory.
			Buckets: []float64{1, 5, 25, 100, 500, 1000, 2500, 5000, 10000},
		}, []string{"reason", "scope", "effect"}),
		ReloadDuration: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name: "da_config_reload_duration_seconds",
			Help: "End-to-end duration of diffAndReload (scan + per-tenant merge + blast-radius emit + fullDirLoad + atomic swap). Observed once per fired debounce window or once per synchronous fallback (debounceWindow=0). v2.8.0 B-3: feeds the empirical p99 used to validate the 300ms debounce floor and inform Phase 2 SLO sign-off.",
			// Buckets cover synthetic 1000-tenant baseline (~200ms p50,
			// ~500ms p99) + 5000-tenant tail (~1.1s) + headroom for
			// degraded FUSE / NFS mounts. 30s top-end exists so a
			// pathological reload does not silently saturate the last
			// bucket — operators want to see the actual tail.
			// #2153: 60/120/300/600 appended — one file declaring 2000-4000
			// tenants was measured reloading in 30s-3m09s, all of which the
			// old 30s top-end folded into +Inf. Appending buckets leaves the
			// value of every existing `le` series unchanged.
			Buckets: []float64{0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600},
		}),
		DebounceBatch: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name: "da_config_debounce_batch_size",
			Help: "Number of triggerDebouncedReload calls collapsed into a single fired window (v2.8.0 B-3 debounce effectiveness). Observed once per fireDebounced; sample count == fire count. p50 == 1 means debounce never coalesces (window may be too short or fsnotify storms are absent); p99 climbing past ~50 signals an event-storm pathology worth investigating.",
			// Bucket boundaries chosen to surface (a) the typical 1-2
			// case (single-file edits), (b) the K8s symlink-rotation
			// case (3-10 fsnotify events per ConfigMap update), and
			// (c) git-sync batch case (10-200 files in one rsync
			// burst — exactly the scenario B-7 stress-tests).
			Buckets: []float64{1, 2, 5, 10, 25, 50, 100, 250, 500},
		}),
		LastScanComplete: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "da_config_last_scan_complete_unixtime_seconds",
			Help: "Wall-clock unix seconds at the most recent successful conf.d tree scan completion (both planes; every watch tick and load). Set by the scanner; read by the e2e harness as anchor T1 (B-1 Phase 2). Production use: alert on time() - <gauge> > N for stuck-scanner detection. 0 means scanner has not yet completed a successful scan.",
		}),
		LastReloadComplete: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "da_config_last_reload_complete_unixtime_seconds",
			Help: "Wall-clock unix seconds at the most recent successful diffAndReload completion (post atomic-swap). Set by the reload pipeline; read by the e2e harness as anchor T2 (B-1 Phase 2). Production use: alert on time() - <gauge> > N for stuck-reloader detection. 0 means reloader has not yet completed a successful reload.",
		}),
		FreeOSMemory: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "da_config_free_os_memory_total",
			Help: "Count of explicit runtime/debug.FreeOSMemory() calls issued after a reload cycle (#459). Stays 0 unless the -free-os-mem-after-reload lever is enabled. Each increment is one forced GC + return-to-OS; correlate with go_memstats_heap_released_bytes to confirm the lever is reclaiming idle heap under sustained reload pressure.",
		}),
		SubtreeUndeliverableTenants: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "da_config_subtree_undeliverable_tenants",
			Help: "Number of tenants that inherit at least one key existing ONLY in a subtree _defaults.yaml (#1976). /effective reports such a key's value, but the collector cannot emit it — it iterates the conf.d ROOT defaults and the declared surface (optional_overrides), and a nested _defaults.yaml feeds neither — so the tenant's alert on that key can never fire. Every other key of the tenant is delivered. Workaround: declare the key in the ROOT _defaults.yaml or in optional_overrides. State-coded: re-Set on every config commit, so it returns to 0 once the key is declared at the root or removed. The accompanying ERROR log names the tenants, their source files and the keys. Replaces the former conf.d scanner-divergence gauge (#1957), whose other cause — one file decoded into different tenant sets by the two planes — is gone because both planes now judge a file with one decode. Known tenant-set exceptions that are NOT counted here: the incremental tenant-only reload keeping a broken file's last good tenants (#1980) and tenants declared in a _-prefixed file (#1982). SUGGESTED alert: > 0 for 10m — no PrometheusRule ships for it.",
		}),
		MaxTenantsPerFile: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "da_config_max_tenants_per_file",
			Help: "Largest number of tenants declared under `tenants:` in any single config file of the committed config (#2153). A whole-tree maximum, not a per-file series. Load and reload time grow faster than linearly with this number, so a file declaring thousands of tenants is the shape to split into several files. A file that failed to parse is not counted. Re-Set on every config commit.",
		}),
		MaxMappingKeys: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "da_config_max_mapping_keys",
			Help: "Largest key count of any single mapping in one config file of the committed config (#2153): `defaults`, `state_filters`, `tenants`, one tenant's overrides, `profiles`, or one profile. The YAML decoder checks duplicate keys pairwise, so decoding a mapping costs time proportional to the square of its key count. Counted on the already-decoded config, so mappings the exporter does not decode into it (unknown keys, nested `_`-prefixed files, sections dropped by the file-placement rules) are not counted, nor is a file that failed to parse. Re-Set on every config commit.",
		}),
		InitialLoadDuration: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "da_config_initial_load_duration_seconds",
			Help: "Wall-clock seconds the startup config load took (#2153). Set once, when that load succeeds. The HTTP server (and so /metrics, /health and /ready) starts only after it, so a startup probe must allow at least this long. Reloads are measured by da_config_reload_duration_seconds instead.",
		}),
		ScanFailures: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "da_config_scan_failures_total",
			Help: "Count of conf.d tree scans that failed on the watch path (#2452): the per-tick change check and the debounced reload's scan, directory mode only. A failed scan applies nothing: the exporter keeps serving the last good config, /ready stays 200 and da_config_reload_trigger_total does not move, so while this keeps rising every later edit is ignored. reason is a closed set: duplicate_tenant (one tenant id declared in two files; both directory modes) or walk_error (the config directory cannot be walked: missing, not a directory). One increment per failed scan, i.e. about one per watch tick while the condition lasts. Alert: ConfigScanFailing (failures, and da_config_last_scan_complete_unixtime_seconds older than 5m).",
		}, []string{"reason"}),
	}
}

// Collectors is the set in the exporter's registration order.
func (s *ConfigMetrics) Collectors() []prometheus.Collector {
	return []prometheus.Collector{
		s.ScanDuration,
		s.ReloadTriggers,
		s.DefaultsNoop,
		s.ParseFailures,
		s.DefaultsShadowed,
		s.BlastRadius,
		s.ReloadDuration,
		s.DebounceBatch,
		s.LastScanComplete,
		s.LastReloadComplete,
		s.FreeOSMemory,
		s.SubtreeUndeliverableTenants,
		s.MaxTenantsPerFile,
		s.MaxMappingKeys,
		s.InitialLoadDuration,
		s.ScanFailures,
	}
}
