package main

// served_series.go — served-values' `series` (#2750): for each threshold key,
// which series of the exporter's /metrics its rows are.
//
// ⛔ NO NAMING OF ITS OWN. A key's series are read back from the Gather of the
// registry the reading came from: the metric the exporter's own builder
// (thresholdmetric.Emit) made for each of the key's rows is looked up, by its
// label set and value, among the series that Gather returned, and the name is
// the name of the family it was found in. Nothing here builds a metric name
// or a label from a key. metric_key is the resolver's own (the key it parsed
// the row's component / metric labels from, config.KeyedThreshold.MetricKey),
// checked against those labels with the parser the resolver uses.

import (
	"fmt"
	"sort"
	"strconv"
	"strings"

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// metricsSeries is one /metrics series a threshold key is served as.
//
// Name and Labels are the series as the Gather of the exporter's /metrics
// returns it: the family name and every label of the series, the same set a
// selector on /metrics matches exactly. MetricKey is the metric key the
// resolver parsed the row's `component` / `metric` labels from: the key
// itself for a plain key, `X` for `X_critical`'s critical row and for a
// dimensional `X{...}` row, the retired base for a #1231 legacy twin.
// Dimensions / DimensionsRegex are the row's dimensional labels as the key
// writes them (`{db="a"}` → {"db": "a"}; `{ts=~"SYS.*"}` → DimensionsRegex
// {"ts": "SYS.*"}, served as the label `ts_re`); {} for a key without.
type metricsSeries struct {
	Name            string            `json:"name"`
	Labels          map[string]string `json:"labels"`
	MetricKey       string            `json:"metric_key"`
	Dimensions      map[string]string `json:"dimensions"`
	DimensionsRegex map[string]string `json:"dimensions_regex"`
}

// gatheredSeries indexes the series of one Gather by label set and value:
// seriesID → the names of the families holding such a series.
type gatheredSeries map[string][]string

func indexGathered(mfs []*dto.MetricFamily) gatheredSeries {
	idx := gatheredSeries{}
	for _, mf := range mfs {
		for _, m := range mf.GetMetric() {
			id := seriesID(m)
			idx[id] = append(idx[id], mf.GetName())
		}
	}
	return idx
}

// seriesID is a series' label set and value, in a canonical form.
func seriesID(m *dto.Metric) string {
	pairs := make([]string, 0, len(m.GetLabel()))
	for _, lp := range m.GetLabel() {
		pairs = append(pairs, strconv.Quote(lp.GetName())+"="+strconv.Quote(lp.GetValue()))
	}
	sort.Strings(pairs)
	var v float64
	switch {
	case m.GetGauge() != nil:
		v = m.GetGauge().GetValue()
	case m.GetCounter() != nil:
		v = m.GetCounter().GetValue()
	case m.GetUntyped() != nil:
		v = m.GetUntyped().GetValue()
	}
	return strings.Join(pairs, ",") + " " + strconv.FormatFloat(v, 'g', -1, 64)
}

// keySeries is, per tenant and threshold key, the series of the rows the
// collector kept (results[i].metric set), looked up in the Gather the
// results came from. `_custom_alerts` has none: its rows are listed in its
// value. A kept row whose metric is not exactly one series of that Gather,
// or whose metric key does not parse to its labels, is an internal error.
func keySeries(keyed []config.KeyedThreshold, results []emitResult, gathered gatheredSeries) (map[string]map[string][]metricsSeries, error) {
	if len(results) != len(keyed) {
		return nil, fmt.Errorf("internal: the collector resolved %d rows, %d have a verdict", len(keyed), len(results))
	}
	out := map[string]map[string][]metricsSeries{}
	for i, k := range keyed {
		r := results[i]
		if r.metric == nil || r.err != nil || k.Key == customAlertsKey {
			continue
		}
		var pb dto.Metric
		if err := r.metric.Write(&pb); err != nil {
			return nil, fmt.Errorf("internal: tenant %s: key %q: the collector's metric cannot be read: %v", k.Tenant, k.Key, err)
		}
		names := gathered[seriesID(&pb)]
		if len(names) != 1 {
			return nil, fmt.Errorf("internal: tenant %s: key %q: the row's metric is %d series of the Gather, want 1", k.Tenant, k.Key, len(names))
		}
		labels := make(map[string]string, len(pb.GetLabel()))
		for _, lp := range pb.GetLabel() {
			labels[lp.GetName()] = lp.GetValue()
		}
		if c, m := config.ParseMetricKey(k.MetricKey); k.MetricKey == "" || c != k.Component || m != k.Metric {
			return nil, fmt.Errorf("internal: tenant %s: key %q: the resolver's metric key %q is not the row's component %q / metric %q",
				k.Tenant, k.Key, k.MetricKey, k.Component, k.Metric)
		}
		if out[k.Tenant] == nil {
			out[k.Tenant] = map[string][]metricsSeries{}
		}
		out[k.Tenant][k.Key] = append(out[k.Tenant][k.Key], metricsSeries{
			Name:            names[0],
			Labels:          labels,
			MetricKey:       k.MetricKey,
			Dimensions:      copyLabels(k.CustomLabels),
			DimensionsRegex: copyLabels(k.RegexLabels),
		})
	}
	return out, nil
}

func copyLabels(m map[string]string) map[string]string {
	out := make(map[string]string, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

// sameSeries reports whether a and b are the same series, in the same order.
func sameSeries(a, b []metricsSeries) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i].Name != b[i].Name || a[i].MetricKey != b[i].MetricKey ||
			!sameLabels(a[i].Labels, b[i].Labels) || !sameLabels(a[i].Dimensions, b[i].Dimensions) ||
			!sameLabels(a[i].DimensionsRegex, b[i].DimensionsRegex) {
			return false
		}
	}
	return true
}

func sameLabels(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if w, ok := b[k]; !ok || w != v {
			return false
		}
	}
	return true
}

// gatherSeries is reg's Gather, with its series indexed (indexGathered).
func gatherSeries(reg *prometheus.Registry) (gatheredSeries, error) {
	mfs, err := reg.Gather()
	if err != nil {
		return nil, err
	}
	return indexGathered(mfs), nil
}
