// Package scrape is the exporter's /metrics: the collector that turns the
// served config into series, the config metrics registered beside it, and the
// registry wiring (#2115). It lives outside package main so `da-guard
// served-values` can gather the very same registry the exporter serves,
// instead of re-deriving which series /metrics would accept.
package scrape

import (
	"log"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/internal/thresholdmetric"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// Source is what the collector reads on each scrape: the committed config
// and its source metadata. The exporter's ConfigManager is one.
type Source interface {
	GetConfig() *config.ThresholdConfig
	GetConfigInfo() config.ConfigInfo
}

// Collector implements prometheus.Collector.
// On each scrape, it resolves the current config and exposes:
//   - user_threshold gauge metrics (Scenario A: numeric thresholds + Phase 2B: dimensional)
//   - user_state_filter gauge metrics (Scenario C: state matching flags)
//
// Uses "unchecked collector" mode (empty Describe) to support dynamic label sets
// introduced in Phase 2B. This is the standard Prometheus Go client pattern when
// the label set varies per metric (e.g., custom dimensional labels from config).
type Collector struct {
	src Source

	// Set only by a reader outside the exporter (served-values); nil on the
	// exporter, which resolves at time.Now() with ResolveAtWithStats and logs
	// a dropped user_threshold row.
	now     func() time.Time
	resolve func(cfg *config.ThresholdConfig, now time.Time) ([]config.ResolvedThreshold, config.ResolveStats)
	report  func(i int, m prometheus.Metric, err error)
	observe func(Reserved)
}

// Reserved is what one scrape resolved for the reserved keys the collector
// serves: the very slices its series were built from.
type Reserved struct {
	Metadata      []config.ResolvedMetadata
	SeverityDedup []config.ResolvedSeverityDedup
	Ops           config.OperationalStates
	// ThresholdExpiries is the time-boxed threshold reading
	// da_config_event{event="threshold_expired"} is emitted from.
	ThresholdExpiries []config.ResolvedThresholdExpiry
}

// NewCollector is the exporter's collector over src.
func NewCollector(src Source) *Collector {
	return &Collector{src: src}
}

// Hooks are the seams a reader outside the exporter sets. All nil is the
// exporter's collector.
type Hooks struct {
	// Now replaces time.Now() as the scrape instant.
	Now func() time.Time
	// Resolve replaces cfg.ResolveAtWithStats; it must return the same rows
	// and stats (da-guard uses ResolveAtWithKeys to keep each row's key).
	Resolve func(cfg *config.ThresholdConfig, now time.Time) ([]config.ResolvedThreshold, config.ResolveStats)
	// Report is thresholdmetric.Emit's report for the user_threshold rows.
	Report func(i int, m prometheus.Metric, err error)
	// Observe receives, once per scrape, the reserved-key readings the scrape
	// emitted from. A reader that needs them takes these instead of calling
	// the resolvers again, which would log each resolver WARN a second
	// time (#2374).
	Observe func(Reserved)
}

// NewCollectorWithHooks is NewCollector with the Hooks set.
func NewCollectorWithHooks(src Source, h Hooks) *Collector {
	return &Collector{src: src, now: h.Now, resolve: h.Resolve, report: h.Report, observe: h.Observe}
}

// Describe sends no descriptors — opts into unchecked collector mode.
// This allows Collect() to produce metrics with dynamic label sets
// (needed for Phase 2B dimensional metrics like {queue="tasks"}).
func (c *Collector) Describe(ch chan<- *prometheus.Desc) {
	// Empty: unchecked collector mode for dynamic labels
}

// Collect implements prometheus.Collector.
// Called on every /metrics scrape — resolves config in real-time.
func (c *Collector) Collect(ch chan<- prometheus.Metric) {
	cfg := c.src.GetConfig()
	if cfg == nil {
		return
	}

	// Scenario A + Phase 2B: numeric thresholds (with optional dimensional labels).
	// ResolveAtWithStats (#652) returns the same resolved-thresholds slice as
	// the legacy Resolve(), plus per-tenant cap-hit magnitudes that
	// collectTenantMetricsOverLimit emits as da_tenant_metrics_over_limit.
	// One timestamp for the whole scrape so threshold values and expiry events
	// resolve consistently — a separate time.Now() per resolver could straddle an
	// `expires:` boundary and emit a one-scrape value/event mismatch (CodeRabbit).
	now := time.Now()
	if c.now != nil {
		now = c.now()
	}
	var resolved []config.ResolvedThreshold
	var stats config.ResolveStats
	if c.resolve != nil {
		resolved, stats = c.resolve(cfg, now)
	} else {
		resolved, stats = cfg.ResolveAtWithStats(now)
	}

	// Each metric family is emitted by a focused collector method; order is
	// irrelevant (Prometheus sorts on Gather) but kept stable for diff clarity.
	c.collectThresholds(ch, resolved)
	c.collectTenantMetricsOverLimit(ch, stats.PerTenantOverLimit)
	c.collectCustomAlertErrors(ch, stats.PerTenantCustomAlertErrors)
	c.collectDeprecatedKeys(ch, stats.PerTenantDeprecatedKeys)
	c.collectSloObjectives(ch, stats.SloObjectives)
	// Silent mode + state filters come from ONE reading shared with tenant-api
	// (config.OperationalStatesAt, #1988), resolved at the scrape's `now`.
	ops := cfg.OperationalStatesAt(now)
	c.collectStateFilters(ch, ops.StateFilters)
	c.collectSilentModes(ch, ops)
	c.collectMaintenanceExpiries(ch, cfg, now)
	expiries := cfg.ResolveThresholdExpiriesAt(now)
	c.collectThresholdExpiries(ch, expiries)
	dedup := cfg.ResolveSeverityDedup()
	c.collectSeverityDedup(ch, dedup)
	c.collectConfigInfo(ch)

	// ResolveMetadata is a pure per-scrape snapshot (walks + yaml.Unmarshals
	// every tenant's _metadata once). Resolve it a single time and share the
	// slice between the two metadata consumers instead of resolving twice.
	metadata := cfg.ResolveMetadata()
	c.collectMetadata(ch, metadata)
	c.collectTenantExpectedExporter(ch, metadata)

	if c.observe != nil {
		c.observe(Reserved{Metadata: metadata, SeverityDedup: dedup, Ops: ops, ThresholdExpiries: expiries})
	}
}

// tenantMetricsOverLimitDesc describes da_tenant_metrics_over_limit (#652).
// Name, help text and label are the exposition contract and stay as they
// were when this was a GaugeVec. The help's "evicted by Reset()" wording
// describes that old mechanism; what it promises a reader still holds —
// a vanished tenant's series is gone from the next scrape.
var tenantMetricsOverLimitDesc = prometheus.NewDesc(
	"da_tenant_metrics_over_limit",
	"State-coded magnitude of per-tenant cardinality cap-hit (#652): max(0, count - max_metrics_per_tenant). 0 means the tenant fits under the cap. Set per scrape from ResolveAtWithStats; vanished tenants are evicted by Reset() before the per-tenant Set() pass. NOT a counter — a tenant stuck 100-over-limit reports 100 for as long as the truncation persists (does not inflate with scrape frequency). Alert: > 0 per-tenant (TenantMetricsOverLimit, warning); count without (tenant)(... > 0) > 50 as a defaults-storm sentinel (DefaultsTruncationStorm, critical).",
	[]string{"tenant"},
	nil,
)

// collectTenantMetricsOverLimit emits da_tenant_metrics_over_limit (#652)
// as const metrics built from this scrape's ResolveStats: one series per
// tenant, 0 for a compliant tenant (so a tenant that just dropped back
// below the cap reads 0 rather than keeping its old value). A tenant
// deleted from config is not in perTenant, so its series is gone from the
// next scrape with no eviction step.
//
// ⛔ Do not turn this back into a registered GaugeVec that Collect
// Reset()s and Set()s. Registry.Gather collects every registered Collector
// on its own goroutine, so the GaugeVec's own Collect is not ordered after
// the Reset+Set, and registration order does not change that: a scrape
// read the family missing, or with only some tenants (seen while dumping
// /metrics for #2266). Two overlapping scrapes also shared that one
// GaugeVec. A const metric is built from the stats of the Collect that
// sends it, so neither can happen.
func (c *Collector) collectTenantMetricsOverLimit(ch chan<- prometheus.Metric, perTenant map[string]int) {
	for tenant, magnitude := range perTenant {
		if m, err := prometheus.NewConstMetric(tenantMetricsOverLimitDesc, prometheus.GaugeValue,
			float64(magnitude), tenant); err == nil {
			ch <- m
		}
	}
}

// collectThresholds emits the user_threshold gauge for every resolved
// threshold (Scenario A numeric + Phase 2B dimensional) with
// internal/thresholdmetric; c.report (nil on the exporter) is told what
// became of each row.
func (c *Collector) collectThresholds(ch chan<- prometheus.Metric, resolved []config.ResolvedThreshold) {
	thresholdmetric.Emit(ch, resolved, c.report)
}

// collectCustomAlertErrors emits da_custom_alert_parse_errors (#741 S3a,
// fail-loud) per tenant. ConstMetric per scrape so a fixed tenant stops
// emitting rather than leaving a stale GaugeVec series.
func (c *Collector) collectCustomAlertErrors(ch chan<- prometheus.Metric, caErrors map[string]int) {
	caErrDesc := prometheus.NewDesc(
		"da_custom_alert_parse_errors",
		"Per-tenant count of malformed _custom_alerts entries dropped at resolve time (ADR-024 能力 B, #741). 0 = all parsed+validated. Fail-loud: a silently-skipped custom alert surfaces here. Alert: > 0 (warning). Upstream hard-gates (CI compiler, tenant-api preflight) catch these first; this is the defense-in-depth last line for a direct-push bypass.",
		[]string{"tenant"},
		nil,
	)
	for tenant, n := range caErrors {
		m, err := prometheus.NewConstMetric(caErrDesc, prometheus.GaugeValue, float64(n), tenant)
		if err != nil {
			log.Printf("WARN: failed to create da_custom_alert_parse_errors metric for tenant=%s: %v", tenant, err)
			continue
		}
		ch <- m
	}
}

// collectDeprecatedKeys emits da_config_deprecated_keys (#1231): the number
// of deprecated (aliased) key spellings still present in each tenant's own
// config during a rename transition window (e.g. mysql_cpu →
// mysql_threads_running). ConstMetric per scrape (same shape as
// collectCustomAlertErrors) so a migrated tenant's series simply stops
// emitting; tenants with zero deprecated keys never appear.
//
// Deliberately its own metric and NOT a da_config_event series: the
// TenantConfigEvent rule fires unconditionally on `da_config_event == 1`
// (for: 0s) with expiry-flavored notification copy — riding that metric
// would page tenants with a bogus "expired and auto-disabled" story for a
// key that still works. This gauge is migration-progress observability only
// and has NO alert wired to it.
func (c *Collector) collectDeprecatedKeys(ch chan<- prometheus.Metric, deprecated map[string]int) {
	depDesc := prometheus.NewDesc(
		"da_config_deprecated_keys",
		"Per-tenant count of deprecated threshold key spellings still present in the tenant's config (#1231 rename transition, e.g. mysql_cpu → mysql_threads_running). Resolve treats them as their canonical replacement and dual-emits the legacy series during the transition window; this gauge tracks migration progress. Informational only — no alert consumes it.",
		[]string{"tenant"},
		nil,
	)
	for tenant, n := range deprecated {
		m, err := prometheus.NewConstMetric(depDesc, prometheus.GaugeValue, float64(n), tenant)
		if err != nil {
			log.Printf("WARN: failed to create da_config_deprecated_keys metric for tenant=%s: %v", tenant, err)
			continue
		}
		ch <- m
	}
}

// collectSloObjectives emits user_slo_objective (ADR-031): the raw SLO target
// percentage a tenant declared on each slo_burn_rate custom alert. The derived
// per-severity burn thresholds ride user_threshold{component="custom"}; this
// gauge echoes the tenant's INPUT so dashboards/rules can display or join the
// objective without reverse-engineering it from the multipliers. ConstMetric per
// scrape (like the user_* family) so a removed/disabled declaration simply stops
// emitting — objective:"disable" produces no series by construction.
func (c *Collector) collectSloObjectives(ch chan<- prometheus.Metric, objectives []config.ResolvedSloObjective) {
	sloDesc := prometheus.NewDesc(
		"user_slo_objective",
		"Tenant-declared SLO objective percentage for a slo_burn_rate custom alert (ADR-031). One series per active declaration; objective:\"disable\" emits none.",
		[]string{"tenant", "recipe_id"},
		nil,
	)
	for _, o := range objectives {
		m, err := prometheus.NewConstMetric(sloDesc, prometheus.GaugeValue, o.Objective, o.Tenant, o.RecipeID)
		if err != nil {
			log.Printf("WARN: failed to create user_slo_objective metric for tenant=%s recipe_id=%s: %v", o.Tenant, o.RecipeID, err)
			continue
		}
		ch <- m
	}
}

// collectStateFilters emits user_state_filter flags (Scenario C).
func (c *Collector) collectStateFilters(ch chan<- prometheus.Metric, stateFilters []config.ResolvedStateFilter) {
	stateDesc := prometheus.NewDesc(
		"user_state_filter",
		"State-based monitoring filter flag (1=enabled, absent=disabled). Scenario C: state/string matching.",
		[]string{"tenant", "filter", "severity"},
		nil,
	)
	for _, sf := range stateFilters {
		m, err := prometheus.NewConstMetric(stateDesc, prometheus.GaugeValue, 1.0, sf.Tenant, sf.FilterName, sf.Severity)
		if err != nil {
			log.Printf("WARN: failed to create user_state_filter metric for tenant=%s filter=%s: %v", sf.Tenant, sf.FilterName, err)
			continue
		}
		ch <- m
	}
}

// configEventDesc is the shared descriptor for da_config_event: an ephemeral
// gauge emitted when a timed config (silent / maintenance / threshold expiry)
// has expired. Used by the TenantConfigEvent alert rule (for: 0s) to notify
// operators. The metric is emitted as long as the expired config YAML remains —
// once the tenant removes or updates the config, this metric disappears (stale
// marker). Shared by the three collect* expiry sites via emitConfigEvent.
//
// ⛔ Every series in this family must carry a distinct label set: two series
// with equal labels do not degrade one event, they fail the whole Gather and
// /metrics answers 500 for every tenant (#2003). Uniqueness is therefore held
// by the labels themselves, never by the free-text reason:
//   - silence_expired     — one per (tenant, target_severity); `target: all`
//     expands to warning + critical, which share the tenant's reason.
//   - maintenance_expired — one per tenant (a single _state_maintenance key).
//   - threshold_expired   — one per (tenant, metric key): the key is the
//     metric_key label (#2031). It used to be encoded into reason only
//     (`<key>: <reason>`), and a key holding ": " collided with another
//     key's reason text — `a` with reason `b: c` and `a: b` with reason `c`
//     both read `a: b: c`, and /metrics answered 500. reason keeps that
//     text, unchanged.
//
// target_severity and metric_key are part of ONE label schema for all three
// events, not per-event extras: the events that do not have one set it to
// "", and in the Prometheus data model an empty label value is the same as
// the label being absent, so `by (target_severity)` / `{metric_key=""}`
// behave uniformly.
var configEventDesc = prometheus.NewDesc(
	"da_config_event",
	"Config lifecycle event (1=event active). Emitted when timed config expires. Labels identify tenant, event type and reason; target_severity is set for silence_expired and metric_key for threshold_expired, each empty for the other events.",
	[]string{"tenant", "event", "reason", "target_severity", "metric_key"},
	nil,
)

// emitConfigEvent sends one da_config_event series (value 1) for an expired
// timed config. Consolidates the three collect* expiry sites that emit the same
// {tenant,event,reason,target_severity,metric_key} shape; a NewConstMetric
// failure is logged and skipped (never fatal to the scrape). targetSeverity is
// "" for events that are not scoped to a severity, metricKey "" for events
// that are not about one threshold.
func emitConfigEvent(ch chan<- prometheus.Metric, tenant, event, reason, targetSeverity, metricKey string) {
	m, err := prometheus.NewConstMetric(configEventDesc, prometheus.GaugeValue, 1.0, tenant, event, reason, targetSeverity, metricKey)
	if err != nil {
		log.Printf("WARN: failed to create da_config_event metric for tenant=%s: %v", tenant, err)
		return
	}
	ch <- m
}

// collectSilentModes emits user_silent_mode for active silences; expired
// silences emit da_config_event instead (v1.7.0) so Alertmanager inhibit
// stops and notifications resume. The active/expired split is
// OperationalStatesAt's, not this method's.
func (c *Collector) collectSilentModes(ch chan<- prometheus.Metric, ops config.OperationalStates) {
	silentDesc := prometheus.NewDesc(
		"user_silent_mode",
		"Silent mode flag (1=active). Alerts fire (TSDB records) but notifications suppressed via Alertmanager inhibit.",
		[]string{"tenant", "target_severity"},
		nil,
	)
	for _, sm := range ops.ExpiredSilences {
		// Expired: emit config event instead of sentinel metric
		reason := sm.Reason
		if reason == "" {
			reason = "silent_mode expired for " + sm.TargetSeverity
		}
		emitConfigEvent(ch, sm.Tenant, "silence_expired", reason, sm.TargetSeverity, "")
	}
	for _, sm := range ops.Silences {
		m, err := prometheus.NewConstMetric(silentDesc, prometheus.GaugeValue, 1.0, sm.Tenant, sm.TargetSeverity)
		if err != nil {
			log.Printf("WARN: failed to create user_silent_mode metric for tenant=%s: %v", sm.Tenant, err)
			continue
		}
		ch <- m
	}
}

// collectMaintenanceExpiries emits da_config_event when a maintenance window
// has auto-deactivated (v1.7.0). Active windows need no event (their state
// filter simply keeps emitting).
//
// The exporter (c.now nil) resolves at the wall clock, as it always has; a
// reader that set Hooks.Now resolves at the scrape's `now` (that hook's
// instant), like every other family.
//
// Collect has already resolved the state filters (OperationalStatesAt), which
// log the _state_maintenance parse WARNs when the maintenance filter is
// declared, so this reading must not log them a second time (#2426).
func (c *Collector) collectMaintenanceExpiries(ch chan<- prometheus.Metric, cfg *config.ThresholdConfig, now time.Time) {
	if c.now == nil {
		now = time.Now()
	}
	expiries := cfg.ResolveMaintenanceExpiriesAfterStateFiltersAt(now)
	for _, me := range expiries {
		if !me.Expired {
			continue // Still active, no event needed
		}
		reason := me.Reason
		if reason == "" {
			reason = "maintenance_mode expired"
		}
		emitConfigEvent(ch, me.Tenant, "maintenance_expired", reason, "", "")
	}
}

// collectThresholdExpiries emits da_config_event when a time-boxed threshold
// override has lapsed (PREVENT #656). The threshold VALUE itself already
// fail-safed back to the platform default in resolveBaseRows; this event lets a
// cleanup PR remove the stale conf.d YAML and gives operators visibility. The
// metric key is the metric_key label, so each (tenant, metric) event is a
// distinct da_config_event series whatever its reason text (#2031; see
// configEventDesc). reason still names the key, as it always has.
func (c *Collector) collectThresholdExpiries(ch chan<- prometheus.Metric, expiries []config.ResolvedThresholdExpiry) {
	for _, te := range expiries {
		if !te.Expired {
			continue // not yet past its TTL — the override is still active
		}
		reason := "threshold " + te.MetricKey + " expired"
		if te.Reason != "" {
			reason = te.MetricKey + ": " + te.Reason
		}
		emitConfigEvent(ch, te.Tenant, "threshold_expired", reason, "", te.MetricKey)
	}
}

// collectSeverityDedup emits user_severity_dedup flags (v1.2.0+).
func (c *Collector) collectSeverityDedup(ch chan<- prometheus.Metric, dedup []config.ResolvedSeverityDedup) {
	dedupDesc := prometheus.NewDesc(
		"user_severity_dedup",
		"Severity dedup flag (1=enabled). Warning notifications suppressed when critical fires for same metric_group. v1.2.0+",
		[]string{"tenant", "mode"},
		nil,
	)
	for _, sd := range dedup {
		m, err := prometheus.NewConstMetric(dedupDesc, prometheus.GaugeValue, 1.0, sd.Tenant, sd.Mode)
		if err != nil {
			log.Printf("WARN: failed to create user_severity_dedup metric for tenant=%s: %v", sd.Tenant, err)
			continue
		}
		ch <- m
	}
}

// collectConfigInfo emits threshold_exporter_config_info (v2.3.0): config
// source + git revision for GitOps drift observability.
func (c *Collector) collectConfigInfo(ch chan<- prometheus.Metric) {
	configInfoDesc := prometheus.NewDesc(
		"threshold_exporter_config_info",
		"Config source metadata (info metric, always 1). Labels identify deployment mode and git revision. v2.3.0+",
		[]string{"config_source", "git_commit"},
		nil,
	)
	info := c.src.GetConfigInfo()
	if m, err := prometheus.NewConstMetric(configInfoDesc, prometheus.GaugeValue, 1.0,
		info.ConfigSource, info.GitCommit); err == nil {
		ch <- m
	}
}

// collectMetadata emits tenant_metadata_info (v1.11.0): unconditional per-tenant
// runbook_url/owner/tier labels for Alertmanager group_left joins.
func (c *Collector) collectMetadata(ch chan<- prometheus.Metric, metadata []config.ResolvedMetadata) {
	metadataDesc := prometheus.NewDesc(
		"tenant_metadata_info",
		"Tenant metadata labels (info metric, always 1). Unconditional output for group_left joins. v1.11.0+",
		[]string{"tenant", "runbook_url", "owner", "tier"},
		nil,
	)
	for _, md := range metadata {
		m, err := prometheus.NewConstMetric(metadataDesc, prometheus.GaugeValue, 1.0,
			md.Tenant, md.RunbookURL, md.Owner, md.Tier)
		if err != nil {
			log.Printf("WARN: failed to create tenant_metadata_info metric for tenant=%s: %v", md.Tenant, err)
			continue
		}
		ch <- m
	}
}

// collectTenantExpectedExporter emits tenant_expected_exporter{tenant, db_type}=1
// for every tenant that DECLARES a db_type in its _metadata block (#869).
//
// This is the LEFT-hand input to the per-tenant liveness anti-join
// (TenantExporterAbsent in rule-pack-liveness.yaml):
//
//	tenant_expected_exporter unless on(tenant) (up{job="tenant-exporters"} == 1)
//
// so each declaring tenant for which no live exporter target reports up==1 is
// flagged — fixing the global `absent(<db>_up)` false-negative where one live
// tenant masked another tenant's total exporter outage.
//
// Opt-in contract — db_type guard is LOAD-BEARING, not cosmetic:
// the metadata slice (from ResolveMetadata, shared with collectMetadata) holds
// EVERY tenant unconditionally, and a tenant with no _metadata resolves to
// DBType=="" (see ResolveMetadata in pkg/config/resolve.go). Emitting db_type=""
// for such tenants would put a left-hand series into the anti-join for a tenant
// that has no corresponding `<db>_up` series at all → the rule would fire a permanent
// false-positive critical against it. So we emit ONLY for tenants that declared a
// db_type: liveness coverage is OPT-IN via declaring db_type. (Hence "1 series per
// tenant that declares db_type", not "1 per tenant" — see #869 design note.)
//
// Deliberately a NEW, SEPARATE metric — db_type is intentionally NOT added to
// tenant_metadata_info. types.go:102-103 records the v2.5.0 decision that db_type
// is NOT a Prometheus label (cardinality concern); tenant_metadata_info is also a
// load-bearing 1-series-per-tenant info metric consumed by ~15 group_left joins,
// so widening its label set is forbidden. This is a SCOPED reversal of types.go:102
// limited to the narrow, low-cardinality (1 series/declaring tenant) liveness need.
func (c *Collector) collectTenantExpectedExporter(ch chan<- prometheus.Metric, metadata []config.ResolvedMetadata) {
	expectedDesc := prometheus.NewDesc(
		"tenant_expected_exporter",
		"Liveness expectation (always 1) for each tenant that declares a db_type in _metadata. "+
			"LHS of the TenantExporterAbsent anti-join (#869). One series per declaring tenant.",
		[]string{"tenant", "db_type"},
		nil,
	)
	for _, md := range metadata {
		// Opt-in: only tenants that declared a db_type are expected to have a
		// live exporter. Skipping db_type="" avoids a false-positive TenantExporterAbsent.
		if md.DBType == "" {
			continue
		}
		m, err := prometheus.NewConstMetric(expectedDesc, prometheus.GaugeValue, 1.0,
			md.Tenant, md.DBType)
		if err != nil {
			log.Printf("WARN: failed to create tenant_expected_exporter metric for tenant=%s: %v", md.Tenant, err)
			continue
		}
		ch <- m
	}
}
