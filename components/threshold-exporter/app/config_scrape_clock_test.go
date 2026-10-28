package main

// #3368: the manager's clock is the one clock of a scrape. Every family —
// the threshold rows, and the maintenance_expired event that used to re-read
// the wall clock — resolves at ConfigManager.Now, so a test that sets it
// pins the whole scrape, and the scrape never straddles two instants.

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/jonboulle/clockwork"
	"github.com/prometheus/client_golang/prometheus"
)

// maintenanceExpiredCount is the number of da_config_event{event=
// "maintenance_expired"} series one gather of m's /metrics collector serves.
func maintenanceExpiredCount(t *testing.T, m *ConfigManager) int {
	t.Helper()
	reg := prometheus.NewRegistry()
	if err := reg.Register(NewThresholdCollector(m)); err != nil {
		t.Fatalf("register collector: %v", err)
	}
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	n := 0
	for _, fam := range families {
		if fam.GetName() != "da_config_event" {
			continue
		}
		for _, mm := range fam.Metric {
			for _, pair := range mm.Label {
				if pair.GetName() == "event" && pair.GetValue() == "maintenance_expired" {
					n++
				}
			}
		}
	}
	return n
}

func TestScrapeResolvesAtTheManagersClock(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  mysql_connections: 50\n"+
			"state_filters:\n  maintenance:\n    reasons: [\"x\"]\n    severity: warning\n")
	writeTestYAML(t, filepath.Join(dir, "t1.yaml"),
		"tenants:\n  t1:\n"+
			"    mysql_connections:\n      default: \"60\"\n"+
			"      overrides:\n        - window: \"00:00-23:59\"\n          value: \"70\"\n"+
			"    _state_maintenance:\n      target: enable\n"+
			"      expires: \"2026-08-01T12:00:00Z\"\n      reason: r\n")
	m := NewConfigManager(dir)
	defer m.Close()
	clock := clockwork.NewFakeClockAt(time.Date(2026, 8, 1, 10, 0, 0, 0, time.UTC))
	m.SetClock(clock)
	captureLoad(t, m)

	for _, tc := range []struct {
		name    string
		at      time.Time
		want    float64
		expired int
	}{
		// Inside the window, before expires.
		{"10:00", time.Date(2026, 8, 1, 10, 0, 0, 0, time.UTC), 70, 0},
		// The window is end-exclusive: shut for the 23:59 minute. The
		// maintenance expired at 12:00 — the event follows the same clock.
		{"23:59:30", time.Date(2026, 8, 1, 23, 59, 30, 0, time.UTC), 60, 1},
	} {
		clock.Advance(tc.at.Sub(clock.Now()))
		if got, ok := seriesFor(t, m, "t1", "connections"); !ok || got != tc.want {
			t.Errorf("at %s: connections = %v (present=%v), want %v", tc.name, got, ok, tc.want)
		}
		if got := maintenanceExpiredCount(t, m); got != tc.expired {
			t.Errorf("at %s: %d maintenance_expired events, want %d", tc.name, got, tc.expired)
		}
	}
}
