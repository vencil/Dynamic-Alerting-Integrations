package scrape

// With Hooks.Now set (da-guard served-values' --at), every family resolves at
// that instant — including the maintenance expiry event, which the exporter
// (Now nil) resolves at the wall clock.

import (
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/pkg/config"
)

type staticSource struct{ cfg *config.ThresholdConfig }

func (s staticSource) GetConfig() *config.ThresholdConfig { return s.cfg }
func (s staticSource) GetConfigInfo() config.ConfigInfo   { return config.ConfigInfo{} }

// maintenanceExpiredEvents gathers the registry served-values builds, at `at`,
// and counts da_config_event{event="maintenance_expired"} series.
func maintenanceExpiredEvents(t *testing.T, cfg *config.ThresholdConfig, at time.Time) int {
	t.Helper()
	metrics := NewConfigMetrics()
	c := NewCollectorWithHooks(staticSource{cfg}, Hooks{
		Now: func() time.Time { return at },
	})
	reg := prometheus.NewRegistry()
	Register(reg, c, metrics)
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	n := 0
	for _, mf := range mfs {
		if mf.GetName() != "da_config_event" {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "event" && lp.GetValue() == "maintenance_expired" {
					n++
				}
			}
		}
	}
	return n
}

func TestCollector_MaintenanceExpiryFollowsHooksNow(t *testing.T) {
	t.Parallel()
	cfg := &config.ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]config.ScheduledValue{
			"tenant-a": {"_state_maintenance": {Default: "target: enable\nexpires: \"2026-08-01T00:00:00Z\"\nreason: window\n"}},
		},
	}
	before := time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
	after := time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC)
	if n := maintenanceExpiredEvents(t, cfg, before); n != 0 {
		t.Errorf("at %s (before expires): %d maintenance_expired events, want 0", before, n)
	}
	if n := maintenanceExpiredEvents(t, cfg, after); n != 1 {
		t.Errorf("at %s (after expires): %d maintenance_expired events, want 1", after, n)
	}
}
