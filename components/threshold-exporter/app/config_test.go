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
		Tenants: map[string]map[string]string{
			"db-a": {"mysql_connections": "70"},
			"db-b": {"mysql_connections": "100", "_state_container_crashloop": "disable"},
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
		t.Errorf("expected 0, got %d: %+v", len(resolved), resolved)
	}
}

func TestResolveStateFilters_NoFilters(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants:  map[string]map[string]string{"db-a": {"mysql_connections": "70"}},
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
		Tenants:      map[string]map[string]string{"db-a": {}},
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
		Tenants:      map[string]map[string]string{"db-a": {"mysql_connections": "70", "_state_container_crashloop": "disable"}},
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
	if cfg.Tenants["db-a"]["mysql_connections"] != "70" {
		t.Errorf("expected db-a mysql_connections=70, got %s", cfg.Tenants["db-a"]["mysql_connections"])
	}
	if cfg.Tenants["db-b"]["mysql_cpu"] != "60" {
		t.Errorf("expected db-b mysql_cpu=60, got %s", cfg.Tenants["db-b"]["mysql_cpu"])
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
	if cfg.Tenants["db-a"]["mysql_connections"] != "70" {
		t.Errorf("expected db-a tenant data preserved, got %s", cfg.Tenants["db-a"]["mysql_connections"])
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
	if mgr.GetConfig().Tenants["db-a"]["mysql_connections"] != "90" {
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
		input      string
		wantBase   string
		wantLabels map[string]string
	}{
		// No labels
		{"redis_memory", "redis_memory", nil},
		{"standalone", "standalone", nil},
		// Single label (double quotes)
		{`redis_db_keys{db="db0"}`, "redis_db_keys", map[string]string{"db": "db0"}},
		// Single label (single quotes)
		{`redis_db_keys{db='db0'}`, "redis_db_keys", map[string]string{"db": "db0"}},
		// Multiple labels
		{`redis_queue_length{queue="tasks", priority="high"}`, "redis_queue_length", map[string]string{"queue": "tasks", "priority": "high"}},
		// Spaces around equals and commas
		{`es_index_size{index = "logstash-*" , tier = "hot"}`, "es_index_size", map[string]string{"index": "logstash-*", "tier": "hot"}},
	}

	for _, tt := range tests {
		base, labels := parseKeyWithLabels(tt.input)
		if base != tt.wantBase {
			t.Errorf("parseKeyWithLabels(%q): base = %q, want %q", tt.input, base, tt.wantBase)
		}
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
	}
}

func TestResolve_DimensionalBasic(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{
			"redis_memory": 80,
		},
		Tenants: map[string]map[string]string{
			"db-a": {
				"redis_memory": "75",
				`redis_queue_length{queue="tasks"}`:                   "500",
				`redis_queue_length{queue="events", priority="high"}`: "1000:critical",
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
		Tenants: map[string]map[string]string{
			"db-a": {
				`redis_queue_length{queue="tasks"}`:  "500",
				`redis_queue_length{queue="events"}`: "disable",
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
		Tenants: map[string]map[string]string{
			"db-a": {"mysql_connections": "70"},
			"db-b": {"mysql_connections": "disable", "mysql_cpu": "40"},
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

// writeTestFile is a helper to create YAML files in test directories.
func writeTestFile(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
}
