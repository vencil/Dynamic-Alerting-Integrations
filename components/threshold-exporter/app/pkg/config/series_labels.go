package config

import "sort"

// SeriesLabels is the label set a threshold row is exported with as
// user_threshold: tenant, metric, component, severity, then its exact
// dimensional labels sorted by name, then its regex dimensional labels
// sorted by name with the `_re` suffix. The collector builds every
// user_threshold series from it, and Prometheus treats two rows with the same
// metric name and label set as one series collected twice — which fails the
// whole scrape — so this is also the identity that rules out a duplicate.
func (t ResolvedThreshold) SeriesLabels() (names, values []string) {
	names = []string{"tenant", "metric", "component", "severity"}
	values = []string{t.Tenant, t.Metric, t.Component, t.Severity}

	// Custom labels in sorted order for deterministic output.
	if len(t.CustomLabels) > 0 {
		keys := make([]string, 0, len(t.CustomLabels))
		for k := range t.CustomLabels {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			names = append(names, k)
			values = append(values, t.CustomLabels[k])
		}
	}

	// Phase 11 B1: regex labels with the _re suffix for PromQL matching. The
	// exporter outputs the regex pattern as a label value; recording rules
	// use label_replace + =~ to match actual metrics at query time.
	if len(t.RegexLabels) > 0 {
		keys := make([]string, 0, len(t.RegexLabels))
		for k := range t.RegexLabels {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			names = append(names, k+"_re")
			values = append(values, t.RegexLabels[k])
		}
	}
	return names, values
}
