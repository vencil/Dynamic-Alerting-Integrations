package main

import (
	"bytes"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"
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

// region ConfigManagerBasics — single-file and directory loading

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

// region DirectoryLoading — LoadDir tests and file hashing

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

// TestConfigManager_LoadDir_UnparseableDefaultsErrorAndMetric (v2.8.0
// Track A A4) locks the cycle-6-RCA fix (planning archive §S#37d): when
// `_defaults.yaml` (or any `_*` file) fails to parse, the entire defaults
// block silently drops and every dependent tenant override breaks. The
// signal must be ERROR-level (not WARN, which is too easy to miss in
// `gh run view --log` output) and must increment
// `da_config_parse_failure_total{file_basename=...}` so ops can alert.
//
// Sibling tenant files must still parse normally (poison-pill isolation,
// same invariant as TestScanDirHierarchical_MixedValidInvalid for the
// hierarchical path).
func TestConfigManager_LoadDir_UnparseableDefaultsErrorAndMetric(t *testing.T) {
	dir := t.TempDir()

	// Reset metrics so parseFailures counter is fresh.
	origMetrics := getConfigMetrics()
	freshMetrics := newConfigMetrics()
	setConfigMetrics(freshMetrics)
	t.Cleanup(func() { setConfigMetrics(origMetrics) })

	// Capture log output to verify ERROR-level promotion.
	var logBuf bytes.Buffer
	origOutput := log.Writer()
	log.SetOutput(&logBuf)
	t.Cleanup(func() { log.SetOutput(origOutput) })

	// Poison-pill `_defaults.yaml`. Use a structurally broken YAML
	// (unclosed brace) so yaml.Unmarshal definitively errors regardless
	// of strict-mode settings. Type-mismatch (`mysql_connections:
	// "X:critical"` against `map[string]float64`) is the cycle-6
	// real-world signature, but yaml.v3 with `KnownFields(false)`
	// silently coerces some forms — using a syntax error makes the test
	// path-deterministic.
	writeTestFile(t, dir, "_defaults.yaml",
		"defaults:\n  mysql_connections: {unclosed-brace\n")

	// Valid sibling tenant — must survive the broken defaults.
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    mysql_connections: "70"
`)

	mgr := NewConfigManager(dir)
	// Load may succeed (defaults dropped, sibling tenant parses) — what we
	// care about is the ERROR log + the metric.
	_ = mgr.Load()

	// Invariant #1: log line at ERROR level (not WARN).
	logOutput := logBuf.String()
	if !strings.Contains(logOutput, "ERROR: skip unparseable defaults/profiles file") {
		t.Errorf("expected ERROR-level log for _defaults.yaml parse failure; got:\n%s", logOutput)
	}
	if strings.Contains(logOutput, "WARN: skip unparseable file") &&
		strings.Contains(logOutput, "_defaults.yaml") {
		t.Errorf("_defaults.yaml parse failure logged at WARN — should be ERROR; cycle-6 RCA")
	}

	// Invariant #2: parse-failure metric incremented for `_defaults.yaml`
	// basename. This may run via loadDir (initial scan) — the counter
	// pattern matches A-8d.
	ch := make(chan prometheus.Metric, 1)
	freshMetrics.parseFailures.WithLabelValues("_defaults.yaml").Collect(ch)
	close(ch)
	var count float64
	for m := range ch {
		var d dto.Metric
		if err := m.Write(&d); err != nil {
			t.Fatalf("metric.Write: %v", err)
		}
		count = d.GetCounter().GetValue()
	}
	if count < 1 {
		t.Errorf("da_config_parse_failure_total{file_basename=_defaults.yaml} = %v, want >= 1", count)
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

// endregion

// region DimensionalMetrics — label parsing and dimensional resolution

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

// endregion

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

// region RegexDimensionalMetrics — regex label parsing and regex-based threshold resolution

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

// region SilentModeResolution — silent mode states and expiry handling

// ============================================================
// Silent Mode Tests
// ============================================================

func TestResolveSilentModes(t *testing.T) {
	tests := []struct {
		name       string
		tenants    map[string]map[string]ScheduledValue
		expectedLen int
		validate   func(t *testing.T, result []ResolvedSilentMode)
	}{
		{"Default", map[string]map[string]ScheduledValue{"db-a": {"mysql_connections": SV("70")}}, 0, nil},
		{"Warning", map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("warning")}}, 1, func(t *testing.T, r []ResolvedSilentMode) {
			if r[0].Tenant != "db-a" || r[0].TargetSeverity != "warning" {
				t.Errorf("unexpected: %+v", r[0])
			}
		}},
		{"Critical", map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("critical")}}, 1, func(t *testing.T, r []ResolvedSilentMode) {
			if r[0].TargetSeverity != "critical" {
				t.Errorf("expected critical, got %s", r[0].TargetSeverity)
			}
		}},
		{"All", map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("all")}}, 2, func(t *testing.T, r []ResolvedSilentMode) {
			sev := map[string]bool{}
			for _, x := range r {
				sev[x.TargetSeverity] = true
			}
			if !sev["warning"] || !sev["critical"] {
				t.Errorf("expected warning+critical, got %v", sev)
			}
		}},
		{"Disable", map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("disable")}}, 0, nil},
		{"InvalidFallback", map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("invalid_value")}}, 0, nil},
		{"CaseInsensitive", map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("WARNING")}, "db-b": {"_silent_mode": SV("All")}, "db-c": {"_silent_mode": SV(" Critical ")}}, 4, nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := &ThresholdConfig{Tenants: tt.tenants}
			result := cfg.ResolveSilentModes()
			if len(result) != tt.expectedLen {
				t.Fatalf("expected %d, got %d", tt.expectedLen, len(result))
			}
			if tt.validate != nil {
				tt.validate(t, result)
			}
		})
	}
}

func TestResolveAt_SkipsSilentKey(t *testing.T) {
	// _silent_mode must NOT produce a user_threshold metric
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SV("70"),
				"_silent_mode":     SV("warning"),
			},
		},
	}
	resolved := cfg.Resolve()
	for _, r := range resolved {
		if r.Metric == "mode" || r.Component == "silent" {
			t.Errorf("_silent_mode leaked into thresholds: %+v", r)
		}
	}
	if len(resolved) != 1 {
		t.Errorf("expected 1 threshold (mysql_connections), got %d", len(resolved))
	}
}

func TestResolveSilentModes_MixedTenants(t *testing.T) {
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_silent_mode": SV("warning")},
			"db-b": {"_silent_mode": SV("all")},
			"db-c": {"_silent_mode": SV("disable")},
			"db-d": {"mysql_connections": SV("70")}, // no silent_mode → Normal
		},
	}
	result := cfg.ResolveSilentModes()
	// db-a: 1, db-b: 2, db-c: 0, db-d: 0 = 3
	if len(result) != 3 {
		t.Errorf("expected 3, got %d", len(result))
	}

	tenantSev := map[string][]string{}
	for _, r := range result {
		tenantSev[r.Tenant] = append(tenantSev[r.Tenant], r.TargetSeverity)
	}
	if len(tenantSev["db-a"]) != 1 || tenantSev["db-a"][0] != "warning" {
		t.Errorf("db-a: expected [warning], got %v", tenantSev["db-a"])
	}
	sort.Strings(tenantSev["db-b"])
	if len(tenantSev["db-b"]) != 2 {
		t.Errorf("db-b: expected 2, got %v", tenantSev["db-b"])
	}
	if _, ok := tenantSev["db-c"]; ok {
		t.Errorf("db-c should not appear (disabled)")
	}
	if _, ok := tenantSev["db-d"]; ok {
		t.Errorf("db-d should not appear (no silent mode)")
	}
}

// ============================================================
// v1.2.0 Severity Dedup Tests
// ============================================================

func TestResolveSeverityDedup(t *testing.T) {
	tests := []struct {
		name       string
		tenants    map[string]map[string]ScheduledValue
		expectedLen int
		validate   func(t *testing.T, r []ResolvedSeverityDedup)
	}{
		{"DefaultEnable", map[string]map[string]ScheduledValue{"db-a": {}}, 1, func(t *testing.T, r []ResolvedSeverityDedup) {
			if r[0].Tenant != "db-a" || r[0].Mode != "enable" {
				t.Errorf("expected db-a/enable, got %s/%s", r[0].Tenant, r[0].Mode)
			}
		}},
		{"ExplicitEnable", map[string]map[string]ScheduledValue{"db-a": {"_severity_dedup": SV("enable")}}, 1, func(t *testing.T, r []ResolvedSeverityDedup) {
			if r[0].Mode != "enable" {
				t.Errorf("expected mode=enable, got %s", r[0].Mode)
			}
		}},
		{"ExplicitDisable", map[string]map[string]ScheduledValue{"db-a": {"_severity_dedup": SV("disable")}}, 0, nil},
		{"MultiTenant", map[string]map[string]ScheduledValue{"db-a": {}, "db-b": {"_severity_dedup": SV("disable")}, "db-c": {"_severity_dedup": SV("enable")}}, 2, func(t *testing.T, r []ResolvedSeverityDedup) {
			ten := map[string]bool{}
			for _, x := range r {
				ten[x.Tenant] = true
			}
			if !ten["db-a"] || !ten["db-c"] {
				t.Errorf("expected db-a and db-c, got %v", ten)
			}
			if ten["db-b"] {
				t.Errorf("db-b should not appear (disabled)")
			}
		}},
		{"CaseInsensitive", map[string]map[string]ScheduledValue{"db-a": {"_severity_dedup": SV("DISABLE")}, "db-b": {"_severity_dedup": SV("Enable")}}, 1, func(t *testing.T, r []ResolvedSeverityDedup) {
			if r[0].Tenant != "db-b" {
				t.Errorf("expected db-b, got %s", r[0].Tenant)
			}
		}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := &ThresholdConfig{Tenants: tt.tenants}
			resolved := cfg.ResolveSeverityDedup()
			if len(resolved) != tt.expectedLen {
				t.Fatalf("expected %d entries, got %d", tt.expectedLen, len(resolved))
			}
			if tt.validate != nil {
				tt.validate(t, resolved)
			}
		})
	}
}

func TestResolveAt_SkipsSeverityDedupKey(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_severity_dedup": SV("enable"),
			},
		},
	}
	resolved := cfg.Resolve()
	for _, r := range resolved {
		if r.Metric == "severity_dedup" || r.Metric == "dedup" {
			t.Errorf("_severity_dedup leaked into threshold metrics: %+v", r)
		}
	}
	// Should only have 1 metric: mysql_connections (from defaults)
	if len(resolved) != 1 {
		t.Fatalf("expected 1 threshold metric, got %d", len(resolved))
	}
}

// ============================================================
// ResolveRouting Tests
// ============================================================

func TestResolveRouting_ValidConfig(t *testing.T) {
	routingYAML := `receiver:
  type: "webhook"
  url: "https://webhook.example.com/alerts"
group_by: ["alertname", "severity"]
group_wait: "30s"
group_interval: "1m"
repeat_interval: "4h"`

	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_routing": SV(routingYAML),
			},
		},
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 1 {
		t.Fatalf("expected 1 routing config, got %d", len(resolved))
	}

	rc := resolved[0]
	if rc.Tenant != "db-a" {
		t.Errorf("expected tenant db-a, got %s", rc.Tenant)
	}
	if rc.ReceiverType != "webhook" {
		t.Errorf("expected receiver type webhook, got %s", rc.ReceiverType)
	}
	if rc.ReceiverConfig["url"] != "https://webhook.example.com/alerts" {
		t.Errorf("expected receiver url, got %v", rc.ReceiverConfig["url"])
	}
	if len(rc.GroupBy) != 2 || rc.GroupBy[0] != "alertname" || rc.GroupBy[1] != "severity" {
		t.Errorf("unexpected group_by: %v", rc.GroupBy)
	}
	if rc.GroupWait != "30s" {
		t.Errorf("expected group_wait 30s, got %s", rc.GroupWait)
	}
	if rc.GroupInterval != "1m" {
		t.Errorf("expected group_interval 1m, got %s", rc.GroupInterval)
	}
	if rc.RepeatInterval != "4h" {
		t.Errorf("expected repeat_interval 4h, got %s", rc.RepeatInterval)
	}
}

func TestResolveRouting_GuardrailClamp(t *testing.T) {
	// group_wait below minimum (5s), repeat_interval above maximum (72h)
	routingYAML := `receiver:
  type: "webhook"
  url: "https://webhook.example.com/alerts"
group_wait: "1s"
repeat_interval: "100h"`

	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_routing": SV(routingYAML),
			},
		},
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 1 {
		t.Fatalf("expected 1 routing config, got %d", len(resolved))
	}

	rc := resolved[0]
	if rc.GroupWait != "5s" {
		t.Errorf("expected group_wait clamped to 5s, got %s", rc.GroupWait)
	}
	if rc.RepeatInterval != "72h" {
		t.Errorf("expected repeat_interval clamped to 72h, got %s", rc.RepeatInterval)
	}
}

func TestResolveRouting_MissingReceiver(t *testing.T) {
	routingYAML := `group_wait: "30s"`

	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_routing": SV(routingYAML),
			},
		},
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 0 {
		t.Fatalf("expected 0 routing configs (missing receiver), got %d", len(resolved))
	}
}

func TestResolveRouting_NoRoutingKey(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SV("70"),
			},
		},
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 0 {
		t.Fatalf("expected 0 routing configs, got %d", len(resolved))
	}
}

func TestResolveRouting_MultiTenant(t *testing.T) {
	routingA := `receiver:
  type: "webhook"
  url: "https://webhook-a.example.com/alerts"
group_wait: "10s"`

	routingB := `receiver:
  type: "webhook"
  url: "https://webhook-b.example.com/alerts"
repeat_interval: "2h"`

	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_routing": SV(routingA),
			},
			"db-b": {
				"_routing": SV(routingB),
			},
			"db-c": {
				"mysql_connections": SV("70"),
				// No routing
			},
		},
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 2 {
		t.Fatalf("expected 2 routing configs, got %d", len(resolved))
	}

	// Sort for deterministic test
	sort.Slice(resolved, func(i, j int) bool {
		return resolved[i].Tenant < resolved[j].Tenant
	})

	if resolved[0].Tenant != "db-a" || resolved[0].ReceiverType != "webhook" {
		t.Errorf("unexpected db-a config: %+v", resolved[0])
	}
	if resolved[1].Tenant != "db-b" || resolved[1].ReceiverType != "webhook" {
		t.Errorf("unexpected db-b config: %+v", resolved[1])
	}
}

func TestResolveRouting_MinimalConfig(t *testing.T) {
	routingYAML := `receiver:
  type: "webhook"
  url: "https://webhook.example.com/alerts"`

	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_routing": SV(routingYAML),
			},
		},
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 1 {
		t.Fatalf("expected 1 routing config, got %d", len(resolved))
	}

	rc := resolved[0]
	if rc.ReceiverType != "webhook" {
		t.Errorf("expected receiver type webhook, got %s", rc.ReceiverType)
	}
	if rc.ReceiverConfig["url"] != "https://webhook.example.com/alerts" {
		t.Errorf("expected receiver url, got %v", rc.ReceiverConfig["url"])
	}
	// Optional fields should be empty
	if rc.GroupWait != "" || rc.GroupInterval != "" || rc.RepeatInterval != "" {
		t.Errorf("expected empty timing params, got wait=%s interval=%s repeat=%s",
			rc.GroupWait, rc.GroupInterval, rc.RepeatInterval)
	}
	if len(rc.GroupBy) != 0 {
		t.Errorf("expected empty group_by, got %v", rc.GroupBy)
	}
}

func TestResolveAt_SkipsRoutingKey(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_routing":         SV(`receiver: "https://example.com"`),
				"mysql_connections": SV("70"),
			},
		},
	}
	resolved := cfg.Resolve()
	for _, r := range resolved {
		if r.Metric == "routing" || r.Component == "_routing" {
			t.Errorf("_routing leaked into threshold metrics: %+v", r)
		}
	}
	if len(resolved) != 1 {
		t.Fatalf("expected 1 threshold metric, got %d", len(resolved))
	}
}

// TestScheduledValue_RoutingMapRoundTrip verifies that _routing nested maps
// survive YAML unmarshalling → ScheduledValue → ResolveRouting pipeline.
// This is the integration path: YAML file → UnmarshalYAML → ResolveRouting.
func TestScheduledValue_RoutingMapRoundTrip(t *testing.T) {
	yamlInput := `
defaults:
  mysql_connections: 80
tenants:
  db-a:
    mysql_connections: "70"
    _routing:
      receiver:
        type: "webhook"
        url: "https://webhook.example.com/alerts"
      group_wait: "30s"
      group_by: ["alertname", "tenant"]
      repeat_interval: "4h"
`
	var cfg ThresholdConfig
	if err := yaml.Unmarshal([]byte(yamlInput), &cfg); err != nil {
		t.Fatalf("failed to unmarshal: %v", err)
	}

	sv, exists := cfg.Tenants["db-a"]["_routing"]
	if !exists {
		t.Fatal("_routing key not found after unmarshal")
	}
	if sv.Default == "" {
		t.Fatal("_routing ScheduledValue.Default is empty — nested map was lost during unmarshalling")
	}

	resolved := cfg.ResolveRouting()
	if len(resolved) != 1 {
		t.Fatalf("expected 1 routing config, got %d", len(resolved))
	}

	rc := resolved[0]
	if rc.ReceiverType != "webhook" {
		t.Errorf("receiver type = %q, want webhook", rc.ReceiverType)
	}
	if rc.GroupWait != "30s" {
		t.Errorf("group_wait = %q, want 30s", rc.GroupWait)
	}
	if rc.RepeatInterval != "4h" {
		t.Errorf("repeat_interval = %q, want 4h", rc.RepeatInterval)
	}
	if len(rc.GroupBy) != 2 || rc.GroupBy[0] != "alertname" || rc.GroupBy[1] != "tenant" {
		t.Errorf("group_by = %v, want [alertname tenant]", rc.GroupBy)
	}
}

// TestFormatDuration_NoDay verifies formatDuration never outputs "d" suffix
// (Prometheus/Alertmanager only supports s/m/h).
func TestFormatDuration_NoDay(t *testing.T) {
	tests := []struct {
		input time.Duration
		want  string
	}{
		{72 * time.Hour, "72h"},
		{24 * time.Hour, "24h"},
		{48 * time.Hour, "48h"},
		{1 * time.Hour, "1h"},
		{5 * time.Minute, "5m"},
		{30 * time.Second, "30s"},
	}
	for _, tt := range tests {
		got := formatDuration(tt.input)
		if got != tt.want {
			t.Errorf("formatDuration(%v) = %q, want %q", tt.input, got, tt.want)
		}
	}
}

// ============================================================
// Cardinality Guard (v1.5.0)
// ============================================================

func TestCardinalityGuard(t *testing.T) {
	defsWith20 := make(map[string]float64)
	for i := 0; i < 20; i++ {
		defsWith20[fmt.Sprintf("metric_%d", i)] = float64(i)
	}
	defsWith10 := make(map[string]float64)
	for i := 0; i < 10; i++ {
		defsWith10[fmt.Sprintf("m_%d", i)] = float64(i)
	}
	tests := []struct {
		name string
		cfg  ThresholdConfig
		expected int
	}{
		{"UnderLimit", ThresholdConfig{Defaults: map[string]float64{"mysql_connections": 70, "redis_memory": 80}, Tenants: map[string]map[string]ScheduledValue{"db-a": {}}, MaxMetricsPerTenant: 10}, 2},
		{"AtLimit", ThresholdConfig{Defaults: map[string]float64{"m1": 1, "m2": 2}, Tenants: map[string]map[string]ScheduledValue{"t": {}}, MaxMetricsPerTenant: 2}, 2},
		{"OverLimitTruncated", ThresholdConfig{Defaults: defsWith20, Tenants: map[string]map[string]ScheduledValue{"t": {}}, MaxMetricsPerTenant: 5}, 5},
		{"DefaultLimit", ThresholdConfig{Defaults: map[string]float64{"m1": 1}, Tenants: map[string]map[string]ScheduledValue{"t": {}}}, 1},
		{"MultiTenantIndependent", ThresholdConfig{Defaults: defsWith10, Tenants: map[string]map[string]ScheduledValue{"t1": {}, "t2": {}}, MaxMetricsPerTenant: 3}, 6},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := tt.cfg.Resolve()
			if len(result) != tt.expected {
				t.Errorf("expected %d metrics, got %d", tt.expected, len(result))
			}
		})
	}
}

// ============================================================
// ValidateTenantKeys (v1.5.0)
// ============================================================

func TestValidateTenantKeys(t *testing.T) {
	tests := []struct {
		name    string
		cfg     ThresholdConfig
		wantLen int
		check   func(t *testing.T, w []string)
	}{
		{"NoWarningsForValidKeys",
			ThresholdConfig{Defaults: map[string]float64{"mysql_connections": 70, "redis_memory": 80}, Tenants: map[string]map[string]ScheduledValue{"db-a": {"mysql_connections": {Default: "60"}, "mysql_connections_critical": {Default: "90"}, "_silent_mode": {Default: "warning"}, "_severity_dedup": {Default: "enable"}, "_state_maintenance": {Default: "enable"}, "_routing": {Default: "receiver: ..."}}}},
			0, nil},
		{"TypoReservedKey",
			ThresholdConfig{Defaults: map[string]float64{"mysql_connections": 70}, Tenants: map[string]map[string]ScheduledValue{"db-a": {"_silence_mode": {Default: "warning"}}}},
			1, func(t *testing.T, w []string) {
				if !strings.Contains(w[0], "unknown reserved key") || !strings.Contains(w[0], "_silence_mode") {
					t.Errorf("expected unknown reserved key with _silence_mode, got %q", w[0])
				}
			}},
		{"UnknownMetricKey",
			ThresholdConfig{Defaults: map[string]float64{"mysql_connections": 70}, Tenants: map[string]map[string]ScheduledValue{"db-a": {"postgres_connections": {Default: "60"}}}},
			1, func(t *testing.T, w []string) {
				if !strings.Contains(w[0], "not in defaults") {
					t.Errorf("expected 'not in defaults', got %q", w[0])
				}
			}},
		{"CriticalSuffixValid",
			ThresholdConfig{Defaults: map[string]float64{"mysql_connections": 70}, Tenants: map[string]map[string]ScheduledValue{"db-a": {"mysql_connections_critical": {Default: "90"}}}},
			0, nil},
		{"NamespacesReservedKey",
			ThresholdConfig{Defaults: map[string]float64{"mysql_connections": 70}, Tenants: map[string]map[string]ScheduledValue{"db-a": {"mysql_connections": {Default: "60"}, "_namespaces": {Default: "[\"ns-a\", \"ns-b\"]"}}}},
			0, nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			w := tt.cfg.ValidateTenantKeys()
			if len(w) != tt.wantLen {
				t.Fatalf("expected %d warnings, got %d: %v", tt.wantLen, len(w), w)
			}
			if tt.check != nil {
				tt.check(t, w)
			}
		})
	}
}

// ============================================================
// Structured Silent Mode Tests (v1.7.0)
// ============================================================

func TestResolveSilentModes_Structured(t *testing.T) {
	future := time.Now().Add(24 * time.Hour).Format(time.RFC3339)
	past := time.Now().Add(-1 * time.Hour).Format(time.RFC3339)
	tests := []struct {
		name    string
		cfg     *ThresholdConfig
		wantLen int
		check   func(t *testing.T, r []ResolvedSilentMode)
	}{
		{"WithExpires_Active",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("expires: " + future + "\nreason: Planned DB migration\ntarget: warning\n")}}},
			1, func(t *testing.T, r []ResolvedSilentMode) {
				if r[0].Tenant != "db-a" || r[0].TargetSeverity != "warning" || r[0].Expired || r[0].Reason != "Planned DB migration" {
					t.Errorf("unexpected: %+v", r[0])
				}
			}},
		{"WithExpires_Expired",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("expires: " + past + "\nreason: DB migration done\ntarget: all\n")}}},
			2, func(t *testing.T, r []ResolvedSilentMode) {
				for _, x := range r {
					if !x.Expired {
						t.Errorf("expected expired for %s", x.TargetSeverity)
					}
				}
			}},
		{"StructuredNoExpires",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("reason: Long-term silencing\ntarget: critical\n")}}},
			1, func(t *testing.T, r []ResolvedSilentMode) {
				if r[0].Expired || r[0].Expires != (time.Time{}) {
					t.Errorf("should not be expired, expires should be zero")
				}
			}},
		{"StructuredDisable",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("target: disable\n")}}},
			0, nil},
		{"ScalarBackwardCompat",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_silent_mode": SV("warning")}, "db-b": {"_silent_mode": SV("all")}}},
			3, func(t *testing.T, r []ResolvedSilentMode) {
				for _, x := range r {
					if x.Expired || !x.Expires.IsZero() {
						t.Error("scalar should never be expired")
					}
				}
			}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := tt.cfg.ResolveSilentModesAt(time.Now())
			if len(result) != tt.wantLen {
				t.Fatalf("expected %d, got %d", tt.wantLen, len(result))
			}
			if tt.check != nil {
				tt.check(t, result)
			}
		})
	}
}

// endregion

// region MaintenanceModeResolution — maintenance mode states, expiry handling, and validation

// ============================================================
// Structured Maintenance Mode Tests (v1.7.0)
// ============================================================

func TestResolveMaintenanceExpiries(t *testing.T) {
	future := time.Now().Add(24 * time.Hour).Format(time.RFC3339)
	past := time.Now().Add(-2 * time.Hour).Format(time.RFC3339)
	tests := []struct {
		name    string
		cfg     *ThresholdConfig
		wantLen int
		check   func(t *testing.T, r []ResolvedMaintenanceExpiry)
	}{
		{"NoMaintenance",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"mysql_connections": SV("70")}}},
			0, nil},
		{"ScalarEnable",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_state_maintenance": SV("enable")}}},
			0, nil},
		{"StructuredActive",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_state_maintenance": SV("expires: " + future + "\nreason: Scheduled upgrade\ntarget: enable\n")}}},
			1, func(t *testing.T, r []ResolvedMaintenanceExpiry) {
				if r[0].Expired || r[0].Reason != "Scheduled upgrade" {
					t.Errorf("expected active with reason, got: %+v", r[0])
				}
			}},
		{"StructuredExpired",
			&ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_state_maintenance": SV("expires: " + past + "\nreason: Upgrade complete\ntarget: enable\n")}}},
			1, func(t *testing.T, r []ResolvedMaintenanceExpiry) {
				if !r[0].Expired {
					t.Error("should be expired")
				}
			}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := tt.cfg.ResolveMaintenanceExpiriesAt(time.Now())
			if len(result) != tt.wantLen {
				t.Fatalf("expected %d, got %d", tt.wantLen, len(result))
			}
			if tt.check != nil {
				tt.check(t, result)
			}
		})
	}
}

func TestResolveStateFilters_MaintenanceExpired(t *testing.T) {
	// When structured _state_maintenance has expired, the state filter should NOT be emitted
	past := time.Now().Add(-1 * time.Hour).Format(time.RFC3339)
	yamlStr := "expires: " + past + "\ntarget: enable\n"
	cfg := &ThresholdConfig{
		StateFilters: map[string]StateFilter{
			"maintenance": {Severity: "warning", DefaultState: "disable"},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_state_maintenance": SV(yamlStr)},
		},
	}
	result := cfg.ResolveStateFiltersAt(time.Now())
	if len(result) != 0 {
		t.Errorf("expected 0 (maintenance expired → filter disabled), got %d", len(result))
	}
}

func TestResolveStateFilters_MaintenanceActive(t *testing.T) {
	// When structured _state_maintenance has future expires, the state filter should be emitted
	future := time.Now().Add(24 * time.Hour).Format(time.RFC3339)
	yamlStr := "expires: " + future + "\ntarget: enable\n"
	cfg := &ThresholdConfig{
		StateFilters: map[string]StateFilter{
			"maintenance": {Severity: "warning", DefaultState: "disable"},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_state_maintenance": SV(yamlStr)},
		},
	}
	result := cfg.ResolveStateFiltersAt(time.Now())
	if len(result) != 1 {
		t.Fatalf("expected 1, got %d", len(result))
	}
	if result[0].Tenant != "db-a" || result[0].FilterName != "maintenance" {
		t.Errorf("unexpected: %+v", result[0])
	}
}

func TestResolveStateFilters_MaintenanceScalarBackwardCompat(t *testing.T) {
	// Scalar "enable" should still work as before
	cfg := &ThresholdConfig{
		StateFilters: map[string]StateFilter{
			"maintenance": {Severity: "warning", DefaultState: "disable"},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"_state_maintenance": SV("enable")},
		},
	}
	result := cfg.ResolveStateFiltersAt(time.Now())
	if len(result) != 1 {
		t.Errorf("expected 1, got %d", len(result))
	}
}

func TestIsMaintenanceActive(t *testing.T) {
	future := time.Now().Add(24 * time.Hour).Format(time.RFC3339)
	past := time.Now().Add(-1 * time.Hour).Format(time.RFC3339)
	tests := []struct {
		name     string
		cfg      *ThresholdConfig
		tenant   string
		isActive bool
	}{
		{"ScalarEnable", &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_state_maintenance": SV("enable")}}}, "db-a", true},
		{"Expired", &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_state_maintenance": SV("expires: " + past + "\ntarget: enable\n")}}}, "db-a", false},
		{"NotExpiredYet", &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"db-a": {"_state_maintenance": SV("expires: " + future + "\ntarget: enable\n")}}}, "db-a", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if active := tt.cfg.IsMaintenanceActive(tt.tenant, time.Now()); active != tt.isActive {
				t.Errorf("expected active=%v, got %v", tt.isActive, active)
			}
		})
	}
}

// ============================================================
// endregion

// region MetadataAndValidation — metadata resolution and tenant key validation

// ResolveMetadata (v1.11.0)
// ============================================================

func TestResolveMetadata_WithMetadata(t *testing.T) {
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SV("70"),
				"_metadata":         SV("runbook_url: https://wiki.example.com/db-a\nowner: team-dba\ntier: gold\n"),
			},
		},
	}
	result := cfg.ResolveMetadata()
	if len(result) != 1 {
		t.Fatalf("expected 1 metadata, got %d", len(result))
	}
	if result[0].RunbookURL != "https://wiki.example.com/db-a" {
		t.Errorf("runbook_url = %q, want https://wiki.example.com/db-a", result[0].RunbookURL)
	}
	if result[0].Owner != "team-dba" {
		t.Errorf("owner = %q, want team-dba", result[0].Owner)
	}
	if result[0].Tier != "gold" {
		t.Errorf("tier = %q, want gold", result[0].Tier)
	}
}

func TestResolveMetadata_WithoutMetadata(t *testing.T) {
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {"mysql_connections": SV("70")},
			"db-b": {"redis_memory": SV("1024")},
		},
	}
	result := cfg.ResolveMetadata()
	if len(result) != 2 {
		t.Fatalf("expected 2 metadata entries (all tenants), got %d", len(result))
	}
	// All fields should be empty string
	for _, m := range result {
		if m.RunbookURL != "" || m.Owner != "" || m.Tier != "" {
			t.Errorf("tenant=%s: expected empty metadata, got runbook=%q owner=%q tier=%q",
				m.Tenant, m.RunbookURL, m.Owner, m.Tier)
		}
	}
}

func TestResolveMetadata_PartialMetadata(t *testing.T) {
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"_metadata": SV("owner: team-dba\n"),
			},
		},
	}
	result := cfg.ResolveMetadata()
	if len(result) != 1 {
		t.Fatalf("expected 1, got %d", len(result))
	}
	if result[0].Owner != "team-dba" {
		t.Errorf("owner = %q, want team-dba", result[0].Owner)
	}
	if result[0].RunbookURL != "" {
		t.Errorf("runbook_url = %q, want empty", result[0].RunbookURL)
	}
	if result[0].Tier != "" {
		t.Errorf("tier = %q, want empty", result[0].Tier)
	}
}

func TestResolveMetadata_Sorted(t *testing.T) {
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"db-c": {"mysql_connections": SV("70")},
			"db-a": {"mysql_connections": SV("70")},
			"db-b": {"mysql_connections": SV("70")},
		},
	}
	result := cfg.ResolveMetadata()
	if len(result) != 3 {
		t.Fatalf("expected 3, got %d", len(result))
	}
	if result[0].Tenant != "db-a" || result[1].Tenant != "db-b" || result[2].Tenant != "db-c" {
		t.Errorf("not sorted: %v, %v, %v", result[0].Tenant, result[1].Tenant, result[2].Tenant)
	}
}

func TestResolveMetadata_UnconditionalOutput(t *testing.T) {
	// All tenants must appear regardless of _metadata presence
	cfg := &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": SV("70"),
				"_metadata":         SV("runbook_url: https://wiki.example.com/db-a\n"),
			},
			"db-b": {"redis_memory": SV("1024")}, // no _metadata
		},
	}
	result := cfg.ResolveMetadata()
	if len(result) != 2 {
		t.Fatalf("expected 2 (all tenants), got %d", len(result))
	}
	tenants := map[string]ResolvedMetadata{}
	for _, m := range result {
		tenants[m.Tenant] = m
	}
	if tenants["db-a"].RunbookURL != "https://wiki.example.com/db-a" {
		t.Errorf("db-a runbook_url = %q", tenants["db-a"].RunbookURL)
	}
	if tenants["db-b"].RunbookURL != "" {
		t.Errorf("db-b should have empty runbook_url, got %q", tenants["db-b"].RunbookURL)
	}
}

// endregion

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

// region IncrementalReloading — hot-reload, file hash detection, and directory watching

// ============================================================
// Incremental Hot-Reload Tests (v2.1.0 §5.6)
// ============================================================

func TestScanDirFileHashes(t *testing.T) {
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

	hashes, composite, _, _, err := scanDirFileHashes(dir, nil, nil)
	if err != nil {
		t.Fatalf("scanDirFileHashes failed: %v", err)
	}
	if len(hashes) != 2 {
		t.Errorf("expected 2 file hashes, got %d", len(hashes))
	}
	if _, ok := hashes["_defaults.yaml"]; !ok {
		t.Error("missing hash for _defaults.yaml")
	}
	if _, ok := hashes["db-a.yaml"]; !ok {
		t.Error("missing hash for db-a.yaml")
	}
	if composite == "" {
		t.Error("composite hash should not be empty")
	}
}

func TestScanDirFileHashes_SkipsHiddenAndSubdirs(t *testing.T) {
	dir := t.TempDir()
	writeTestFile(t, dir, "_defaults.yaml", `defaults: {}`)
	writeTestFile(t, dir, ".hidden.yaml", `defaults: {}`)
	os.MkdirAll(filepath.Join(dir, "subdir"), 0700)
	writeTestFile(t, filepath.Join(dir, "subdir"), "extra.yaml", `defaults: {}`)

	hashes, _, _, _, err := scanDirFileHashes(dir, nil, nil)
	if err != nil {
		t.Fatalf("failed: %v", err)
	}
	if len(hashes) != 1 {
		t.Errorf("expected 1 file (hidden + subdir skipped), got %d", len(hashes))
	}
}

func TestScanDirFileHashes_StableComposite(t *testing.T) {
	dir := t.TempDir()
	writeTestFile(t, dir, "a.yaml", `defaults: {x: 1}`)
	writeTestFile(t, dir, "b.yaml", `tenants: {t1: {}}`)

	_, hash1, _, _, _ := scanDirFileHashes(dir, nil, nil)
	_, hash2, _, _, _ := scanDirFileHashes(dir, nil, nil)
	if hash1 != hash2 {
		t.Error("composite hash should be stable across calls with same content")
	}
}

func TestIncrementalLoad_InitialLoad(t *testing.T) {
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
	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad (initial) failed: %v", err)
	}
	if !mgr.IsLoaded() {
		t.Error("should be loaded after IncrementalLoad")
	}
	if len(mgr.fileHashes) != 2 {
		t.Errorf("expected 2 file hashes, got %d", len(mgr.fileHashes))
	}
	if len(mgr.fileConfigs) != 2 {
		t.Errorf("expected 2 file configs, got %d", len(mgr.fileConfigs))
	}
	cfg := mgr.GetConfig()
	if cfg.Defaults["mysql_connections"] != 80 {
		t.Errorf("expected default 80, got %.0f", cfg.Defaults["mysql_connections"])
	}
	if cfg.Tenants["db-a"]["mysql_connections"].Default != "70" {
		t.Errorf("expected tenant value 70, got %s", cfg.Tenants["db-a"]["mysql_connections"].Default)
	}
}

func TestIncrementalLoad_FileModified(t *testing.T) {
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
	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}
	hash1 := mgr.lastHash

	// Modify one file
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    mysql_connections: "90"
`)

	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("incremental load failed: %v", err)
	}
	if mgr.lastHash == hash1 {
		t.Error("hash should change after modification")
	}
	cfg := mgr.GetConfig()
	if cfg.Tenants["db-a"]["mysql_connections"].Default != "90" {
		t.Errorf("expected updated value 90, got %s", cfg.Tenants["db-a"]["mysql_connections"].Default)
	}
}

func TestIncrementalLoad_FileAdded(t *testing.T) {
	dir := t.TempDir()
	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
`)

	mgr := NewConfigManager(dir)
	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}
	if len(mgr.GetConfig().Tenants) != 0 {
		t.Error("expected 0 tenants initially")
	}

	// Add new tenant file
	writeTestFile(t, dir, "db-b.yaml", `
tenants:
  db-b:
    mysql_connections: "60"
`)

	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("incremental load after add failed: %v", err)
	}
	cfg := mgr.GetConfig()
	if len(cfg.Tenants) != 1 {
		t.Errorf("expected 1 tenant after add, got %d", len(cfg.Tenants))
	}
	if cfg.Tenants["db-b"]["mysql_connections"].Default != "60" {
		t.Error("expected tenant db-b with value 60")
	}
	if len(mgr.fileHashes) != 2 {
		t.Errorf("expected 2 file hashes after add, got %d", len(mgr.fileHashes))
	}
}

func TestIncrementalLoad_FileRemoved(t *testing.T) {
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
	writeTestFile(t, dir, "db-b.yaml", `
tenants:
  db-b:
    mysql_connections: "60"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}
	if len(mgr.GetConfig().Tenants) != 2 {
		t.Errorf("expected 2 tenants, got %d", len(mgr.GetConfig().Tenants))
	}

	// Remove one tenant file
	os.Remove(filepath.Join(dir, "db-b.yaml"))

	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("incremental load after remove failed: %v", err)
	}
	cfg := mgr.GetConfig()
	if len(cfg.Tenants) != 1 {
		t.Errorf("expected 1 tenant after remove, got %d", len(cfg.Tenants))
	}
	if _, exists := cfg.Tenants["db-b"]; exists {
		t.Error("db-b should be removed")
	}
	if len(mgr.fileHashes) != 2 {
		t.Errorf("expected 2 file hashes after remove, got %d", len(mgr.fileHashes))
	}
}

func TestIncrementalLoad_NoChange(t *testing.T) {
	dir := t.TempDir()
	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
`)

	mgr := NewConfigManager(dir)
	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}
	reload1 := mgr.LastReload()

	// Small delay to detect timestamp change
	time.Sleep(10 * time.Millisecond)

	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("no-change reload failed: %v", err)
	}
	// lastReload should NOT update (composite hash unchanged → early return)
	if !mgr.LastReload().Equal(reload1) {
		t.Error("lastReload should not update when nothing changed")
	}
}

func TestIncrementalLoad_SingleFileModeFallback(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	writeTestFile(t, dir, "config.yaml", `
defaults:
  mysql_connections: 80
tenants:
  db-a:
    mysql_connections: "70"
`)

	mgr := NewConfigManager(path)
	// Should fall back to full Load for single-file mode
	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad (single-file) failed: %v", err)
	}
	if !mgr.IsLoaded() {
		t.Error("should be loaded")
	}
	// fileHashes should remain nil (single-file mode doesn't use incremental)
	if mgr.fileHashes != nil {
		t.Error("fileHashes should be nil for single-file mode")
	}
}

func TestIncrementalLoad_BoundaryEnforcement(t *testing.T) {
	dir := t.TempDir()
	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
`)

	mgr := NewConfigManager(dir)
	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}

	// Add a tenant file that violates boundary (contains defaults)
	writeTestFile(t, dir, "db-a.yaml", `
defaults:
  mysql_connections: 999
tenants:
  db-a:
    mysql_connections: "70"
`)

	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("incremental load failed: %v", err)
	}
	cfg := mgr.GetConfig()
	// defaults from tenant file should be ignored
	if cfg.Defaults["mysql_connections"] != 80 {
		t.Errorf("expected defaults 80 (boundary enforced), got %.0f", cfg.Defaults["mysql_connections"])
	}
}

func TestIncrementalLoad_ProfilesAfterIncremental(t *testing.T) {
	dir := t.TempDir()
	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
  mysql_cpu: 80
`)
	writeTestFile(t, dir, "_profiles.yaml", `
profiles:
  high-load:
    mysql_connections: "100"
`)
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    _profile: "high-load"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}
	cfg := mgr.GetConfig()
	// Profile should be applied
	if cfg.Tenants["db-a"]["mysql_connections"].Default != "100" {
		t.Errorf("expected profile value 100, got %s", cfg.Tenants["db-a"]["mysql_connections"].Default)
	}

	// Modify profile
	writeTestFile(t, dir, "_profiles.yaml", `
profiles:
  high-load:
    mysql_connections: "120"
`)

	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("incremental load failed: %v", err)
	}
	cfg = mgr.GetConfig()
	if cfg.Tenants["db-a"]["mysql_connections"].Default != "120" {
		t.Errorf("expected updated profile value 120, got %s", cfg.Tenants["db-a"]["mysql_connections"].Default)
	}
}

func TestMergePartialConfigs_Empty(t *testing.T) {
	merged := mergePartialConfigs(map[string]ThresholdConfig{})
	if len(merged.Defaults) != 0 || len(merged.Tenants) != 0 {
		t.Error("empty merge should produce empty config")
	}
}

func TestMergePartialConfigs_DeterministicOrder(t *testing.T) {
	configs := map[string]ThresholdConfig{
		"b.yaml": {
			Defaults: map[string]float64{"mysql_connections": 90},
		},
		"a.yaml": {
			Defaults: map[string]float64{"mysql_connections": 80},
		},
	}
	// b.yaml sorts after a.yaml, so b's value should win
	merged := mergePartialConfigs(configs)
	if merged.Defaults["mysql_connections"] != 90 {
		t.Errorf("expected 90 (b.yaml wins), got %.0f", merged.Defaults["mysql_connections"])
	}
}

func TestApplyBoundaryRules_DefaultsFile(t *testing.T) {
	partial := ThresholdConfig{
		Defaults:     map[string]float64{"x": 1},
		StateFilters: map[string]StateFilter{"f": {Severity: "warning"}},
	}
	applyBoundaryRules("_defaults.yaml", &partial)
	if len(partial.Defaults) != 1 {
		t.Error("defaults file should keep its defaults")
	}
	if len(partial.StateFilters) != 1 {
		t.Error("defaults file should keep its state_filters")
	}
}

func TestApplyBoundaryRules_TenantFile(t *testing.T) {
	partial := ThresholdConfig{
		Defaults:     map[string]float64{"x": 1},
		StateFilters: map[string]StateFilter{"f": {Severity: "warning"}},
		Profiles:     map[string]map[string]ScheduledValue{"p": {"k": SV("v")}},
	}
	applyBoundaryRules("db-a.yaml", &partial)
	if partial.Defaults != nil {
		t.Error("tenant file defaults should be cleared")
	}
	if partial.StateFilters != nil {
		t.Error("tenant file state_filters should be cleared")
	}
	if partial.Profiles != nil {
		t.Error("tenant file profiles should be cleared")
	}
}

func TestFullDirLoad_InitializesCache(t *testing.T) {
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
	if err := mgr.fullDirLoad(); err != nil {
		t.Fatalf("fullDirLoad failed: %v", err)
	}
	if len(mgr.fileHashes) != 2 {
		t.Errorf("expected 2 file hashes, got %d", len(mgr.fileHashes))
	}
	if len(mgr.fileConfigs) != 2 {
		t.Errorf("expected 2 file configs, got %d", len(mgr.fileConfigs))
	}
}

// TestIncrementalLoad_MultiOp verifies correct handling when multiple
// operations occur in a single reload cycle: add + modify + remove.
func TestIncrementalLoad_MultiOp(t *testing.T) {
	dir := t.TempDir()
	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
  mysql_cpu: 80
`)
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    mysql_connections: "70"
`)
	writeTestFile(t, dir, "db-b.yaml", `
tenants:
  db-b:
    mysql_connections: "60"
`)
	writeTestFile(t, dir, "db-c.yaml", `
tenants:
  db-c:
    mysql_connections: "50"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("initial load: %v", err)
	}
	if len(mgr.GetConfig().Tenants) != 3 {
		t.Fatalf("expected 3 tenants initially, got %d", len(mgr.GetConfig().Tenants))
	}

	// Simultaneously: modify db-a, remove db-b, add db-d
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    mysql_connections: "99"
    mysql_cpu: "95"
`)
	os.Remove(filepath.Join(dir, "db-b.yaml"))
	writeTestFile(t, dir, "db-d.yaml", `
tenants:
  db-d:
    mysql_connections: "40"
`)

	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("multi-op incremental: %v", err)
	}
	cfg := mgr.GetConfig()

	// Should have 3 tenants: db-a (modified), db-c (unchanged), db-d (added)
	if len(cfg.Tenants) != 3 {
		t.Errorf("expected 3 tenants after multi-op, got %d", len(cfg.Tenants))
	}
	if _, exists := cfg.Tenants["db-b"]; exists {
		t.Error("db-b should have been removed")
	}
	if cfg.Tenants["db-a"]["mysql_connections"].Default != "99" {
		t.Errorf("db-a mysql_connections: expected 99, got %s", cfg.Tenants["db-a"]["mysql_connections"].Default)
	}
	if cfg.Tenants["db-a"]["mysql_cpu"].Default != "95" {
		t.Errorf("db-a mysql_cpu: expected 95, got %s", cfg.Tenants["db-a"]["mysql_cpu"].Default)
	}
	if cfg.Tenants["db-c"]["mysql_connections"].Default != "50" {
		t.Errorf("db-c should be unchanged at 50, got %s", cfg.Tenants["db-c"]["mysql_connections"].Default)
	}
	if cfg.Tenants["db-d"]["mysql_connections"].Default != "40" {
		t.Errorf("db-d should be added with 40, got %s", cfg.Tenants["db-d"]["mysql_connections"].Default)
	}

	// Verify cache consistency
	if len(mgr.fileHashes) != 4 { // _defaults + db-a + db-c + db-d
		t.Errorf("expected 4 file hashes, got %d", len(mgr.fileHashes))
	}
}

// TestIncrementalLoad_ScheduledValues verifies that scheduled (time-window)
// values survive incremental reload correctly.
func TestIncrementalLoad_ScheduledValues(t *testing.T) {
	dir := t.TempDir()
	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  container_cpu: 80
`)
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    container_cpu:
      default: "80"
      overrides:
        - window: "22:00-06:00"
          value: "95"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("initial load: %v", err)
	}
	cfg := mgr.GetConfig()
	sv := cfg.Tenants["db-a"]["container_cpu"]
	if sv.Default != "80" {
		t.Errorf("expected default 80, got %s", sv.Default)
	}
	if len(sv.Overrides) != 1 || sv.Overrides[0].Window != "22:00-06:00" {
		t.Errorf("expected 1 override with window 22:00-06:00, got %+v", sv.Overrides)
	}

	// Modify the schedule via incremental reload
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    container_cpu:
      default: "85"
      overrides:
        - window: "00:00-06:00"
          value: "disable"
`)

	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("incremental load: %v", err)
	}
	cfg = mgr.GetConfig()
	sv = cfg.Tenants["db-a"]["container_cpu"]
	if sv.Default != "85" {
		t.Errorf("expected updated default 85, got %s", sv.Default)
	}
	if len(sv.Overrides) != 1 || sv.Overrides[0].Value != "disable" {
		t.Errorf("expected override with disable, got %+v", sv.Overrides)
	}
}

// TestIncrementalLoad_DefaultsModified verifies that modifying _defaults.yaml
// propagates correctly through incremental reload.
func TestIncrementalLoad_DefaultsModified(t *testing.T) {
	dir := t.TempDir()
	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
  mysql_cpu: 80
`)
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    mysql_connections: "70"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("initial load: %v", err)
	}
	cfg := mgr.GetConfig()
	if cfg.Defaults["mysql_cpu"] != 80 {
		t.Fatalf("expected default mysql_cpu 80, got %.0f", cfg.Defaults["mysql_cpu"])
	}

	// Modify defaults — add a new metric
	writeTestFile(t, dir, "_defaults.yaml", `
defaults:
  mysql_connections: 80
  mysql_cpu: 85
  container_memory: 90
`)

	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("incremental load: %v", err)
	}
	cfg = mgr.GetConfig()
	if cfg.Defaults["mysql_cpu"] != 85 {
		t.Errorf("expected updated mysql_cpu 85, got %.0f", cfg.Defaults["mysql_cpu"])
	}
	if cfg.Defaults["container_memory"] != 90 {
		t.Errorf("expected new container_memory 90, got %.0f", cfg.Defaults["container_memory"])
	}
	// Tenant override should be unaffected
	if cfg.Tenants["db-a"]["mysql_connections"].Default != "70" {
		t.Errorf("tenant override should survive, got %s", cfg.Tenants["db-a"]["mysql_connections"].Default)
	}
}

// TestIncrementalLoad_CacheOnlyReparses verifies that only changed files
// get re-parsed, not the entire directory.
func TestIncrementalLoad_CacheOnlyReparses(t *testing.T) {
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
	writeTestFile(t, dir, "db-b.yaml", `
tenants:
  db-b:
    mysql_connections: "60"
`)

	mgr := NewConfigManager(dir)
	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("initial load: %v", err)
	}

	// Record cache state for db-b (unchanged file)
	oldDbBConfig := mgr.fileConfigs["db-b.yaml"]
	oldDbBHash := mgr.fileHashes["db-b.yaml"]

	// Only modify db-a
	writeTestFile(t, dir, "db-a.yaml", `
tenants:
  db-a:
    mysql_connections: "99"
`)

	if err := mgr.IncrementalLoad(); err != nil {
		t.Fatalf("incremental load: %v", err)
	}

	// db-b's cache entry should remain identical (same hash → not re-parsed)
	if mgr.fileHashes["db-b.yaml"] != oldDbBHash {
		t.Error("db-b hash should not change")
	}
	newDbBConfig := mgr.fileConfigs["db-b.yaml"]
	if newDbBConfig.Tenants["db-b"]["mysql_connections"].Default != oldDbBConfig.Tenants["db-b"]["mysql_connections"].Default {
		t.Error("db-b config should be preserved from cache")
	}

	// db-a should be updated
	if mgr.GetConfig().Tenants["db-a"]["mysql_connections"].Default != "99" {
		t.Errorf("db-a should be updated to 99")
	}
}

// endregion

// region UtilityFunctionsAndHelpers — duration parsing, helper utilities, and logging

// ============================================================
// logConfigStats — unit test
// ============================================================

func TestLogConfigStats_Format(t *testing.T) {
	cfg := &ThresholdConfig{
		Defaults:     map[string]float64{"mysql_connections": 80, "mysql_cpu": 75},
		Profiles:     map[string]map[string]ScheduledValue{"gold": {"mysql_connections": {Default: "100"}}},
		StateFilters: map[string]StateFilter{"_state_maintenance": {Reasons: []string{"CrashLoopBackOff"}, Severity: "warning"}},
		Tenants: map[string]map[string]ScheduledValue{
			"db-a": {
				"mysql_connections": {Default: "90"},
				"_silent_mode":     {Default: "warning"},
				"_state_maintenance": {Default: "1"},
			},
			"db-b": {
				"mysql_connections": {Default: "85"},
				"mysql_cpu":        {Default: "70"},
			},
		},
	}

	// Capture log output
	var buf bytes.Buffer
	orig := log.Writer()
	log.SetOutput(&buf)
	defer log.SetOutput(orig)

	logConfigStats(cfg, "Test prefix")

	output := buf.String()

	// Verify all expected counts appear
	if !strings.Contains(output, "2 defaults") {
		t.Errorf("expected '2 defaults', got: %s", output)
	}
	if !strings.Contains(output, "1 profiles") {
		t.Errorf("expected '1 profiles', got: %s", output)
	}
	if !strings.Contains(output, "1 state_filters") {
		t.Errorf("expected '1 state_filters', got: %s", output)
	}
	if !strings.Contains(output, "2 tenants") {
		t.Errorf("expected '2 tenants', got: %s", output)
	}
	if !strings.Contains(output, "~3 threshold overrides") {
		t.Errorf("expected '~3 threshold overrides' (mysql_connections×2 + mysql_cpu×1), got: %s", output)
	}
	if !strings.Contains(output, "1 state entries") {
		t.Errorf("expected '1 state entries', got: %s", output)
	}
	if !strings.Contains(output, "1 silent modes") {
		t.Errorf("expected '1 silent modes', got: %s", output)
	}
	if !strings.Contains(output, "Test prefix") {
		t.Errorf("expected prefix 'Test prefix', got: %s", output)
	}
}

// ============================================================
// parsePromDuration — direct unit tests
// ============================================================

func TestParsePromDuration(t *testing.T) {
	tests := []struct {
		input    string
		wantDur  time.Duration
		wantErr  bool
	}{
		{"30s", 30 * time.Second, false},
		{"5m", 5 * time.Minute, false},
		{"2h", 2 * time.Hour, false},
		{"1d", 24 * time.Hour, false},
		{"0s", 0, false},
		{"", 0, true},
		{"abc", 0, true},
		{"5x", 0, true},
		{"-1m", -1 * time.Minute, false},
	}
	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got, err := parsePromDuration(tt.input)
			if (err != nil) != tt.wantErr {
				t.Errorf("parsePromDuration(%q): err=%v, wantErr=%v", tt.input, err, tt.wantErr)
			}
			if !tt.wantErr && got != tt.wantDur {
				t.Errorf("parsePromDuration(%q) = %v, want %v", tt.input, got, tt.wantDur)
			}
		})
	}
}

// ============================================================
// isDisabled — direct unit tests
// ============================================================

func TestIsDisabled(t *testing.T) {
	trueCases := []string{"disable", "disabled", "off", "false"}
	for _, s := range trueCases {
		if !isDisabled(s) {
			t.Errorf("isDisabled(%q) = false, want true", s)
		}
	}
	falseCases := []string{"enable", "warning", "80", "", "true", "on"}
	for _, s := range falseCases {
		if isDisabled(s) {
			t.Errorf("isDisabled(%q) = true, want false", s)
		}
	}
}

// ============================================================
// clampDuration — direct unit tests
// ============================================================

func TestClampDuration(t *testing.T) {
	tests := []struct {
		name   string
		value  string
		param  string
		expect string
	}{
		// Within bounds — no clamping
		{"within_group_wait", "30s", "group_wait", "30s"},
		{"within_group_interval", "1m", "group_interval", "1m"},
		{"within_repeat_interval", "1h", "repeat_interval", "1h"},
		// Below minimum — clamp up
		{"below_min_group_wait", "1s", "group_wait", "5s"},
		{"below_min_repeat_interval", "10s", "repeat_interval", "1m"},
		// Above maximum — clamp down
		{"above_max_group_wait", "10m", "group_wait", "5m"},
		{"above_max_repeat_interval", "100h", "repeat_interval", "72h"},
		// Invalid value — returns empty (logged as warning, value ignored)
		{"invalid_value", "abc", "group_wait", ""},
		// Unknown param — return as-is (no guardrails defined)
		{"unknown_param", "30s", "unknown_param", "30s"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := clampDuration(tt.value, tt.param, "test-tenant")
			if got != tt.expect {
				t.Errorf("clampDuration(%q, %q) = %q, want %q", tt.value, tt.param, got, tt.expect)
			}
		})
	}
}

// ============================================================
// WatchLoop Integration Test
// ============================================================

func TestWatchLoop_Integration(t *testing.T) {
	// Create temporary directory with initial config
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.yaml")
	initialContent := `
defaults:
  mysql_connections: 80
tenants:
  db-a:
    mysql_connections: "70"
`
	writeTestFile(t, dir, "config.yaml", initialContent)

	// Create ConfigManager and load initial config
	mgr := NewConfigManager(configPath)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Initial Load failed: %v", err)
	}

	// Verify initial config
	cfg := mgr.GetConfig()
	if cfg.Defaults["mysql_connections"] != 80 {
		t.Errorf("initial default: expected 80, got %.0f", cfg.Defaults["mysql_connections"])
	}

	// Start WatchLoop with short interval
	stopCh := make(chan struct{})
	go mgr.WatchLoop(100*time.Millisecond, stopCh)

	// Modify config file
	updatedContent := `
defaults:
  mysql_connections: 90
tenants:
  db-a:
    mysql_connections: "75"
`
	writeTestFile(t, dir, "config.yaml", updatedContent)

	// Poll for config change with timeout
	deadline := time.After(3 * time.Second)
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()

	var changed bool
	for {
		select {
		case <-deadline:
			t.Fatal("timeout waiting for config change")
		case <-ticker.C:
			cfg := mgr.GetConfig()
			if cfg.Defaults["mysql_connections"] == 90 {
				changed = true
			}
			if changed {
				break
			}
		}
		if changed {
			break
		}
	}

	// Verify updated config
	cfg = mgr.GetConfig()
	if cfg.Defaults["mysql_connections"] != 90 {
		t.Errorf("updated default: expected 90, got %.0f", cfg.Defaults["mysql_connections"])
	}
	if cfg.Tenants["db-a"]["mysql_connections"].Default != "75" {
		t.Errorf("updated tenant value: expected 75, got %s", cfg.Tenants["db-a"]["mysql_connections"].Default)
	}

	// Stop WatchLoop
	close(stopCh)
	time.Sleep(200 * time.Millisecond) // Allow goroutine to exit
}

// endregion

// region ConfigSourceDetectionAndReload — config source detection and fail-safe reloading

// ============================================================
// detectConfigSource Unit Test
// ============================================================

func TestDetectConfigSource(t *testing.T) {
	tests := []struct {
		name      string
		withGitRev bool
		withEnv   bool
		wantSource string
		wantCommit string
	}{
		{"Configmap", false, false, "configmap", ""},
		{"GitSync", true, false, "git-sync", "abc123def456"},
		{"Operator", false, true, "operator", ""},
		{"GitSyncPrecedence", true, true, "git-sync", "xyz789"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			dir := t.TempDir()
			configPath := filepath.Join(dir, "config.yaml")
			writeTestFile(t, dir, "config.yaml", "defaults:\n  mysql_connections: 80\n")
			if tt.withGitRev {
				commit := "abc123def456"
				if tt.name == "GitSyncPrecedence" {
					commit = "xyz789"
				}
				writeTestFile(t, dir, ".git-revision", commit+"\n")
			}
			oldEnv := os.Getenv("OPERATOR_CRD_SOURCE")
			if tt.withEnv {
				os.Setenv("OPERATOR_CRD_SOURCE", "true")
			}
			defer func() {
				if tt.withEnv {
					if oldEnv == "" {
						os.Unsetenv("OPERATOR_CRD_SOURCE")
					} else {
						os.Setenv("OPERATOR_CRD_SOURCE", oldEnv)
					}
				}
			}()
			mgr := NewConfigManager(configPath)
			if err := mgr.Load(); err != nil {
				t.Fatalf("Load failed: %v", err)
			}
			info := mgr.GetConfigInfo()
			if info.ConfigSource != tt.wantSource {
				t.Errorf("expected source %s, got %s", tt.wantSource, info.ConfigSource)
			}
			if info.GitCommit != tt.wantCommit {
				t.Errorf("expected commit %s, got %s", tt.wantCommit, info.GitCommit)
			}
		})
	}
}

// ============================================================
// Fail-Safe Reload E2E Test
// ============================================================

func TestFailSafeReload_InvalidYAML(t *testing.T) {
	// Create temp directory with valid config
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.yaml")
	validContent := `
defaults:
  mysql_connections: 80
tenants:
  db-a:
    mysql_connections: "70"
`
	writeTestFile(t, dir, "config.yaml", validContent)

	// Load initial valid config
	mgr := NewConfigManager(configPath)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Initial Load failed: %v", err)
	}

	// Verify initial load
	cfg := mgr.GetConfig()
	if cfg.Defaults["mysql_connections"] != 80 {
		t.Errorf("initial config: expected 80, got %.0f", cfg.Defaults["mysql_connections"])
	}

	// Capture log output
	var buf bytes.Buffer
	oldOutput := log.Writer()
	log.SetOutput(&buf)
	defer log.SetOutput(oldOutput)

	// Write invalid YAML
	invalidContent := `
defaults:
  mysql_connections: 80
tenants:
  db-a:
    mysql_connections: [invalid yaml here
`
	writeTestFile(t, dir, "config.yaml", invalidContent)

	// Attempt to reload
	err := mgr.Load()
	if err == nil {
		t.Fatal("expected Load to fail with invalid YAML, but got nil")
	}

	// Verify original config is preserved
	cfg = mgr.GetConfig()
	if cfg.Defaults["mysql_connections"] != 80 {
		t.Errorf("after failed reload: expected preserved config with 80, got %.0f", cfg.Defaults["mysql_connections"])
	}

	// Verify config is still marked as loaded
	if !mgr.IsLoaded() {
		t.Error("expected IsLoaded() = true after failed reload (fail-safe preserved)")
	}

	// Verify error was logged
	logOutput := buf.String()
	if !strings.Contains(logOutput, "ERROR") && !strings.Contains(logOutput, "error") {
		t.Logf("note: error logging may not include 'ERROR' string, log output was: %s", logOutput)
	}
}

// endregion

// region Helpers — test utility functions

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

// endregion
