package main

// Incremental hot-reload tests — scanDirFileHashes, IncrementalLoad
// (initial / modified / added / removed / no-change paths), boundary
// enforcement during reload, profile reload, defaults-modified, cache
// only-reparses-changed-files invariant, mergePartialConfigs. Split out
// of config_test.go in PR-2; shared helpers live in config_test.go.

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

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
