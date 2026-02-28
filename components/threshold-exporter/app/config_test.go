package main

import (
	"os"
	"path/filepath"
	"sort"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

// SV is a test helper to create a scalar ScheduledValue.
func SV(s string) ScheduledValue {
	return ScheduledValue{Default: s}
}

// SVScheduled is a test helper to create a ScheduledValue with time-window overrides.
func SVScheduled(def string, overrides ...TimeWindowOverride) ScheduledValue {
	return ScheduledValue{Default: def, Overrides: overrides}
}

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
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}

	mgr := NewConfigManager(path)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	if !mgr.IsLoaded() {
		t.Error("expected IsLoaded() = true")
	}
	if mgr.Mode() != "single-file" {
		t.Errorf("expected single-file mode, got %s", mgr.Mode())
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
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}

	mgr := NewConfigManager(path)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	cfg := mgr.GetConfig()
	if len(cfg.StateFilters) != 2 {
		t.Errorf("expected 2 state filters, got %d", len(cfg.StateFilters))
	}

	resolved := cfg.ResolveStateFilters()
	sort.Slice(resolved, func(i, j int) bool {
		if resolved[i].Tenant != resolved[j].Tenant {
			return resolved[i].Tenant < resolved[j].Tenant
		}
		return resolved[i].FilterName < resolved[j].FilterName
	})

	// db-a: 2, db-b: 1 (crashloop disabled) = 3
	if len(resolved) != 3 {
		t.Fatalf("expected 3, got %d: %+v", len(resolved), resolved)
	}
}

// ============================================================
// Directory Mode Tests (Phase 2C)
// ============================================================

func TestConfigManager_LoadDir_BasicMerge(t *testing.T) {
	dir := t.TempDir()

	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
  mysql_cpu: 80
  container_cpu: 80
state_filters:
  container_crashloop:
    reasons: ["CrashLoopBackOff"]
    severity: "critical"
  maintenance:
    reasons: []
    severity: "info"
    default_state: "disable"
`)
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    mysql_connections: "70"
    container_cpu: "70"
`)
	writeTestFile(t, dir, "db-b.yaml", `
tenants:
  db-b:
    mysql_connections: "100"
    mysql_cpu: "60"
    _state_container_crashloop: "disable"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.Load(); err != nil {
		t.Fatalf("LoadDir failed: %v", err)
	}

	if mgr.Mode() != "directory" {
		t.Errorf("expected directory mode, got %s", mgr.Mode())
	}

	cfg := mgr.GetConfig()

	if len(cfg.Defaults) != 3 {
		t.Errorf("expected 3 defaults, got %d", len(cfg.Defaults))
	}
	if len(cfg.StateFilters) != 2 {
		t.Errorf("expected 2 state_filters, got %d", len(cfg.StateFilters))
	}
	if len(cfg.Tenants) != 2 {
		t.Errorf("expected 2 tenants, got %d", len(cfg.Tenants))
	}
	if cfg.Tenants["db-a"]["mysql_connections"].Default != "70" {
		t.Errorf("expected db-a mysql_connections=70, got %s", cfg.Tenants["db-a"]["mysql_connections"].Default)
	}
	if cfg.Tenants["db-b"]["mysql_cpu"].Default != "60" {
		t.Errorf("expected db-b mysql_cpu=60, got %s", cfg.Tenants["db-b"]["mysql_cpu"].Default)
	}

	// db-a: 3 metrics, db-b: 3 metrics = 6
	resolved := cfg.Resolve()
	if len(resolved) != 6 {
		t.Errorf("expected 6 resolved thresholds, got %d: %+v", len(resolved), resolved)
	}
}

func TestConfigManager_LoadDir_BoundaryEnforcement(t *testing.T) {
	dir := t.TempDir()

	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
`)
	// Tenant file tries to sneak in defaults and state_filters → should be ignored
	writeTestFile(t, dir, "db-a.yaml", `
defaults:
  mysql_connections: 999
state_filters:
  sneaky_filter:
    reasons: ["SneakyReason"]
tenants:
  db-a:
    mysql_connections: "70"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.Load(); err != nil {
		t.Fatalf("LoadDir failed: %v", err)
	}

	cfg := mgr.GetConfig()

	if cfg.Defaults["mysql_connections"] != 80 {
		t.Errorf("boundary violation: expected 80, got %.0f", cfg.Defaults["mysql_connections"])
	}
	if len(cfg.StateFilters) != 0 {
		t.Errorf("boundary violation: expected 0 state_filters, got %d", len(cfg.StateFilters))
	}
	if cfg.Tenants["db-a"]["mysql_connections"].Default != "70" {
		t.Errorf("expected db-a tenant data preserved, got %s", cfg.Tenants["db-a"]["mysql_connections"].Default)
	}
}

func TestConfigManager_LoadDir_HashChangeDetection(t *testing.T) {
	dir := t.TempDir()

	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
`)
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    mysql_connections: "70"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load failed: %v", err)
	}
	hash1 := mgr.lastHash

	// Reload without changes
	if err := mgr.Load(); err != nil {
		t.Fatalf("Reload failed: %v", err)
	}
	if mgr.lastHash != hash1 {
		t.Error("hash should not change without modifications")
	}

	// Modify file
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    mysql_connections: "90"
`)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Reload after change failed: %v", err)
	}
	if mgr.lastHash == hash1 {
		t.Error("hash should change after modification")
	}
	if mgr.GetConfig().Tenants["db-a"]["mysql_connections"].Default != "90" {
		t.Error("expected updated value 90")
	}
}

func TestConfigManager_LoadDir_EmptyDir(t *testing.T) {
	dir := t.TempDir()
	mgr := NewConfigManager(dir)
	if err := mgr.Load(); err == nil {
		t.Error("expected error for empty directory")
	}
}

func TestConfigManager_LoadDir_SkipsHiddenAndSubdirs(t *testing.T) {
	dir := t.TempDir()

	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
`)
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    mysql_connections: "70"
`)
	writeTestFile(t, dir, ".hidden.yaml", `
defaults:
  mysql_connections: 999
`)

	subdir := filepath.Join(dir, "subdir")
	os.MkdirAll(subdir, 0700)
	writeTestFile(t, subdir, "extra.yaml", `
tenants:
  db-c:
    mysql_connections: "50"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	cfg := mgr.GetConfig()
	if cfg.Defaults["mysql_connections"] != 80 {
		t.Errorf("expected 80 (hidden file ignored), got %.0f", cfg.Defaults["mysql_connections"])
	}
	if len(cfg.Tenants) != 1 {
		t.Errorf("expected 1 tenant (subdir ignored), got %d", len(cfg.Tenants))
	}
}

func TestConfigManager_LoadDir_CriticalSuffix(t *testing.T) {
	dir := t.TempDir()

	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
`)
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    mysql_connections: "70"
    mysql_connections_critical: "120"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	resolved := mgr.GetConfig().Resolve()
	sort.Slice(resolved, func(i, j int) bool {
		return resolved[i].Severity < resolved[j].Severity
	})

	if len(resolved) != 2 {
		t.Fatalf("expected 2 (warning + critical), got %d: %+v", len(resolved), resolved)
	}
	if resolved[0].Severity != "critical" || resolved[0].Value != 120 {
		t.Errorf("expected critical=120, got %s=%.0f", resolved[0].Severity, resolved[0].Value)
	}
	if resolved[1].Severity != "warning" || resolved[1].Value != 70 {
		t.Errorf("expected warning=70, got %s=%.0f", resolved[1].Severity, resolved[1].Value)
	}
}

// ============================================================
// Phase 2B: Dimensional Metrics Tests
// ============================================================

func TestParseKeyWithLabels(t *testing.T) {
	tests := []struct {
		input       string
		wantBase    string
		wantLabels  map[string]string
		wantRegex   map[string]string
	}{
		// No labels
		{"redis_memory", "redis_memory", nil, nil},
		{"standalone", "standalone", nil, nil},
		// Single label (double quotes)
		{`redis_db_keys{db="db0"}`, "redis_db_keys", map[string]string{"db": "db0"}, nil},
		// Single label (single quotes)
		{`redis_db_keys{db='db0'}`, "redis_db_keys", map[string]string{"db": "db0"}, nil},
		// Multiple labels
		{`redis_queue_length{queue="tasks", priority="high"}`, "redis_queue_length", map[string]string{"queue": "tasks", "priority": "high"}, nil},
		// Spaces around equals and commas
		{`es_index_size{index = "logstash-*" , tier = "hot"}`, "es_index_size", map[string]string{"index": "logstash-*", "tier": "hot"}, nil},
		// B1: Regex label
		{`oracle_tablespace{tablespace=~"SYS.*"}`, "oracle_tablespace", nil, map[string]string{"tablespace": "SYS.*"}},
		// B1: Mixed exact + regex
		{`oracle_ts{env="prod", tablespace=~"SYS.*"}`, "oracle_ts", map[string]string{"env": "prod"}, map[string]string{"tablespace": "SYS.*"}},
	}

	for _, tt := range tests {
		base, labels, regex := parseKeyWithLabels(tt.input)
		if base != tt.wantBase {
			t.Errorf("parseKeyWithLabels(%q): base = %q, want %q", tt.input, base, tt.wantBase)
		}
		// Check exact labels
		if tt.wantLabels == nil {
			if labels != nil {
				t.Errorf("parseKeyWithLabels(%q): labels = %v, want nil", tt.input, labels)
			}
		} else {
			if len(labels) != len(tt.wantLabels) {
				t.Errorf("parseKeyWithLabels(%q): labels count = %d, want %d", tt.input, len(labels), len(tt.wantLabels))
				continue
			}
			for k, v := range tt.wantLabels {
				if labels[k] != v {
					t.Errorf("parseKeyWithLabels(%q): labels[%q] = %q, want %q", tt.input, k, labels[k], v)
				}
			}
		}
		// Check regex labels
		if tt.wantRegex == nil {
			if regex != nil {
				t.Errorf("parseKeyWithLabels(%q): regex = %v, want nil", tt.input, regex)
			}
		} else {
			if len(regex) != len(tt.wantRegex) {
				t.Errorf("parseKeyWithLabels(%q): regex count = %d, want %d", tt.input, len(regex), len(tt.wantRegex))
				continue
			}
			for k, v := range tt.wantRegex {
				if regex[k] != v {
					t.Errorf("parseKeyWithLabels(%q): regex[%q] = %q, want %q", tt.input, k, regex[k], v)
				}
			}
		}
	}
}

func TestResolve_DimensionalBasic(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"redis_memory": 80,
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"redis_memory": SV("75"),
				`redis_queue_length{queue="tasks"}`:                   SV("500"),
				`redis_queue_length{queue="events", priority="high"}`: SV("1000:critical"),
			},
		},
	}

	resolved := cfg.Resolve()
	sort.Slice(resolved, func(i, j int) bool {
		if resolved[i].Metric != resolved[j].Metric {
			return resolved[i].Metric < resolved[j].Metric
		}
		return resolved[i].Value < resolved[j].Value
	})

	// Expected: 1 base metric (redis_memory=75) + 2 dimensional (queue_length 500 + 1000)
	if len(resolved) != 3 {
		t.Fatalf("expected 3 resolved, got %d: %+v", len(resolved), resolved)
	}

	// Find the base metric
	var base *ResolvedThreshold
	var dims []ResolvedThreshold
	for i := range resolved {
		if len(resolved[i].CustomLabels) == 0 {
			base = &resolved[i]
		} else {
			dims = append(dims, resolved[i])
		}
	}

	if base == nil {
		t.Fatal("expected a base metric without custom labels")
	}
	if base.Metric != "memory" || base.Value != 75 || base.Component != "redis" {
		t.Errorf("base metric: got metric=%s value=%.0f component=%s", base.Metric, base.Value, base.Component)
	}

	if len(dims) != 2 {
		t.Fatalf("expected 2 dimensional metrics, got %d", len(dims))
	}

	sort.Slice(dims, func(i, j int) bool { return dims[i].Value < dims[j].Value })

	// queue_length 500 (warning)
	if dims[0].Metric != "queue_length" || dims[0].Value != 500 || dims[0].Severity != "warning" {
		t.Errorf("dim[0]: got metric=%s value=%.0f severity=%s", dims[0].Metric, dims[0].Value, dims[0].Severity)
	}
	if dims[0].CustomLabels["queue"] != "tasks" {
		t.Errorf("dim[0]: expected queue=tasks, got %v", dims[0].CustomLabels)
	}

	// queue_length 1000 (critical)
	if dims[1].Metric != "queue_length" || dims[1].Value != 1000 || dims[1].Severity != "critical" {
		t.Errorf("dim[1]: got metric=%s value=%.0f severity=%s", dims[1].Metric, dims[1].Value, dims[1].Severity)
	}
	if dims[1].CustomLabels["queue"] != "events" || dims[1].CustomLabels["priority"] != "high" {
		t.Errorf("dim[1]: expected queue=events priority=high, got %v", dims[1].CustomLabels)
	}
}

func TestResolve_DimensionalDisable(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				`redis_queue_length{queue="tasks"}`:  SV("500"),
				`redis_queue_length{queue="events"}`: SV("disable"),
			},
		},
	}

	resolved := cfg.Resolve()
	if len(resolved) != 1 {
		t.Fatalf("expected 1 (disabled one skipped), got %d: %+v", len(resolved), resolved)
	}
	if resolved[0].CustomLabels["queue"] != "tasks" {
		t.Errorf("expected queue=tasks, got %v", resolved[0].CustomLabels)
	}
}

func TestResolve_DimensionalBackwardCompat(t *testing.T) {
	// Non-dimensional config should still work identically
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"mysql_connections": 80,
			"mysql_cpu":         80,
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"mysql_connections": SV("70")},
			"db-b": {"mysql_connections": SV("disable"), "mysql_cpu": SV("40")},
		},
	}

	resolved := cfg.Resolve()
	for _, r := range resolved {
		if len(r.CustomLabels) > 0 {
			t.Errorf("non-dimensional config should have no CustomLabels, got %v", r.CustomLabels)
		}
	}

	sort.Slice(resolved, func(i, j int) bool {
		if resolved[i].Tenant != resolved[j].Tenant {
			return resolved[i].Tenant < resolved[j].Tenant
		}
		return resolved[i].Metric < resolved[j].Metric
	})

	if len(resolved) != 3 {
		t.Fatalf("expected 3, got %d", len(resolved))
	}
}

func TestResolve_DimensionalWithDirMode(t *testing.T) {
	dir := t.TempDir()

	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  redis_memory: 80
`)
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    redis_memory: "75"
    "redis_db_keys{db=\"db0\"}": "1000"
    "redis_db_keys{db=\"db1\"}": "disable"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	resolved := mgr.GetConfig().Resolve()
	// Expected: redis_memory=75 + redis_db_keys{db=db0}=1000 = 2 (db1 disabled)
	if len(resolved) != 2 {
		t.Fatalf("expected 2, got %d: %+v", len(resolved), resolved)
	}

	var hasDimensional bool
	for _, r := range resolved {
		if len(r.CustomLabels) > 0 {
			hasDimensional = true
			if r.CustomLabels["db"] != "db0" || r.Value != 1000 {
				t.Errorf("expected db=db0 value=1000, got %v value=%.0f", r.CustomLabels, r.Value)
			}
		}
	}
	if !hasDimensional {
		t.Error("expected at least one dimensional metric")
	}
}

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

// ============================================================
// Phase 11 B1: Regex Dimensional Labels Tests
// ============================================================

func TestParseLabelsStringWithOp(t *testing.T) {
	tests := []struct {
		input     string
		wantExact map[string]string
		wantRegex map[string]string
	}{
		// Pure exact
		{`queue="tasks"`, map[string]string{"queue": "tasks"}, map[string]string{}},
		// Pure regex
		{`tablespace=~"SYS.*"`, map[string]string{}, map[string]string{"tablespace": "SYS.*"}},
		// Mixed
		{`env="prod", tablespace=~"SYS.*"`, map[string]string{"env": "prod"}, map[string]string{"tablespace": "SYS.*"}},
		// Multiple regex
		{`ns=~"db-.*", table=~"SYS.*"`, map[string]string{}, map[string]string{"ns": "db-.*", "table": "SYS.*"}},
	}

	for _, tt := range tests {
		exact, regex := parseLabelsStringWithOp(tt.input)
		for k, v := range tt.wantExact {
			if exact[k] != v {
				t.Errorf("parseLabelsStringWithOp(%q): exact[%q] = %q, want %q", tt.input, k, exact[k], v)
			}
		}
		for k, v := range tt.wantRegex {
			if regex[k] != v {
				t.Errorf("parseLabelsStringWithOp(%q): regex[%q] = %q, want %q", tt.input, k, regex[k], v)
			}
		}
	}
}

func TestResolve_RegexDimensional(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"oracle_tablespace": 80,
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"oracle_tablespace":                              SV("75"),
				`oracle_tablespace{tablespace=~"SYS.*"}`:         SV("95"),
				`oracle_tablespace{tablespace=~"USER.*"}`:        SV("500:critical"),
				`oracle_tablespace{env="prod", ts=~"TEMP.*"}`:    SV("200"),
			},
		},
	}

	resolved := cfg.Resolve()

	// Should get: 1 base + 3 dimensional = 4
	if len(resolved) != 4 {
		t.Fatalf("expected 4, got %d: %+v", len(resolved), resolved)
	}

	var base *ResolvedThreshold
	var dims []ResolvedThreshold
	for i := range resolved {
		if len(resolved[i].CustomLabels) == 0 && len(resolved[i].RegexLabels) == 0 {
			base = &resolved[i]
		} else {
			dims = append(dims, resolved[i])
		}
	}

	if base == nil || base.Value != 75 {
		t.Errorf("base: expected value=75, got %+v", base)
	}

	if len(dims) != 3 {
		t.Fatalf("expected 3 dimensional, got %d", len(dims))
	}

	sort.Slice(dims, func(i, j int) bool { return dims[i].Value < dims[j].Value })

	// 95: regex only
	if dims[0].RegexLabels["tablespace"] != "SYS.*" || dims[0].Value != 95 {
		t.Errorf("dim[0]: expected tablespace_re=SYS.* value=95, got %+v", dims[0])
	}
	if len(dims[0].CustomLabels) != 0 {
		t.Errorf("dim[0]: expected no exact labels, got %v", dims[0].CustomLabels)
	}

	// 200: mixed exact + regex
	if dims[1].CustomLabels["env"] != "prod" || dims[1].RegexLabels["ts"] != "TEMP.*" || dims[1].Value != 200 {
		t.Errorf("dim[1]: got %+v", dims[1])
	}

	// 500: regex + critical severity
	if dims[2].RegexLabels["tablespace"] != "USER.*" || dims[2].Value != 500 || dims[2].Severity != "critical" {
		t.Errorf("dim[2]: got %+v", dims[2])
	}
}

func TestResolve_RegexDimensionalWithYAML(t *testing.T) {
	content := `
defaults:
  oracle_tablespace: 80
tenants:
  db-a:
    oracle_tablespace: "75"
    "oracle_tablespace{tablespace=~\"SYS.*\"}": "95"
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

	resolved := mgr.GetConfig().Resolve()
	if len(resolved) != 2 {
		t.Fatalf("expected 2, got %d: %+v", len(resolved), resolved)
	}

	var hasRegex bool
	for _, r := range resolved {
		if len(r.RegexLabels) > 0 {
			hasRegex = true
			if r.RegexLabels["tablespace"] != "SYS.*" || r.Value != 95 {
				t.Errorf("expected tablespace_re=SYS.* value=95, got %v value=%.0f", r.RegexLabels, r.Value)
			}
		}
	}
	if !hasRegex {
		t.Error("expected at least one regex dimensional metric")
	}
}

func TestResolve_RegexScheduled(t *testing.T) {
	// B1 + B4 combined: regex dimensional with time-window override
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"oracle_tablespace": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				`oracle_tablespace{tablespace=~"SYS.*"}`: SVScheduled("95",
					TimeWindowOverride{Window: "01:00-09:00", Value: "disable"},
				),
			},
		},
	}

	// During window — disabled
	inside := time.Date(2026, 1, 15, 3, 0, 0, 0, time.UTC)
	resolved := cfg.ResolveAt(inside)
	// Only base metric (from default), regex one is disabled
	baseCount := 0
	for _, r := range resolved {
		if len(r.RegexLabels) == 0 && len(r.CustomLabels) == 0 {
			baseCount++
		}
	}
	if baseCount != 1 {
		t.Errorf("during window: expected 1 base metric only, got %d total: %+v", len(resolved), resolved)
	}

	// Outside window — regex metric active
	outside := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)
	resolved = cfg.ResolveAt(outside)
	if len(resolved) != 2 {
		t.Errorf("outside window: expected 2 (base + regex), got %d: %+v", len(resolved), resolved)
	}
}

// ============================================================
// Negative Tests: Unsupported Combinations
// ============================================================

// TestResolve_RegexDimensionalCriticalNotSupported verifies that
// _critical suffix on regex dimensional keys has no effect.
// Regex dimensional keys must use "value:severity" syntax instead.
func TestResolve_RegexDimensionalCriticalNotSupported(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"oracle_tablespace": 90,
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"oracle_tablespace":                                   SV("85"),
				`oracle_tablespace{tablespace=~"SYS.*"}`:             SV("95"),
				`oracle_tablespace{tablespace=~"SYS.*"}_critical`:    SV("99"),
			},
		},
	}

	resolved := cfg.Resolve()

	// The _critical key with {} is NOT processed by the _critical scanner
	// because it doesn't match: it contains "{" so it enters the dimensional block,
	// but "oracle_tablespace{tablespace=~\"SYS.*\"}_critical" won't parse as
	// a valid dimensional key (the _critical suffix is outside the braces and
	// the base key becomes "oracle_tablespace{tablespace=~\"SYS" which is invalid).
	// Result: the _critical key is silently ignored.
	// The _critical scanner also won't pick it up because it contains "{".
	//
	// Expected: base metric (85) + regex dimensional (95) = 2
	// The _critical key produces nothing.
	var hasRegex bool
	var hasCriticalSeverity bool
	for _, r := range resolved {
		if len(r.RegexLabels) > 0 {
			hasRegex = true
		}
		if r.Severity == "critical" {
			hasCriticalSeverity = true
		}
	}

	if !hasRegex {
		t.Error("expected regex dimensional metric to be present")
	}
	if hasCriticalSeverity {
		t.Error("regex dimensional + _critical suffix should NOT produce a critical metric; use 'value:critical' syntax instead")
	}

	// Correct approach: use "95:critical" value syntax for regex dimensional
	cfg2 := &ThresholdConfig{
		Defaults: map[string]float64{},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				`oracle_tablespace{tablespace=~"SYS.*"}`: SV("95:critical"),
			},
		},
	}
	resolved2 := cfg2.Resolve()
	if len(resolved2) != 1 || resolved2[0].Severity != "critical" {
		t.Errorf("value:critical syntax should work for regex dimensional, got %+v", resolved2)
	}
}

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

// ============================================================
// Helpers
// ============================================================

// writeTestFile is a helper to create YAML files in test directories.
func writeTestFile(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
}
