package scrape

// The config metrics moved here from package main (#2115); #2254 (#2153) added
// three gauges and appended scan / reload buckets. This pins that what Register
// installs still carries them, so the registry da-guard served-values builds
// and the exporter's /metrics registry both expose them.

import (
	"reflect"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"
)

func TestRegister_ConfigShapeGaugesAndAppendedBuckets(t *testing.T) {
	reg := prometheus.NewRegistry()
	placeholder := prometheus.NewGauge(prometheus.GaugeOpts{Name: "placeholder_collector"})
	Register(reg, placeholder, NewConfigMetrics())

	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("Gather: %v", err)
	}
	byName := map[string]*dto.MetricFamily{}
	for _, mf := range mfs {
		byName[mf.GetName()] = mf
	}

	for _, name := range []string{
		"da_config_max_tenants_per_file",
		"da_config_max_mapping_keys",
		"da_config_initial_load_duration_seconds",
	} {
		mf, ok := byName[name]
		if !ok {
			t.Errorf("%s not gathered after Register", name)
			continue
		}
		if mf.GetType() != dto.MetricType_GAUGE {
			t.Errorf("%s type = %v, want GAUGE", name, mf.GetType())
		}
		if len(mf.GetMetric()) != 1 {
			t.Errorf("%s series = %d, want 1", name, len(mf.GetMetric()))
		}
	}

	for name, want := range map[string][]float64{
		"da_config_scan_duration_seconds":   {0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 120},
		"da_config_reload_duration_seconds": {0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600},
	} {
		mf, ok := byName[name]
		if !ok || len(mf.GetMetric()) != 1 {
			t.Errorf("%s not gathered as one series after Register", name)
			continue
		}
		var got []float64
		for _, b := range mf.GetMetric()[0].GetHistogram().GetBucket() {
			got = append(got, b.GetUpperBound())
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("%s buckets = %v, want %v", name, got, want)
		}
	}
}
