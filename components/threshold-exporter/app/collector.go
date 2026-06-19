package main

import (
	"log"
	"net/http"
	"sort"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// ThresholdCollector implements prometheus.Collector.
// On each scrape, it resolves the current config and exposes:
//   - user_threshold gauge metrics (Scenario A: numeric thresholds + Phase 2B: dimensional)
//   - user_state_filter gauge metrics (Scenario C: state matching flags)
//
// Uses "unchecked collector" mode (empty Describe) to support dynamic label sets
// introduced in Phase 2B. This is the standard Prometheus Go client pattern when
// the label set varies per metric (e.g., custom dimensional labels from config).
type ThresholdCollector struct {
	manager *ConfigManager
}

func NewThresholdCollector(manager *ConfigManager) *ThresholdCollector {
	return &ThresholdCollector{
		manager: manager,
	}
}

// Describe sends no descriptors — opts into unchecked collector mode.
// This allows Collect() to produce metrics with dynamic label sets
// (needed for Phase 2B dimensional metrics like {queue="tasks"}).
func (c *ThresholdCollector) Describe(ch chan<- *prometheus.Desc) {
	// Empty: unchecked collector mode for dynamic labels
}

// Collect implements prometheus.Collector.
// Called on every /metrics scrape — resolves config in real-time.
func (c *ThresholdCollector) Collect(ch chan<- prometheus.Metric) {
	cfg := c.manager.GetConfig()
	if cfg == nil {
		return
	}

	// Scenario A + Phase 2B: numeric thresholds (with optional dimensional labels).
	// ResolveAtWithStats (#652) returns the same resolved-thresholds slice as
	// the legacy Resolve(), plus per-tenant cap-hit magnitudes used to publish
	// the da_tenant_metrics_over_limit gauge. PublishTenantMetricsOverLimit
	// Reset()s the GaugeVec then per-tenant Set()s in one pass so vanished
	// tenants are evicted automatically and just-dropped-below-the-cap tenants
	// clamp to 0 instead of carrying their stale over-limit value forward.
	//
	// Route through c.manager.getMetrics() — NOT the package-level
	// PublishTenantMetricsOverLimit helper — so tests that inject a fresh
	// configMetrics via ConfigManager.SetMetrics observe the gauge writes
	// on their own instance. Every other metric in this file already
	// follows the m.getMetrics() pattern (see config.go IncReloadTrigger
	// / IncParseFailure call sites); writing to the global singleton
	// here would break that test-isolation contract.
	// One timestamp for the whole scrape so threshold values and expiry events
	// resolve consistently — a separate time.Now() per resolver could straddle an
	// `expires:` boundary and emit a one-scrape value/event mismatch (CodeRabbit).
	now := time.Now()
	resolved, stats := cfg.ResolveAtWithStats(now)
	c.manager.getMetrics().PublishTenantMetricsOverLimit(stats.PerTenantOverLimit)

	// Each metric family is emitted by a focused collector method; order is
	// irrelevant (Prometheus sorts on Gather) but kept stable for diff clarity.
	c.collectThresholds(ch, resolved)
	c.collectCustomAlertErrors(ch, stats.PerTenantCustomAlertErrors)
	c.collectStateFilters(ch, cfg)
	c.collectSilentModes(ch, cfg)
	c.collectMaintenanceExpiries(ch, cfg)
	c.collectThresholdExpiries(ch, cfg, now)
	c.collectSeverityDedup(ch, cfg)
	c.collectConfigInfo(ch)
	c.collectMetadata(ch, cfg)
	c.collectTenantExpectedExporter(ch, cfg)
}

// collectThresholds emits the user_threshold gauge for every resolved
// threshold (Scenario A numeric + Phase 2B dimensional). Custom labels are
// appended sorted; regex labels get the _re suffix for PromQL matching.
func (c *ThresholdCollector) collectThresholds(ch chan<- prometheus.Metric, resolved []ResolvedThreshold) {
	for _, t := range resolved {
		labelNames := []string{"tenant", "metric", "component", "severity"}
		labelValues := []string{t.Tenant, t.Metric, t.Component, t.Severity}

		// Append custom labels in sorted order for deterministic output
		if len(t.CustomLabels) > 0 {
			keys := make([]string, 0, len(t.CustomLabels))
			for k := range t.CustomLabels {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for _, k := range keys {
				labelNames = append(labelNames, k)
				labelValues = append(labelValues, t.CustomLabels[k])
			}
		}

		// Phase 11 B1: append regex labels with _re suffix for PromQL matching.
		// Exporter outputs the regex pattern as a label value; recording rules
		// use label_replace + =~ to match actual metrics at query time.
		if len(t.RegexLabels) > 0 {
			keys := make([]string, 0, len(t.RegexLabels))
			for k := range t.RegexLabels {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for _, k := range keys {
				labelNames = append(labelNames, k+"_re")
				labelValues = append(labelValues, t.RegexLabels[k])
			}
		}

		desc := prometheus.NewDesc(
			"user_threshold",
			"User-defined alerting threshold (config-driven, three-state: custom/default/disable)",
			labelNames,
			nil,
		)
		m, err := prometheus.NewConstMetric(desc, prometheus.GaugeValue, t.Value, labelValues...)
		if err != nil {
			log.Printf("WARN: failed to create user_threshold metric for tenant=%s metric=%s: %v", t.Tenant, t.Metric, err)
			continue
		}
		ch <- m
	}
}

// collectCustomAlertErrors emits da_custom_alert_parse_errors (#741 S3a,
// fail-loud) per tenant. ConstMetric per scrape so a fixed tenant stops
// emitting rather than leaving a stale GaugeVec series.
func (c *ThresholdCollector) collectCustomAlertErrors(ch chan<- prometheus.Metric, caErrors map[string]int) {
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

// collectStateFilters emits user_state_filter flags (Scenario C).
func (c *ThresholdCollector) collectStateFilters(ch chan<- prometheus.Metric, cfg *ThresholdConfig) {
	stateDesc := prometheus.NewDesc(
		"user_state_filter",
		"State-based monitoring filter flag (1=enabled, absent=disabled). Scenario C: state/string matching.",
		[]string{"tenant", "filter", "severity"},
		nil,
	)
	for _, sf := range cfg.ResolveStateFilters() {
		m, err := prometheus.NewConstMetric(stateDesc, prometheus.GaugeValue, 1.0, sf.Tenant, sf.FilterName, sf.Severity)
		if err != nil {
			log.Printf("WARN: failed to create user_state_filter metric for tenant=%s filter=%s: %v", sf.Tenant, sf.FilterName, err)
			continue
		}
		ch <- m
	}
}

// collectSilentModes emits user_silent_mode for active silences; expired
// silences emit da_config_event instead (v1.7.0) so Alertmanager inhibit
// stops and notifications resume.
func (c *ThresholdCollector) collectSilentModes(ch chan<- prometheus.Metric, cfg *ThresholdConfig) {
	silentDesc := prometheus.NewDesc(
		"user_silent_mode",
		"Silent mode flag (1=active). Alerts fire (TSDB records) but notifications suppressed via Alertmanager inhibit.",
		[]string{"tenant", "target_severity"},
		nil,
	)
	// da_config_event: ephemeral gauge emitted when a timed config (silent/maintenance)
	// has expired. Used by TenantConfigEvent alert rule (for: 0s) to notify operators.
	// The metric is emitted as long as the expired config YAML remains — once the tenant
	// removes or updates the config, this metric disappears (stale marker).
	configEventDesc := prometheus.NewDesc(
		"da_config_event",
		"Config lifecycle event (1=event active). Emitted when timed config expires. Labels identify event type and tenant.",
		[]string{"tenant", "event", "reason"},
		nil,
	)
	for _, sm := range cfg.ResolveSilentModes() {
		if sm.Expired {
			// Expired: emit config event instead of sentinel metric
			reason := sm.Reason
			if reason == "" {
				reason = "silent_mode expired for " + sm.TargetSeverity
			}
			m, err := prometheus.NewConstMetric(configEventDesc, prometheus.GaugeValue, 1.0,
				sm.Tenant, "silence_expired", reason)
			if err != nil {
				log.Printf("WARN: failed to create da_config_event metric for tenant=%s: %v", sm.Tenant, err)
				continue
			}
			ch <- m
			continue
		}
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
func (c *ThresholdCollector) collectMaintenanceExpiries(ch chan<- prometheus.Metric, cfg *ThresholdConfig) {
	configEventDesc := prometheus.NewDesc(
		"da_config_event",
		"Config lifecycle event (1=event active). Emitted when timed config expires. Labels identify event type and tenant.",
		[]string{"tenant", "event", "reason"},
		nil,
	)
	for _, me := range cfg.ResolveMaintenanceExpiries() {
		if !me.Expired {
			continue // Still active, no event needed
		}
		reason := me.Reason
		if reason == "" {
			reason = "maintenance_mode expired"
		}
		m, err := prometheus.NewConstMetric(configEventDesc, prometheus.GaugeValue, 1.0,
			me.Tenant, "maintenance_expired", reason)
		if err != nil {
			log.Printf("WARN: failed to create da_config_event metric for tenant=%s: %v", me.Tenant, err)
			continue
		}
		ch <- m
	}
}

// collectThresholdExpiries emits da_config_event when a time-boxed threshold
// override has lapsed (PREVENT #656). The threshold VALUE itself already
// fail-safed back to the platform default in resolveBaseRows; this event lets a
// cleanup PR remove the stale conf.d YAML and gives operators visibility. The
// metric key is encoded into the reason so each (tenant, metric) event is a
// distinct da_config_event series (the label set is {tenant,event,reason}, so a
// shared user reason on two metrics would otherwise collide into one series).
func (c *ThresholdCollector) collectThresholdExpiries(ch chan<- prometheus.Metric, cfg *ThresholdConfig, now time.Time) {
	configEventDesc := prometheus.NewDesc(
		"da_config_event",
		"Config lifecycle event (1=event active). Emitted when timed config expires. Labels identify event type and tenant.",
		[]string{"tenant", "event", "reason"},
		nil,
	)
	for _, te := range cfg.ResolveThresholdExpiriesAt(now) {
		if !te.Expired {
			continue // not yet past its TTL — the override is still active
		}
		reason := "threshold " + te.MetricKey + " expired"
		if te.Reason != "" {
			reason = te.MetricKey + ": " + te.Reason
		}
		m, err := prometheus.NewConstMetric(configEventDesc, prometheus.GaugeValue, 1.0,
			te.Tenant, "threshold_expired", reason)
		if err != nil {
			log.Printf("WARN: failed to create da_config_event metric for tenant=%s: %v", te.Tenant, err)
			continue
		}
		ch <- m
	}
}

// collectSeverityDedup emits user_severity_dedup flags (v1.2.0+).
func (c *ThresholdCollector) collectSeverityDedup(ch chan<- prometheus.Metric, cfg *ThresholdConfig) {
	dedupDesc := prometheus.NewDesc(
		"user_severity_dedup",
		"Severity dedup flag (1=enabled). Warning notifications suppressed when critical fires for same metric_group. v1.2.0+",
		[]string{"tenant", "mode"},
		nil,
	)
	for _, sd := range cfg.ResolveSeverityDedup() {
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
func (c *ThresholdCollector) collectConfigInfo(ch chan<- prometheus.Metric) {
	configInfoDesc := prometheus.NewDesc(
		"threshold_exporter_config_info",
		"Config source metadata (info metric, always 1). Labels identify deployment mode and git revision. v2.3.0+",
		[]string{"config_source", "git_commit"},
		nil,
	)
	info := c.manager.GetConfigInfo()
	if m, err := prometheus.NewConstMetric(configInfoDesc, prometheus.GaugeValue, 1.0,
		info.ConfigSource, info.GitCommit); err == nil {
		ch <- m
	}
}

// collectMetadata emits tenant_metadata_info (v1.11.0): unconditional per-tenant
// runbook_url/owner/tier labels for Alertmanager group_left joins.
func (c *ThresholdCollector) collectMetadata(ch chan<- prometheus.Metric, cfg *ThresholdConfig) {
	metadataDesc := prometheus.NewDesc(
		"tenant_metadata_info",
		"Tenant metadata labels (info metric, always 1). Unconditional output for group_left joins. v1.11.0+",
		[]string{"tenant", "runbook_url", "owner", "tier"},
		nil,
	)
	for _, md := range cfg.ResolveMetadata() {
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
// ResolveMetadata() returns EVERY tenant unconditionally, and a tenant with no
// _metadata resolves to DBType=="" (resolve.go:763-765). Emitting db_type="" for
// such tenants would put a left-hand series into the anti-join for a tenant that
// has no corresponding `<db>_up` series at all → the rule would fire a permanent
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
func (c *ThresholdCollector) collectTenantExpectedExporter(ch chan<- prometheus.Metric, cfg *ThresholdConfig) {
	expectedDesc := prometheus.NewDesc(
		"tenant_expected_exporter",
		"Liveness expectation (always 1) for each tenant that declares a db_type in _metadata. "+
			"LHS of the TenantExporterAbsent anti-join (#869). One series per declaring tenant.",
		[]string{"tenant", "db_type"},
		nil,
	)
	for _, md := range cfg.ResolveMetadata() {
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

// MetricsHandler returns an HTTP handler that serves /metrics
// with both default Go metrics and our custom threshold collector.
func (c *ThresholdCollector) MetricsHandler() http.Handler {
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	// Also register default Go collector for process metrics
	reg.MustRegister(prometheus.NewGoCollector())

	// v2.7.0 Phase 4: register hierarchical scan / reload metrics on the
	// same registry so /metrics serves them alongside everything else.
	// getConfigMetrics lazily instantiates the singleton so the first
	// metric increment from a scan goroutine does not race with
	// registration (registration itself takes a mutex internally).
	registerConfigMetrics(reg, getConfigMetrics())

	return promhttp.HandlerFor(reg, promhttp.HandlerOpts{
		EnableOpenMetrics: false,
	})
}
