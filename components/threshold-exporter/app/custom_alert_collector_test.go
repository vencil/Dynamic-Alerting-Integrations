package main

import (
	"testing"
)

// TestCollector_CustomAlerts_EmitAndParseErrorGauge proves the S3a data plane
// end-to-end at the collector level: a valid tenant _custom_alerts declaration
// emits a user_threshold{component="custom", recipe_id, name, mode} series, and
// a malformed declaration is dropped (not silently — it raises the
// da_custom_alert_parse_errors gauge while leaving other tenants intact).
func TestCollector_CustomAlerts_EmitAndParseErrorGauge(t *testing.T) {
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			// valid: one threshold custom alert (mode omitted → default page)
			"good-t": {"_custom_alerts": {Default: "- {recipe: threshold, name: q_high, metric: qd, op: \">\", window: 5m, threshold: \"100:warning\"}\n"}},
			// malformed: scalar where a list is expected → dropped + counted
			"bad-t": {"_custom_alerts": {Default: "not a yaml list\n"}},
		},
	}
	manager := newTestManager(cfg)
	fresh, reg := freshMetrics(t)
	manager.SetMetrics(fresh)

	collector := NewThresholdCollector(manager)
	reg.MustRegister(collector) // gather user_threshold + the gauges in one pass

	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}

	// (1) the valid custom alert surfaces as user_threshold{component="custom",...}
	var found bool
	for _, mf := range mfs {
		if mf.GetName() != "user_threshold" {
			continue
		}
		for _, m := range mf.GetMetric() {
			lbl := map[string]string{}
			for _, lp := range m.GetLabel() {
				lbl[lp.GetName()] = lp.GetValue()
			}
			if lbl["tenant"] == "good-t" && lbl["component"] == "custom" {
				found = true
				if lbl["recipe_id"] != "threshold__qd__gt__w5m" || lbl["name"] != "q_high" || lbl["mode"] != "page" {
					t.Errorf("custom-alert user_threshold labels = %v (want recipe_id=threshold__qd__gt__w5m, name=q_high, mode=page)", lbl)
				}
				if v := m.GetGauge().GetValue(); v != 100 {
					t.Errorf("custom-alert user_threshold value = %v, want 100", v)
				}
			}
		}
	}
	if !found {
		t.Error("no user_threshold{component=\"custom\", tenant=\"good-t\"} emitted")
	}

	// (2) malformed declaration → da_custom_alert_parse_errors fires for bad-t,
	// stays 0 for good-t (fail-loud). The collector emits this as a ConstMetric
	// per scrape (no GaugeVec Reset+Set race), so we read it from the gathered
	// metric family rather than a registered gauge handle.
	parseErrs := map[string]float64{}
	for _, mf := range mfs {
		if mf.GetName() != "da_custom_alert_parse_errors" {
			continue
		}
		for _, m := range mf.GetMetric() {
			var tenant string
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "tenant" {
					tenant = lp.GetValue()
				}
			}
			parseErrs[tenant] = m.GetGauge().GetValue()
		}
	}
	if v, ok := parseErrs["bad-t"]; !ok || v != 1 {
		t.Errorf("da_custom_alert_parse_errors{bad-t} = %v (present=%v), want 1", v, ok)
	}
	if v, ok := parseErrs["good-t"]; !ok || v != 0 {
		t.Errorf("da_custom_alert_parse_errors{good-t} = %v (present=%v), want 0 (must emit 0, not omit)", v, ok)
	}
}
