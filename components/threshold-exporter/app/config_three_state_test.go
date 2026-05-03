package main

// Three-state + scheduled value tests — UTC time-window overrides,
// scalar/structured ScheduledValue YAML round-trip, FirstMatchWins
// semantics, cross-midnight windows, ResolveAt at arbitrary times.
// Split out of config_test.go in PR-2; shared helpers live in
// config_test.go.

import (
	"os"
	"path/filepath"
	"sort"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

// region ScheduledValuesAndTimeWindows — YAML parsing, time-window resolution, and scheduled overrides

// ============================================================
// Phase 11 B4: Scheduled Value / Time-Window Override Tests
// ============================================================

func TestScheduledValue_UnmarshalYAML_Scalar(t *testing.T) {
	content := `
tenants:
  db-a:
    mysql_connections: "70"
    mysql_cpu: "disable"
`
	var cfg ThresholdConfig
	if err := yaml.Unmarshal([]byte(content), &cfg); err != nil {
		t.Fatalf("unmarshal failed: %v", err)
	}

	sv := cfg.Tenants["db-a"]["mysql_connections"]
	if sv.Default != "70" {
		t.Errorf("expected Default=70, got %q", sv.Default)
	}
	if len(sv.Overrides) != 0 {
		t.Errorf("expected 0 overrides for scalar, got %d", len(sv.Overrides))
	}
}

func TestScheduledValue_UnmarshalYAML_Structured(t *testing.T) {
	content := `
tenants:
  db-a:
    mysql_connections:
      default: "70"
      overrides:
        - window: "01:00-09:00"
          value: "1000"
        - window: "22:00-06:00"
          value: "500"
`
	var cfg ThresholdConfig
	if err := yaml.Unmarshal([]byte(content), &cfg); err != nil {
		t.Fatalf("unmarshal failed: %v", err)
	}

	sv := cfg.Tenants["db-a"]["mysql_connections"]
	if sv.Default != "70" {
		t.Errorf("expected Default=70, got %q", sv.Default)
	}
	if len(sv.Overrides) != 2 {
		t.Fatalf("expected 2 overrides, got %d", len(sv.Overrides))
	}
	if sv.Overrides[0].Window != "01:00-09:00" || sv.Overrides[0].Value != "1000" {
		t.Errorf("override[0]: got %+v", sv.Overrides[0])
	}
	if sv.Overrides[1].Window != "22:00-06:00" || sv.Overrides[1].Value != "500" {
		t.Errorf("override[1]: got %+v", sv.Overrides[1])
	}
}

func TestScheduledValue_UnmarshalYAML_MixedFormats(t *testing.T) {
	content := `
tenants:
  db-a:
    mysql_connections: "70"
    mysql_cpu:
      default: "80"
      overrides:
        - window: "01:00-09:00"
          value: "disable"
`
	var cfg ThresholdConfig
	if err := yaml.Unmarshal([]byte(content), &cfg); err != nil {
		t.Fatalf("unmarshal failed: %v", err)
	}

	if cfg.Tenants["db-a"]["mysql_connections"].Default != "70" {
		t.Error("scalar format broken")
	}
	if cfg.Tenants["db-a"]["mysql_cpu"].Default != "80" {
		t.Error("structured format default broken")
	}
	if len(cfg.Tenants["db-a"]["mysql_cpu"].Overrides) != 1 {
		t.Error("structured format overrides broken")
	}
}

func TestScheduledValue_ResolveValue_NoOverrides(t *testing.T) {
	sv := SV("70")
	now := time.Date(2026, 1, 15, 3, 0, 0, 0, time.UTC) // 03:00 UTC
	if got := sv.ResolveValue(now); got != "70" {
		t.Errorf("expected 70, got %q", got)
	}
}

func TestScheduledValue_ResolveValue_WindowMatch(t *testing.T) {
	sv := SVScheduled("70",
		TimeWindowOverride{Window: "01:00-09:00", Value: "1000"},
	)

	// 03:00 UTC — inside window
	inside := time.Date(2026, 1, 15, 3, 0, 0, 0, time.UTC)
	if got := sv.ResolveValue(inside); got != "1000" {
		t.Errorf("at 03:00 (inside window), expected 1000, got %q", got)
	}

	// 12:00 UTC — outside window
	outside := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)
	if got := sv.ResolveValue(outside); got != "70" {
		t.Errorf("at 12:00 (outside window), expected 70, got %q", got)
	}
}

func TestScheduledValue_ResolveValue_CrossMidnight(t *testing.T) {
	sv := SVScheduled("70",
		TimeWindowOverride{Window: "22:00-06:00", Value: "500"},
	)

	// 23:00 UTC — inside (after start)
	if got := sv.ResolveValue(time.Date(2026, 1, 15, 23, 0, 0, 0, time.UTC)); got != "500" {
		t.Errorf("at 23:00, expected 500, got %q", got)
	}

	// 03:00 UTC — inside (before end)
	if got := sv.ResolveValue(time.Date(2026, 1, 15, 3, 0, 0, 0, time.UTC)); got != "500" {
		t.Errorf("at 03:00, expected 500, got %q", got)
	}

	// 12:00 UTC — outside
	if got := sv.ResolveValue(time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)); got != "70" {
		t.Errorf("at 12:00, expected 70, got %q", got)
	}

	// 06:00 UTC — boundary (end is exclusive)
	if got := sv.ResolveValue(time.Date(2026, 1, 15, 6, 0, 0, 0, time.UTC)); got != "70" {
		t.Errorf("at 06:00 (boundary), expected 70, got %q", got)
	}

	// 22:00 UTC — boundary (start is inclusive)
	if got := sv.ResolveValue(time.Date(2026, 1, 15, 22, 0, 0, 0, time.UTC)); got != "500" {
		t.Errorf("at 22:00 (boundary), expected 500, got %q", got)
	}
}

func TestScheduledValue_ResolveValue_FirstMatchWins(t *testing.T) {
	sv := SVScheduled("70",
		TimeWindowOverride{Window: "01:00-09:00", Value: "1000"},
		TimeWindowOverride{Window: "03:00-06:00", Value: "2000"},
	)

	// 04:00 UTC — matches both, first wins
	if got := sv.ResolveValue(time.Date(2026, 1, 15, 4, 0, 0, 0, time.UTC)); got != "1000" {
		t.Errorf("at 04:00, expected 1000 (first match), got %q", got)
	}
}

func TestScheduledValue_ResolveValue_DisableWindow(t *testing.T) {
	sv := SVScheduled("70",
		TimeWindowOverride{Window: "01:00-09:00", Value: "disable"},
	)

	// 03:00 UTC — inside window, should resolve to "disable"
	if got := sv.ResolveValue(time.Date(2026, 1, 15, 3, 0, 0, 0, time.UTC)); got != "disable" {
		t.Errorf("at 03:00, expected disable, got %q", got)
	}
}

func TestResolveAt_ScheduledOverride(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SVScheduled("70",
					TimeWindowOverride{Window: "01:00-09:00", Value: "1000"},
				),
			},
		},
	}

	// During backup window
	inside := time.Date(2026, 1, 15, 3, 0, 0, 0, time.UTC)
	resolved := cfg.ResolveAt(inside)
	if len(resolved) != 1 {
		t.Fatalf("expected 1, got %d", len(resolved))
	}
	if resolved[0].Value != 1000 {
		t.Errorf("during backup window, expected 1000, got %.0f", resolved[0].Value)
	}

	// Outside backup window
	outside := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)
	resolved = cfg.ResolveAt(outside)
	if len(resolved) != 1 {
		t.Fatalf("expected 1, got %d", len(resolved))
	}
	if resolved[0].Value != 70 {
		t.Errorf("outside backup window, expected 70, got %.0f", resolved[0].Value)
	}
}

func TestResolveAt_ScheduledDisable(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SVScheduled("70",
					TimeWindowOverride{Window: "01:00-09:00", Value: "disable"},
				),
			},
		},
	}

	// During window — disabled
	inside := time.Date(2026, 1, 15, 3, 0, 0, 0, time.UTC)
	resolved := cfg.ResolveAt(inside)
	if len(resolved) != 0 {
		t.Errorf("during disable window, expected 0, got %d: %+v", len(resolved), resolved)
	}

	// Outside window — normal
	outside := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)
	resolved = cfg.ResolveAt(outside)
	if len(resolved) != 1 || resolved[0].Value != 70 {
		t.Errorf("outside disable window, expected value=70, got %+v", resolved)
	}
}

func TestResolveAt_ScheduledCritical(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections":          SV("70"),
				"mysql_connections_critical": SVScheduled("120", TimeWindowOverride{Window: "01:00-09:00", Value: "200"}),
			},
		},
	}

	// During window — critical should be 200
	inside := time.Date(2026, 1, 15, 3, 0, 0, 0, time.UTC)
	resolved := cfg.ResolveAt(inside)
	sort.Slice(resolved, func(i, j int) bool { return resolved[i].Severity < resolved[j].Severity })

	if len(resolved) != 2 {
		t.Fatalf("expected 2, got %d: %+v", len(resolved), resolved)
	}
	if resolved[0].Severity != "critical" || resolved[0].Value != 200 {
		t.Errorf("expected critical=200, got %s=%.0f", resolved[0].Severity, resolved[0].Value)
	}

	// Outside window — critical should be 120
	outside := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)
	resolved = cfg.ResolveAt(outside)
	sort.Slice(resolved, func(i, j int) bool { return resolved[i].Severity < resolved[j].Severity })

	if len(resolved) != 2 {
		t.Fatalf("expected 2, got %d", len(resolved))
	}
	if resolved[0].Severity != "critical" || resolved[0].Value != 120 {
		t.Errorf("expected critical=120, got %s=%.0f", resolved[0].Severity, resolved[0].Value)
	}
}

func TestResolveAt_ScheduledWithYAML(t *testing.T) {
	content := `
defaults:
  mysql_connections: 80
tenants:
  db-a:
    mysql_connections:
      default: "70"
      overrides:
        - window: "01:00-09:00"
          value: "1000"
  db-b:
    mysql_connections: "50"
`
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}

	mgr := NewConfigManager(path)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	cfg := mgr.GetConfig()

	// During backup window
	inside := time.Date(2026, 1, 15, 3, 0, 0, 0, time.UTC)
	resolved := cfg.ResolveAt(inside)
	sort.Slice(resolved, func(i, j int) bool { return resolved[i].Tenant < resolved[j].Tenant })

	if len(resolved) != 2 {
		t.Fatalf("expected 2, got %d: %+v", len(resolved), resolved)
	}
	if resolved[0].Tenant != "db-a" || resolved[0].Value != 1000 {
		t.Errorf("db-a during window: expected 1000, got %.0f", resolved[0].Value)
	}
	if resolved[1].Tenant != "db-b" || resolved[1].Value != 50 {
		t.Errorf("db-b: expected 50, got %.0f", resolved[1].Value)
	}

	// Outside window
	outside := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)
	resolved = cfg.ResolveAt(outside)
	sort.Slice(resolved, func(i, j int) bool { return resolved[i].Tenant < resolved[j].Tenant })

	if resolved[0].Tenant != "db-a" || resolved[0].Value != 70 {
		t.Errorf("db-a outside window: expected 70, got %.0f", resolved[0].Value)
	}
}

func TestMatchTimeWindow(t *testing.T) {
	tests := []struct {
		window string
		hour   int
		minute int
		want   bool
	}{
		// Same-day window 01:00-09:00
		{"01:00-09:00", 0, 30, false},  // before
		{"01:00-09:00", 1, 0, true},    // start (inclusive)
		{"01:00-09:00", 5, 30, true},   // middle
		{"01:00-09:00", 8, 59, true},   // just before end
		{"01:00-09:00", 9, 0, false},   // end (exclusive)
		{"01:00-09:00", 12, 0, false},  // after

		// Cross-midnight window 22:00-06:00
		{"22:00-06:00", 21, 59, false}, // before
		{"22:00-06:00", 22, 0, true},   // start (inclusive)
		{"22:00-06:00", 23, 30, true},  // late night
		{"22:00-06:00", 0, 0, true},    // midnight
		{"22:00-06:00", 3, 0, true},    // early morning
		{"22:00-06:00", 5, 59, true},   // just before end
		{"22:00-06:00", 6, 0, false},   // end (exclusive)
		{"22:00-06:00", 12, 0, false},  // midday

		// Edge: full day (should never match if start==end)
		{"00:00-00:00", 12, 0, false},

		// Edge: minute precision
		{"08:30-09:15", 8, 29, false},
		{"08:30-09:15", 8, 30, true},
		{"08:30-09:15", 9, 14, true},
		{"08:30-09:15", 9, 15, false},
	}

	for _, tt := range tests {
		now := time.Date(2026, 1, 15, tt.hour, tt.minute, 0, 0, time.UTC)
		got := matchTimeWindow(tt.window, now)
		if got != tt.want {
			t.Errorf("matchTimeWindow(%q, %02d:%02d) = %v, want %v", tt.window, tt.hour, tt.minute, got, tt.want)
		}
	}
}

func TestMatchTimeWindow_NonUTCInput(t *testing.T) {
	// Input in JST (+9), window is UTC. 03:00 JST = 18:00 UTC
	jst := time.FixedZone("JST", 9*3600)
	now := time.Date(2026, 1, 15, 3, 0, 0, 0, jst) // 18:00 UTC

	// Window 01:00-09:00 UTC — 18:00 UTC should NOT match
	if matchTimeWindow("01:00-09:00", now) {
		t.Error("expected false: 18:00 UTC is outside 01:00-09:00 UTC")
	}

	// Window 17:00-20:00 UTC — 18:00 UTC should match
	if !matchTimeWindow("17:00-20:00", now) {
		t.Error("expected true: 18:00 UTC is inside 17:00-20:00 UTC")
	}
}

func TestParseHHMM(t *testing.T) {
	tests := []struct {
		input  string
		wantH  int
		wantM  int
		wantOK bool
	}{
		{"00:00", 0, 0, true},
		{"23:59", 23, 59, true},
		{"09:30", 9, 30, true},
		{"  01:00  ", 1, 0, true}, // whitespace trimmed
		{"24:00", 0, 0, false},   // invalid hour
		{"12:60", 0, 0, false},   // invalid minute
		{"abc", 0, 0, false},     // garbage
		{"12", 0, 0, false},      // no colon
	}

	for _, tt := range tests {
		h, m, err := parseHHMM(tt.input)
		if tt.wantOK {
			if err != nil {
				t.Errorf("parseHHMM(%q): unexpected error %v", tt.input, err)
			} else if h != tt.wantH || m != tt.wantM {
				t.Errorf("parseHHMM(%q) = (%d, %d), want (%d, %d)", tt.input, h, m, tt.wantH, tt.wantM)
			}
		} else {
			if err == nil {
				t.Errorf("parseHHMM(%q): expected error, got (%d, %d)", tt.input, h, m)
			}
		}
	}
}

// endregion
// region ScheduledValueStringAndMerge — backward compatibility and directory loading with scheduled values

// ============================================================
// Backward Compatibility: ScheduledValue.String()
// ============================================================

func TestScheduledValue_String(t *testing.T) {
	sv := SV("70")
	if sv.String() != "70" {
		t.Errorf("expected 70, got %q", sv.String())
	}

	sv2 := SVScheduled("80", TimeWindowOverride{Window: "01:00-09:00", Value: "1000"})
	if sv2.String() != "80" {
		t.Errorf("expected 80 (default), got %q", sv2.String())
	}
}

// ============================================================
// B4: Directory Mode + ScheduledValue Merge
// ============================================================

func TestConfigManager_LoadDir_ScheduledValueMerge(t *testing.T) {
	dir := t.TempDir()

	// _defaults.yaml: platform-managed defaults
	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
  mysql_cpu: 80
`)

	// db-a.yaml: tenant with mixed scalar + structured ScheduledValue
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    mysql_connections:
      default: "70"
      overrides:
        - window: "01:00-09:00"
          value: "1000"
    mysql_cpu: "90"
`)

	// db-b.yaml: tenant with regex dimensional (scalar only, using double quotes for consistency)
	writeTestFile(t, dir, "db-b.yaml", `
tenants:
  db-b:
    "oracle_tablespace{tablespace=~\"SYS.*\"}": "95"
`)

	manager := NewConfigManager(dir)
	if err := manager.Load(); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	cfg := manager.GetConfig()
	if cfg == nil {
		t.Fatal("expected config to be loaded")
	}

	// Verify defaults loaded
	if cfg.Defaults["mysql_connections"] != 80 {
		t.Errorf("expected default mysql_connections=80, got %v", cfg.Defaults["mysql_connections"])
	}

	// Verify db-a structured ScheduledValue
	dbA, ok := cfg.Tenants["db-a"]
	if !ok {
		t.Fatal("expected tenant db-a")
	}
	sv := dbA["mysql_connections"]
	if sv.Default != "70" {
		t.Errorf("expected db-a mysql_connections default=70, got %q", sv.Default)
	}
	if len(sv.Overrides) != 1 {
		t.Errorf("expected 1 override, got %d", len(sv.Overrides))
	}
	if sv.Overrides[0].Window != "01:00-09:00" || sv.Overrides[0].Value != "1000" {
		t.Errorf("unexpected override: %+v", sv.Overrides[0])
	}

	// Verify db-a scalar ScheduledValue
	if dbA["mysql_cpu"].Default != "90" {
		t.Errorf("expected db-a mysql_cpu=90, got %q", dbA["mysql_cpu"].Default)
	}

	// Verify db-b regex dimensional
	dbB, ok := cfg.Tenants["db-b"]
	if !ok {
		t.Fatal("expected tenant db-b")
	}
	key := `oracle_tablespace{tablespace=~"SYS.*"}`
	if dbB[key].Default != "95" {
		t.Errorf("expected db-b %s=95, got %q", key, dbB[key].Default)
	}

	// Verify mode is directory
	if manager.Mode() != "directory" {
		t.Errorf("expected directory mode, got %q", manager.Mode())
	}
}

// endregion
