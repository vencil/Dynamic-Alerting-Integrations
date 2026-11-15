package scrape

import (
	"testing"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// unsetSeries gathers /metrics for cfg and returns tenant → key → fix of the
// user_threshold_unset series (#1200 D-A A3), checking pack and value.
func unsetSeries(t *testing.T, cfg *config.ThresholdConfig) map[string]map[string]string {
	t.Helper()
	reg := prometheus.NewRegistry()
	Register(reg, NewCollectorWithHooks(staticSource{cfg, scrapeAt}, Hooks{}), NewConfigMetrics())
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	packs := config.UnsetThresholdKeys()
	out := map[string]map[string]string{}
	for _, mf := range mfs {
		if mf.GetName() != "user_threshold_unset" {
			continue
		}
		for _, m := range mf.GetMetric() {
			l := map[string]string{}
			for _, lp := range m.GetLabel() {
				l[lp.GetName()] = lp.GetValue()
			}
			if m.GetGauge().GetValue() != 1 || packs[l["key"]] != l["pack"] {
				t.Errorf("%v: value %v / pack, want 1 / %q", l, m.GetGauge().GetValue(), packs[l["key"]])
			}
			if out[l["tenant"]] == nil {
				out[l["tenant"]] = map[string]string{}
			}
			out[l["tenant"]][l["key"]] = l["fix"]
		}
	}
	return out
}

// /metrics serves user_threshold_unset only for a tenant whose state filter
// unset_thresholds is on, one series per key that serves it no threshold and
// that it does not disable, with fix naming who can arm it.
func TestCollector_ServesUnsetThresholdsToSubscribedTenants(t *testing.T) {
	t.Parallel()
	cfg := &config.ThresholdConfig{
		Defaults:          map[string]float64{"container_cpu": 80},
		OptionalOverrides: []string{"oracle_wait_time_rate", "oracle_process_count"},
		StateFilters:      map[string]config.StateFilter{config.UnsetThresholdsFilter: {DefaultState: "disable"}},
		Tenants: map[string]map[string]config.ScheduledValue{
			"ta": {
				"_state_unset_thresholds": svYAML(`enable`),
				"oracle_wait_time_rate":   svYAML(`"5"`),
				"oracle_process_count":    svYAML(`"disable"`),
			},
			"tb": {"oracle_wait_time_rate": svYAML(`"5"`)}, // not subscribed
		},
	}
	all := unsetSeries(t, cfg)
	if _, ok := all["tb"]; ok {
		t.Errorf("tb did not subscribe: no series, got %v", all["tb"])
	}
	got := all["ta"]
	if got["redis_replication_lag"] != config.UnsetFixPlatform {
		t.Errorf("redis_replication_lag (not declared): fix %q, want platform", got["redis_replication_lag"])
	}
	for _, configured := range []string{"oracle_wait_time_rate", "oracle_process_count", "container_cpu"} {
		if _, ok := got[configured]; ok {
			t.Errorf("%s is served or disabled (or ships armed): no series", configured)
		}
	}
	if len(got) != len(config.UnsetThresholdKeys())-2 {
		t.Errorf("%d unset series, want every generated key but the two configured (%d)",
			len(got), len(config.UnsetThresholdKeys())-2)
	}
}
