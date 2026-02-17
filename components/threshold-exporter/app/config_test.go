package main

import (
	"os"
	"path/filepath"
	"sort"
	"testing"
)

func TestResolve_ThreeState(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"mysql_connections": 80,
			"mysql_cpu":         80,
		},
		Tenants: map[string]map[string]string{
			"db-a": {
				"mysql_connections": "70",
				// mysql_cpu omitted → default 80
			},
			"db-b": {
				"mysql_connections": "disable",
				"mysql_cpu":         "40",
			},
		},
	}

	resolved := cfg.Resolve()
	// Sort for deterministic comparison
	sort.Slice(resolved, func(i, j int) bool {
		if resolved[i].Tenant != resolved[j].Tenant {
			return resolved[i].Tenant < resolved[j].Tenant
		}
		return resolved[i].Metric < resolved[j].Metric
	})

	// Expected:
	// db-a: connections=70 (custom), cpu=80 (default)
	// db-b: connections=SKIP (disabled), cpu=40 (custom)
	expected := []struct {
		tenant, metric, component string
		value                     float64
	}{
		{"db-a", "connections", "mysql", 70},
		{"db-a", "cpu", "mysql", 80},
		{"db-b", "cpu", "mysql", 40},
	}

	if len(resolved) != len(expected) {
		t.Fatalf("expected %d resolved thresholds, got %d: %+v", len(expected), len(resolved), resolved)
	}

	for i, exp := range expected {
		r := resolved[i]
		if r.Tenant != exp.tenant || r.Metric != exp.metric || r.Component != exp.component || r.Value != exp.value {
			t.Errorf("index %d: expected {%s %s %s %.0f}, got {%s %s %s %.0f}",
				i, exp.tenant, exp.metric, exp.component, exp.value,
				r.Tenant, r.Metric, r.Component, r.Value)
		}
	}
}

func TestResolve_DisableVariants(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]string{
			"t1": {"mysql_connections": "disable"},
			"t2": {"mysql_connections": "disabled"},
			"t3": {"mysql_connections": "off"},
			"t4": {"mysql_connections": "false"},
			"t5": {"mysql_connections": "DISABLE"},
		},
	}

	resolved := cfg.Resolve()
	if len(resolved) != 0 {
		t.Errorf("expected 0 resolved thresholds for disabled variants, got %d: %+v", len(resolved), resolved)
	}
}

func TestResolve_CustomSeverity(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]string{
			"db-a": {"mysql_connections": "50:critical"},
		},
	}

	resolved := cfg.Resolve()
	if len(resolved) != 1 {
		t.Fatalf("expected 1, got %d", len(resolved))
	}
	if resolved[0].Value != 50 || resolved[0].Severity != "critical" {
		t.Errorf("expected value=50 severity=critical, got value=%.0f severity=%s", resolved[0].Value, resolved[0].Severity)
	}
}

func TestResolve_EmptyTenants(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants:  map[string]map[string]string{},
	}

	resolved := cfg.Resolve()
	if len(resolved) != 0 {
		t.Errorf("expected 0 (no tenants), got %d", len(resolved))
	}
}

func TestResolve_TenantWithNoOverrides(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"mysql_connections": 80,
			"mysql_cpu":         90,
		},
		Tenants: map[string]map[string]string{
			"db-a": {}, // no overrides → all defaults
		},
	}

	resolved := cfg.Resolve()
	if len(resolved) != 2 {
		t.Fatalf("expected 2, got %d", len(resolved))
	}

	for _, r := range resolved {
		if r.Tenant != "db-a" {
			t.Errorf("unexpected tenant: %s", r.Tenant)
		}
	}
}

func TestParseMetricKey(t *testing.T) {
	tests := []struct {
		input              string
		wantComp, wantMet string
	}{
		{"mysql_connections", "mysql", "connections"},
		{"mysql_cpu", "mysql", "cpu"},
		{"container_cpu_percent", "container", "cpu_percent"},
		{"standalone", "default", "standalone"},
	}

	for _, tt := range tests {
		comp, met := parseMetricKey(tt.input)
		if comp != tt.wantComp || met != tt.wantMet {
			t.Errorf("parseMetricKey(%q) = (%q, %q), want (%q, %q)",
				tt.input, comp, met, tt.wantComp, tt.wantMet)
		}
	}
}

func TestConfigManager_LoadFile(t *testing.T) {
	content := `
defaults:
  mysql_connections: 80
  mysql_cpu: 80
tenants:
  db-a:
    mysql_connections: "70"
  db-b:
    mysql_connections: "disable"
    mysql_cpu: "40"
`
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	mgr := NewConfigManager(path)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	if !mgr.IsLoaded() {
		t.Error("expected IsLoaded() = true")
	}

	cfg := mgr.GetConfig()
	if len(cfg.Defaults) != 2 {
		t.Errorf("expected 2 defaults, got %d", len(cfg.Defaults))
	}
	if len(cfg.Tenants) != 2 {
		t.Errorf("expected 2 tenants, got %d", len(cfg.Tenants))
	}
}

// --- Scenario C: State Filter Tests ---

func TestResolveStateFilters_AllEnabled(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		StateFilters: map[string]StateFilter{
			"container_crashloop": {
				Reasons:  []string{"CrashLoopBackOff"},
				Severity: "critical",
			},
			"container_imagepull": {
				Reasons:  []string{"ImagePullBackOff", "InvalidImageName"},
				Severity: "warning",
			},
		},
		Tenants: map[string]map[string]string{
			"db-a": {"mysql_connections": "70"},
			"db-b": {"mysql_connections": "100"},
		},
	}

	resolved := cfg.ResolveStateFilters()
	sort.Slice(resolved, func(i, j int) bool {
		if resolved[i].Tenant != resolved[j].Tenant {
			return resolved[i].Tenant < resolved[j].Tenant
		}
		return resolved[i].FilterName < resolved[j].FilterName
	})

	// 2 filters × 2 tenants = 4 resolved state filters
	if len(resolved) != 4 {
		t.Fatalf("expected 4 resolved state filters, got %d: %+v", len(resolved), resolved)
	}

	// Verify all tenants get all filters
	expected := []struct {
		tenant, filter, severity string
	}{
		{"db-a", "container_crashloop", "critical"},
		{"db-a", "container_imagepull", "warning"},
		{"db-b", "container_crashloop", "critical"},
		{"db-b", "container_imagepull", "warning"},
	}

	for i, exp := range expected {
		r := resolved[i]
		if r.Tenant != exp.tenant || r.FilterName != exp.filter || r.Severity != exp.severity {
			t.Errorf("index %d: expected {%s %s %s}, got {%s %s %s}",
				i, exp.tenant, exp.filter, exp.severity,
				r.Tenant, r.FilterName, r.Severity)
		}
	}
}

func TestResolveStateFilters_PerTenantDisable(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		StateFilters: map[string]StateFilter{
			"container_crashloop": {
				Reasons:  []string{"CrashLoopBackOff"},
				Severity: "critical",
			},
		},
		Tenants: map[string]map[string]string{
			"db-a": {"mysql_connections": "70"},
			"db-b": {
				"mysql_connections":          "100",
				"_state_container_crashloop": "disable", // disable for db-b
			},
		},
	}

	resolved := cfg.ResolveStateFilters()

	// Only db-a should have the filter (db-b disabled)
	if len(resolved) != 1 {
		t.Fatalf("expected 1 resolved state filter, got %d: %+v", len(resolved), resolved)
	}

	if resolved[0].Tenant != "db-a" {
		t.Errorf("expected tenant db-a, got %s", resolved[0].Tenant)
	}
	if resolved[0].FilterName != "container_crashloop" {
		t.Errorf("expected filter container_crashloop, got %s", resolved[0].FilterName)
	}
	if resolved[0].Severity != "critical" {
		t.Errorf("expected severity critical, got %s", resolved[0].Severity)
	}
}

func TestResolveStateFilters_DisableVariants(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		StateFilters: map[string]StateFilter{
			"container_crashloop": {
				Reasons:  []string{"CrashLoopBackOff"},
				Severity: "critical",
			},
		},
		Tenants: map[string]map[string]string{
			"t1": {"_state_container_crashloop": "disable"},
			"t2": {"_state_container_crashloop": "disabled"},
			"t3": {"_state_container_crashloop": "off"},
			"t4": {"_state_container_crashloop": "false"},
			"t5": {"_state_container_crashloop": "DISABLE"},
		},
	}

	resolved := cfg.ResolveStateFilters()
	if len(resolved) != 0 {
		t.Errorf("expected 0 resolved state filters for disabled variants, got %d: %+v", len(resolved), resolved)
	}
}

func TestResolveStateFilters_NoFilters(t *testing.T) {
	// Backward compatibility: no state_filters section = no state filter metrics
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]string{
			"db-a": {"mysql_connections": "70"},
		},
	}

	resolved := cfg.ResolveStateFilters()
	if len(resolved) != 0 {
		t.Errorf("expected 0 state filters for config without state_filters, got %d", len(resolved))
	}

	// Verify Resolve() still works normally
	thresholds := cfg.Resolve()
	if len(thresholds) != 1 {
		t.Errorf("expected 1 threshold, got %d", len(thresholds))
	}
}

func TestResolveStateFilters_DefaultSeverity(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		StateFilters: map[string]StateFilter{
			"container_crashloop": {
				Reasons: []string{"CrashLoopBackOff"},
				// Severity omitted → should default to "warning"
			},
		},
		Tenants: map[string]map[string]string{
			"db-a": {},
		},
	}

	resolved := cfg.ResolveStateFilters()
	if len(resolved) != 1 {
		t.Fatalf("expected 1, got %d", len(resolved))
	}
	if resolved[0].Severity != "warning" {
		t.Errorf("expected default severity 'warning', got %q", resolved[0].Severity)
	}
}

func TestResolve_IgnoresStateKeys(t *testing.T) {
	// Verify that _state_ prefixed keys in tenant overrides don't affect numeric resolution
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		StateFilters: map[string]StateFilter{
			"container_crashloop": {
				Reasons:  []string{"CrashLoopBackOff"},
				Severity: "critical",
			},
		},
		Tenants: map[string]map[string]string{
			"db-a": {
				"mysql_connections":          "70",
				"_state_container_crashloop": "disable",
			},
		},
	}

	// Numeric thresholds should work normally
	thresholds := cfg.Resolve()
	if len(thresholds) != 1 {
		t.Fatalf("expected 1 threshold, got %d: %+v", len(thresholds), thresholds)
	}
	if thresholds[0].Value != 70 {
		t.Errorf("expected value 70, got %.0f", thresholds[0].Value)
	}

	// State filters should reflect the disable
	stateFilters := cfg.ResolveStateFilters()
	if len(stateFilters) != 0 {
		t.Errorf("expected 0 state filters (disabled), got %d", len(stateFilters))
	}
}

func TestConfigManager_LoadFileWithStateFilters(t *testing.T) {
	content := `
defaults:
  mysql_connections: 80
state_filters:
  container_crashloop:
    reasons: ["CrashLoopBackOff"]
    severity: "critical"
  container_imagepull:
    reasons: ["ImagePullBackOff", "InvalidImageName"]
    severity: "warning"
tenants:
  db-a:
    mysql_connections: "70"
  db-b:
    mysql_connections: "100"
    _state_container_crashloop: "disable"
`
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	mgr := NewConfigManager(path)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	cfg := mgr.GetConfig()

	// Verify state filters parsed
	if len(cfg.StateFilters) != 2 {
		t.Errorf("expected 2 state filters, got %d", len(cfg.StateFilters))
	}

	// Verify reasons
	crash := cfg.StateFilters["container_crashloop"]
	if len(crash.Reasons) != 1 || crash.Reasons[0] != "CrashLoopBackOff" {
		t.Errorf("unexpected crashloop reasons: %v", crash.Reasons)
	}

	// Verify resolution
	resolved := cfg.ResolveStateFilters()
	sort.Slice(resolved, func(i, j int) bool {
		if resolved[i].Tenant != resolved[j].Tenant {
			return resolved[i].Tenant < resolved[j].Tenant
		}
		return resolved[i].FilterName < resolved[j].FilterName
	})

	// db-a: both filters enabled (2)
	// db-b: crashloop disabled, imagepull enabled (1)
	// Total: 3
	if len(resolved) != 3 {
		t.Fatalf("expected 3 resolved state filters, got %d: %+v", len(resolved), resolved)
	}

	// Verify db-b only has imagepull
	dbBFilters := []ResolvedStateFilter{}
	for _, r := range resolved {
		if r.Tenant == "db-b" {
			dbBFilters = append(dbBFilters, r)
		}
	}
	if len(dbBFilters) != 1 {
		t.Fatalf("expected 1 filter for db-b, got %d", len(dbBFilters))
	}
	if dbBFilters[0].FilterName != "container_imagepull" {
		t.Errorf("expected db-b to have container_imagepull, got %s", dbBFilters[0].FilterName)
	}
}
