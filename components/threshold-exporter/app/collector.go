package main

import (
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
			continue
		}
		ch <- m
	}

	// Silent mode: per-tenant notification suppression flags.
	// Alerts still fire in Prometheus (TSDB records exist), but Alertmanager
	// uses sentinel alerts + inhibit_rules to suppress notifications.
	// Distinct from maintenance mode which suppresses at PromQL level (no TSDB records).
	silentDesc := prometheus.NewDesc(
		"user_silent_mode",
		"Silent mode flag (1=active). Alerts fire (TSDB records) but notifications suppressed via Alertmanager inhibit.",
		[]string{"tenant", "target_severity"},
		nil,
	)
	for _, sm := range cfg.ResolveSilentModes() {
		m, err := prometheus.NewConstMetric(silentDesc, prometheus.GaugeValue, 1.0, sm.Tenant, sm.TargetSeverity)
		if err != nil {
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
