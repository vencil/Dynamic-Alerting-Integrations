package main

// Profile + Routing tests — _routing_profiles resolution, receiver type
// validation, matcher dedup, RoutingMap YAML round-trip, ProfileRef
// fallback chain (tenant override → profile → defaults). Split out of
// config_test.go in PR-2; shared helpers live in config_test.go.

import (
	"testing"
	"time"
)

// region ProfilesAndRouting — profile resolution, routing configuration, and profile merging

func TestValidateTenantKeys_MetadataReservedKey(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SV("70"),
				"_metadata":         SV("owner: team-dba\n"),
			},
		},
	}
	warnings := cfg.ValidateTenantKeys()
	if len(warnings) != 0 {
		t.Errorf("_metadata should be valid reserved key, got warnings: %v", warnings)
	}
}

// ============================================================
// Profile Tests (v1.12.0)
// ============================================================

func TestResolve_ProfileBasic(t *testing.T) {
	// Profile provides value, tenant does NOT override → use profile value
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Profiles: map[string]map[string]ScheduledValue{
			"standard-mariadb": {"mysql_connections": SV("85")},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_profile": SV("standard-mariadb")},
		},
	}
	cfg.applyProfiles()
	result := cfg.Resolve()
	if len(result) != 1 {
		t.Fatalf("expected 1 resolved threshold, got %d", len(result))
	}
	if result[0].Value != 85 {
		t.Errorf("expected profile value 85, got %v", result[0].Value)
	}
}

func TestResolve_ProfileOverriddenByTenant(t *testing.T) {
	// Tenant overrides profile value → tenant wins
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Profiles: map[string]map[string]ScheduledValue{
			"standard-mariadb": {"mysql_connections": SV("85")},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_profile":          SV("standard-mariadb"),
				"mysql_connections": SV("95"),
			},
		},
	}
	cfg.applyProfiles()
	result := cfg.Resolve()
	if len(result) != 1 {
		t.Fatalf("expected 1 resolved threshold, got %d", len(result))
	}
	if result[0].Value != 95 {
		t.Errorf("expected tenant override value 95, got %v", result[0].Value)
	}
}

func TestResolve_ProfileFallbackToDefaults(t *testing.T) {
	// Profile does NOT define a metric → fall back to defaults
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80, "mysql_cpu": 70},
		Profiles: map[string]map[string]ScheduledValue{
			"standard-mariadb": {"mysql_connections": SV("85")},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_profile": SV("standard-mariadb")},
		},
	}
	cfg.applyProfiles()
	result := cfg.Resolve()
	if len(result) != 2 {
		t.Fatalf("expected 2 resolved thresholds, got %d", len(result))
	}
	// Find each metric
	for _, r := range result {
		switch r.Metric {
		case "connections":
			if r.Value != 85 {
				t.Errorf("connections: expected profile value 85, got %v", r.Value)
			}
		case "cpu":
			if r.Value != 70 {
				t.Errorf("cpu: expected default value 70, got %v", r.Value)
			}
		}
	}
}

func TestResolve_ProfileDisable(t *testing.T) {
	// Tenant sets "disable" on a profile-defined metric → no metric exposed
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Profiles: map[string]map[string]ScheduledValue{
			"standard-mariadb": {"mysql_connections": SV("85")},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_profile":          SV("standard-mariadb"),
				"mysql_connections": SV("disable"),
			},
		},
	}
	cfg.applyProfiles()
	result := cfg.Resolve()
	if len(result) != 0 {
		t.Errorf("expected 0 resolved thresholds (disabled), got %d", len(result))
	}
}

func TestResolve_ProfileNotFound(t *testing.T) {
	// _profile references unknown profile → WARN + ignore, fall back to defaults
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Profiles: map[string]map[string]ScheduledValue{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_profile": SV("nonexistent")},
		},
	}
	cfg.applyProfiles()
	result := cfg.Resolve()
	if len(result) != 1 {
		t.Fatalf("expected 1 resolved threshold (default), got %d", len(result))
	}
	if result[0].Value != 80 {
		t.Errorf("expected default value 80, got %v", result[0].Value)
	}
}

func TestResolve_ProfileWithSilentMode(t *testing.T) {
	// Profile includes _silent_mode → tenant inherits
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Profiles: map[string]map[string]ScheduledValue{
			"standard-mariadb": {
				"mysql_connections": SV("85"),
				"_silent_mode":     SV("warning"),
			},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_profile": SV("standard-mariadb")},
		},
	}
	cfg.applyProfiles()
	silents := cfg.ResolveSilentModes()
	if len(silents) != 1 {
		t.Fatalf("expected 1 silent mode from profile, got %d", len(silents))
	}
	if silents[0].TargetSeverity != "warning" {
		t.Errorf("expected warning severity from profile, got %s", silents[0].TargetSeverity)
	}
}

func TestResolve_ProfileWithRouting(t *testing.T) {
	// Profile includes _routing → tenant inherits routing config
	routingYAML := "receiver:\n  type: \"webhook\"\n  url: \"https://noc.example.com/alerts\"\ngroup_wait: \"30s\""
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Profiles: map[string]map[string]ScheduledValue{
			"standard-mariadb": {
				"_routing": SV(routingYAML),
			},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_profile": SV("standard-mariadb")},
		},
	}
	cfg.applyProfiles()
	routes := cfg.ResolveRouting()
	if len(routes) != 1 {
		t.Fatalf("expected 1 routing config from profile, got %d", len(routes))
	}
	if routes[0].ReceiverType != "webhook" {
		t.Errorf("expected webhook receiver from profile, got %s", routes[0].ReceiverType)
	}
}

func TestResolve_ProfileWithMetadata(t *testing.T) {
	// Profile includes _metadata → tenant inherits metadata
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Profiles: map[string]map[string]ScheduledValue{
			"standard-mariadb": {
				"_metadata": SV("runbook_url: https://wiki.example.com/mariadb\nowner: team-dba\ntier: gold\n"),
			},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_profile": SV("standard-mariadb")},
		},
	}
	cfg.applyProfiles()
	metadata := cfg.ResolveMetadata()
	found := false
	for _, m := range metadata {
		if m.Tenant == "db-a" {
			found = true
			if m.RunbookURL != "https://wiki.example.com/mariadb" {
				t.Errorf("expected runbook from profile, got %s", m.RunbookURL)
			}
			if m.Owner != "team-dba" {
				t.Errorf("expected owner from profile, got %s", m.Owner)
			}
		}
	}
	if !found {
		t.Error("expected metadata for db-a from profile, not found")
	}
}

func TestResolve_ProfileWithScheduledValue(t *testing.T) {
	// Profile value is a ScheduledValue → time windows resolve correctly
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Profiles: map[string]map[string]ScheduledValue{
			"standard-mariadb": {
				"mysql_connections": SVScheduled("85",
					TimeWindowOverride{Window: "01:00-05:00", Value: "200"},
				),
			},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_profile": SV("standard-mariadb")},
		},
	}
	cfg.applyProfiles()

	// During window (03:00 UTC)
	inWindow := time.Date(2025, 6, 15, 3, 0, 0, 0, time.UTC)
	result := cfg.ResolveAt(inWindow)
	if len(result) != 1 || result[0].Value != 200 {
		t.Errorf("during window: expected 200, got %v", result)
	}

	// Outside window (12:00 UTC)
	outWindow := time.Date(2025, 6, 15, 12, 0, 0, 0, time.UTC)
	result = cfg.ResolveAt(outWindow)
	if len(result) != 1 || result[0].Value != 85 {
		t.Errorf("outside window: expected 85, got %v", result)
	}
}

func TestResolve_ProfileWithCritical(t *testing.T) {
	// Profile defines <metric>_critical → tenant inherits multi-tier severity
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Profiles: map[string]map[string]ScheduledValue{
			"standard-mariadb": {
				"mysql_connections":          SV("85"),
				"mysql_connections_critical": SV("120"),
			},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_profile": SV("standard-mariadb")},
		},
	}
	cfg.applyProfiles()
	result := cfg.Resolve()
	if len(result) != 2 {
		t.Fatalf("expected 2 thresholds (warning+critical), got %d", len(result))
	}
	var hasWarning, hasCritical bool
	for _, r := range result {
		if r.Severity == "warning" && r.Value == 85 {
			hasWarning = true
		}
		if r.Severity == "critical" && r.Value == 120 {
			hasCritical = true
		}
	}
	if !hasWarning || !hasCritical {
		t.Errorf("expected warning=85 + critical=120, got %v", result)
	}
}

func TestLoadDir_ProfilesBoundary(t *testing.T) {
	// _profiles.yaml loads correctly; tenant file with profiles → WARN + ignore
	dir := t.TempDir()
	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
`)
	writeTestFile(t, dir, "_profiles.yaml", `
profiles:
  standard-mariadb:
    mysql_connections: "85"
`)
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    _profile: standard-mariadb
profiles:
  should-be-ignored:
    mysql_connections: "999"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load() failed: %v", err)
	}
	cfg := mgr.GetConfig()

	// _profiles.yaml profile should be loaded
	if _, ok := cfg.Profiles["standard-mariadb"]; !ok {
		t.Error("expected standard-mariadb profile to be loaded from _profiles.yaml")
	}
	// Tenant file's profiles section should be ignored
	if _, ok := cfg.Profiles["should-be-ignored"]; ok {
		t.Error("profiles in tenant file should be ignored")
	}

	result := cfg.Resolve()
	if len(result) != 1 || result[0].Value != 85 {
		t.Errorf("expected profile value 85, got %v", result)
	}
}

func TestLoadDir_ProfilesMergeWithDefaults(t *testing.T) {
	// Profile + defaults + tenant override coexist correctly
	dir := t.TempDir()
	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
  mysql_cpu: 70
`)
	writeTestFile(t, dir, "_profiles.yaml", `
profiles:
  standard-mariadb:
    mysql_connections: "85"
    mysql_cpu: "75"
`)
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    _profile: standard-mariadb
    mysql_connections: "95"
`)
	writeTestFile(t, dir, "db-b.yaml", `
tenants:
  db-b:
    _profile: standard-mariadb
`)

	mgr := NewConfigManager(dir)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load() failed: %v", err)
	}
	cfg := mgr.GetConfig()
	result := cfg.Resolve()

	// db-a: connections=95 (tenant override), cpu=75 (profile)
	// db-b: connections=85 (profile), cpu=75 (profile)
	if len(result) != 4 {
		t.Fatalf("expected 4 resolved thresholds, got %d", len(result))
	}

	for _, r := range result {
		switch {
		case r.Tenant == "db-a" && r.Metric == "connections":
			if r.Value != 95 {
				t.Errorf("db-a connections: expected 95 (tenant override), got %v", r.Value)
			}
		case r.Tenant == "db-a" && r.Metric == "cpu":
			if r.Value != 75 {
				t.Errorf("db-a cpu: expected 75 (profile), got %v", r.Value)
			}
		case r.Tenant == "db-b" && r.Metric == "connections":
			if r.Value != 85 {
				t.Errorf("db-b connections: expected 85 (profile), got %v", r.Value)
			}
		case r.Tenant == "db-b" && r.Metric == "cpu":
			if r.Value != 75 {
				t.Errorf("db-b cpu: expected 75 (profile), got %v", r.Value)
			}
		}
	}
}

func TestValidateTenantKeys_ProfileRef(t *testing.T) {
	// _profile referencing existing profile → no warning
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Profiles: map[string]map[string]ScheduledValue{
			"standard-mariadb": {"mysql_connections": SV("85")},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_profile": SV("standard-mariadb"), "mysql_connections": SV("90")},
		},
	}
	warnings := cfg.ValidateTenantKeys()
	if len(warnings) != 0 {
		t.Errorf("expected no warnings for valid profile ref, got: %v", warnings)
	}

	// _profile referencing unknown profile → warning
	cfg2 := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Profiles: map[string]map[string]ScheduledValue{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_profile": SV("nonexistent"), "mysql_connections": SV("90")},
		},
	}
	warnings2 := cfg2.ValidateTenantKeys()
	if len(warnings2) != 1 {
		t.Errorf("expected 1 warning for unknown profile ref, got %d: %v", len(warnings2), warnings2)
	}
}

// endregion
