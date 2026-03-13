package main

import (
	"log"
	"net/http"
	"sort"

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

	// Scenario A + Phase 2B: numeric thresholds (with optional dimensional labels)
	for _, t := range cfg.Resolve() {
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

	// Scenario C: state filter flags
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

	// Silent mode: per-tenant notification suppression flags.
	// Alerts still fire in Prometheus (TSDB records exist), but Alertmanager
	// uses sentinel alerts + inhibit_rules to suppress notifications.
	// Distinct from maintenance mode which suppresses at PromQL level (no TSDB records).
	//
	// v1.7.0: expired entries (Expired=true) do NOT emit user_silent_mode —
	// sentinel metric disappears → Alertmanager inhibit stops → notifications resume.
	// Instead, expired entries emit da_config_event for notification purposes.
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

	// Maintenance mode expiry events (v1.7.0).
	// When structured _state_maintenance with expires is past, the state filter
	// metric already stops emitting (handled in ResolveStateFiltersAt).
	// Here we emit da_config_event to notify that maintenance auto-deactivated.
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

	// Severity dedup: per-tenant warning↔critical notification deduplication.
	// When enabled (default), Alertmanager inhibit rules suppress warning notifications
	// when critical fires for the same tenant+metric_group.
	// Both alerts ALWAYS fire in TSDB regardless of this setting — dedup only controls notifications.
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

	// Tenant metadata info metric (v1.11.0): unconditionally emitted for ALL tenants.
	// Carries runbook_url, owner, tier as labels. Unset fields default to empty string.
	// Rule Pack Part 3 alert rules use group_left(runbook_url, owner, tier) to inherit
	// these labels, enabling dynamic runbook injection in Alertmanager notifications.
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

// MetricsHandler returns an HTTP handler that serves /metrics
// with both default Go metrics and our custom threshold collector.
func (c *ThresholdCollector) MetricsHandler() http.Handler {
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	// Also register default Go collector for process metrics
	reg.MustRegister(prometheus.NewGoCollector())

	return promhttp.HandlerFor(reg, promhttp.HandlerOpts{
		EnableOpenMetrics: false,
	})
}
