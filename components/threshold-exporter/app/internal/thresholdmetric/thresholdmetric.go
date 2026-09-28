// Package thresholdmetric builds the user_threshold series the exporter's
// /metrics serves. It is the collector's own code, moved out of package main
// so `da-guard served-values` (#2115) can run the very same code over a
// private registry and read what /metrics would actually carry — instead of
// re-deriving which rows client_golang accepts.
package thresholdmetric

import (
	"log"
	"sort"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// Emit sends the user_threshold gauge for every resolved threshold (Scenario
// A numeric + Phase 2B dimensional) to ch. Custom labels are appended sorted;
// regex labels get the _re suffix for PromQL matching. A row whose metric
// cannot be built is logged and dropped, as it always was.
//
// report is nil on the exporter's path, where a dropped row is logged (WARN,
// the process-global logger). When non-nil it is told, for each row i of
// resolved, either the metric built and sent, or the error that dropped the
// row — and the drop is left to it, not logged: a caller outside the exporter
// (da-guard) reports it in its own output instead of the process's log.
func Emit(ch chan<- prometheus.Metric, resolved []config.ResolvedThreshold, report func(i int, m prometheus.Metric, err error)) {
	for i, t := range resolved {
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
			// ⚠️ The three states hold for keys the platform gives a default;
			// a declared key (optional_overrides) has none, so it is
			// custom-or-silent — saying otherwise here would be the same
			// untrue claim #1321 removed from the tenant-facing files.
			"User-defined alerting threshold (config-driven, three-state: custom/default/disable; declared keys have no default: custom or silent)",
			labelNames,
			nil,
		)
		m, err := prometheus.NewConstMetric(desc, prometheus.GaugeValue, t.Value, labelValues...)
		if err != nil {
			if report != nil {
				report(i, nil, err)
			} else {
				log.Printf("WARN: failed to create user_threshold metric for tenant=%s metric=%s: %v", t.Tenant, t.Metric, err)
			}
			continue
		}
		if report != nil {
			report(i, m, nil)
		}
		ch <- m
	}
}
