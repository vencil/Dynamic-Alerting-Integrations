package main

// Resolve / ResolveAt / ResolveStateFilters tests + ParseMetricKey.
// Split out of config_test.go in PR-2 (4008-line monolith decomposition).
// Shared helpers (SV / SVScheduled / writeTestFile) live in config_test.go.

import (
	"sort"
	"testing"
)

// region ThresholdResolution — basic Resolve/ResolveAt tests

func TestResolve_ThreeState(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"mysql_connections": 80,
			"mysql_cpu":         80,
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SV("70"),
				// mysql_cpu omitted → default 80
			},
			"db-b": {
				"mysql_connections": SV("disable"),
				"mysql_cpu":         SV("40"),
			},
		},
	}

	resolved := cfg.Resolve()
	sort.Slice(resolved, func(i, j int) bool {
		if resolved[i].Tenant != resolved[j].Tenant {
			return resolved[i].Tenant < resolved[j].Tenant
		}
		return resolved[i].Metric < resolved[j].Metric
	})

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
		Tenants: map[string]map[string]ScheduledValue{
			"t1": {"mysql_connections": SV("disable")},
			"t2": {"mysql_connections": SV("disabled")},
			"t3": {"mysql_connections": SV("off")},
			"t4": {"mysql_connections": SV("false")},
			"t5": {"mysql_connections": SV("DISABLE")},
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
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"mysql_connections": SV("50:critical")},
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
		Tenants:  map[string]map[string]ScheduledValue{},
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
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {},
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

// endregion

// region MetricKeyParsing — test helpers and key parsing

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

// endregion
// region StateFiltersResolution — state filter tests and validation

// --- Scenario C: State Filter Tests ---

func TestResolveStateFilters_AllEnabled(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		StateFilters: map[string]StateFilter{
			"container_crashloop": {Reasons: []string{"CrashLoopBackOff"}, Severity: "critical"},
			"container_imagepull": {Reasons: []string{"ImagePullBackOff", "InvalidImageName"}, Severity: "warning"},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"mysql_connections": SV("70")},
			"db-b": {"mysql_connections": SV("100")},
		},
	}

	resolved := cfg.ResolveStateFilters()
	sort.Slice(resolved, func(i, j int) bool {
		if resolved[i].Tenant != resolved[j].Tenant {
			return resolved[i].Tenant < resolved[j].Tenant
		}
		return resolved[i].FilterName < resolved[j].FilterName
	})

	if len(resolved) != 4 {
		t.Fatalf("expected 4, got %d: %+v", len(resolved), resolved)
	}

	expected := []struct{ tenant, filter, severity string }{
		{"db-a", "container_crashloop", "critical"},
		{"db-a", "container_imagepull", "warning"},
		{"db-b", "container_crashloop", "critical"},
		{"db-b", "container_imagepull", "warning"},
	}
	for i, exp := range expected {
		r := resolved[i]
		if r.Tenant != exp.tenant || r.FilterName != exp.filter || r.Severity != exp.severity {
			t.Errorf("index %d: expected {%s %s %s}, got {%s %s %s}",
				i, exp.tenant, exp.filter, exp.severity, r.Tenant, r.FilterName, r.Severity)
		}
	}
}

func TestResolveStateFilters_PerTenantDisable(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults:     map[string]float64{"mysql_connections": 80},
		StateFilters: map[string]StateFilter{"container_crashloop": {Reasons: []string{"CrashLoopBackOff"}, Severity: "critical"}},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"mysql_connections": SV("70")},
			"db-b": {"mysql_connections": SV("100"), "_state_container_crashloop": SV("disable")},
		},
	}

	resolved := cfg.ResolveStateFilters()
	if len(resolved) != 1 {
		t.Fatalf("expected 1, got %d: %+v", len(resolved), resolved)
	}
	if resolved[0].Tenant != "db-a" || resolved[0].FilterName != "container_crashloop" {
		t.Errorf("unexpected: %+v", resolved[0])
	}
}

func TestResolveStateFilters_DisableVariants(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults:     map[string]float64{},
		StateFilters: map[string]StateFilter{"container_crashloop": {Reasons: []string{"CrashLoopBackOff"}, Severity: "critical"}},
		Tenants: map[string]map[string]ScheduledValue{
			"t1": {"_state_container_crashloop": SV("disable")},
			"t2": {"_state_container_crashloop": SV("disabled")},
			"t3": {"_state_container_crashloop": SV("off")},
			"t4": {"_state_container_crashloop": SV("false")},
			"t5": {"_state_container_crashloop": SV("DISABLE")},
		},
	}

	resolved := cfg.ResolveStateFilters()
	if len(resolved) != 0 {
		t.Errorf("expected 0, got %d: %+v", len(resolved), resolved)
	}
}

func TestResolveStateFilters_NoFilters(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants:  map[string]map[string]ScheduledValue{"db-a": {"mysql_connections": SV("70")}},
	}

	if len(cfg.ResolveStateFilters()) != 0 {
		t.Error("expected 0 state filters")
	}
	if len(cfg.Resolve()) != 1 {
		t.Error("expected 1 threshold")
	}
}

func TestResolveStateFilters_DefaultSeverity(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults:     map[string]float64{},
		StateFilters: map[string]StateFilter{"container_crashloop": {Reasons: []string{"CrashLoopBackOff"}}},
		Tenants:      map[string]map[string]ScheduledValue{"db-a": {}},
	}

	resolved := cfg.ResolveStateFilters()
	if len(resolved) != 1 || resolved[0].Severity != "warning" {
		t.Errorf("expected severity=warning, got %+v", resolved)
	}
}

func TestResolve_IgnoresStateKeys(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults:     map[string]float64{"mysql_connections": 80},
		StateFilters: map[string]StateFilter{"container_crashloop": {Reasons: []string{"CrashLoopBackOff"}, Severity: "critical"}},
		Tenants:      map[string]map[string]ScheduledValue{"db-a": {"mysql_connections": SV("70"), "_state_container_crashloop": SV("disable")}},
	}

	if thresholds := cfg.Resolve(); len(thresholds) != 1 || thresholds[0].Value != 70 {
		t.Errorf("unexpected thresholds: %+v", thresholds)
	}
	if sf := cfg.ResolveStateFilters(); len(sf) != 0 {
		t.Errorf("expected 0 state filters, got %d", len(sf))
	}
}

// endregion
