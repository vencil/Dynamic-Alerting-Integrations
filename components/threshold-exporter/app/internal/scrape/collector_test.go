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

// Hooks.Observe gets, once per scrape, the readings the scrape emitted from
// (#2374): served-values reads the reserved keys from it instead of calling
// the resolvers a second time.
func TestCollector_ObserveOncePerScrape(t *testing.T) {
	t.Parallel()
	cfg := &config.ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]config.ScheduledValue{
			"tenant-a": {
				"_metadata":       {Default: "owner: team-a\nregion: r1\n"},
				"_severity_dedup": {Default: "disable"},
				"_silent_mode":    {Default: "warning"},
			},
			"tenant-b": {},
		},
	}
	var got []Reserved
	c := NewCollectorWithHooks(staticSource{cfg}, Hooks{Observe: func(r Reserved) { got = append(got, r) }})
	reg := prometheus.NewRegistry()
	Register(reg, c, NewConfigMetrics())
	if _, err := reg.Gather(); err != nil {
		t.Fatalf("gather: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("Observe called %d times, want 1", len(got))
	}
	r := got[0]
	if len(r.Metadata) != 2 || r.Metadata[0].Tenant != "tenant-a" || r.Metadata[0].Region != "r1" {
		t.Errorf("Metadata = %+v", r.Metadata)
	}
	if len(r.SeverityDedup) != 1 || r.SeverityDedup[0].Tenant != "tenant-b" {
		t.Errorf("SeverityDedup = %+v, want only tenant-b (tenant-a disabled it)", r.SeverityDedup)
	}
	if len(r.Ops.Silences) != 1 || r.Ops.Silences[0].Tenant != "tenant-a" || r.Ops.Silences[0].TargetSeverity != "warning" {
		t.Errorf("Ops.Silences = %+v", r.Ops.Silences)
	}
}

// The readings Observe hands over are resolved at Hooks.Now, not the wall
// clock: served-values' --at moves a silence across its expires with them.
func TestCollector_ObserveFollowsHooksNow(t *testing.T) {
	t.Parallel()
	cfg := &config.ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]config.ScheduledValue{
			"tenant-a": {"_silent_mode": {Default: "target: warning\nexpires: \"2026-08-01T00:00:00Z\"\nreason: window\n"}},
		},
	}
	observe := func(at time.Time) Reserved {
		t.Helper()
		var got []Reserved
		c := NewCollectorWithHooks(staticSource{cfg}, Hooks{
			Now:     func() time.Time { return at },
			Observe: func(r Reserved) { got = append(got, r) },
		})
		reg := prometheus.NewRegistry()
		Register(reg, c, NewConfigMetrics())
		if _, err := reg.Gather(); err != nil {
			t.Fatalf("gather at %s: %v", at, err)
		}
		if len(got) != 1 {
			t.Fatalf("at %s: Observe called %d times, want 1", at, len(got))
		}
		return got[0]
	}
	before := observe(time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC))
	if len(before.Ops.Silences) != 1 || len(before.Ops.ExpiredSilences) != 0 {
		t.Errorf("before expires: Silences=%+v Expired=%+v, want one active silence", before.Ops.Silences, before.Ops.ExpiredSilences)
	}
	after := observe(time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC))
	if len(after.Ops.Silences) != 0 || len(after.Ops.ExpiredSilences) != 1 {
		t.Errorf("after expires: Silences=%+v Expired=%+v, want one expired silence", after.Ops.Silences, after.Ops.ExpiredSilences)
	}
}
