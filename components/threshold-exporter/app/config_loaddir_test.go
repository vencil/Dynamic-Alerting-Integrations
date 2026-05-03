package main

// LoadDir tests — directory-mode YAML scanning, deep-merge, boundary-rule
// enforcement, hash-change detection, hidden-file skip, _critical suffix
// parsing. Split out of config_test.go in PR-2; shared helpers live in
// config_test.go.

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"
)

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
