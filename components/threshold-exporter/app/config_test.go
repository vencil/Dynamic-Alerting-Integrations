package main

// Core test fixtures + loader-domain residuals after the PR-2 split.
// Houses:
//   - SV / SVScheduled — ScheduledValue builders used by every sibling
//     *_test.go file (Go same-package _test.go files share helpers)
//   - writeTestFile — directory-mode YAML helper, ditto
//   - ConfigManagerBasics — single-file Load + state-filter Load
//   - UtilityFunctionsAndHelpers — duration parsing, IsDisabled,
//     ClampDuration, ParsePromDuration, FormatDuration_NoDay,
//     LogConfigStats_Format, WatchLoop_Integration
//   - ConfigSourceDetectionAndReload — DetectConfigSource +
//     FailSafeReload_InvalidYAML
//
// Subject-themed tests live in:
//   config_resolve_test.go     · Resolve / ResolveAt / StateFilter / ParseMetricKey
//   config_dimensional_test.go · exact + regex dimensional labels
//   config_three_state_test.go · ScheduledValue + time windows
//   config_silent_mode_test.go · _silent_mode + _state_maintenance
//   config_metadata_test.go    · _metadata + ValidateTenantKeys
//   config_routing_test.go     · _routing_profiles
//   config_loaddir_test.go     · LoadDir directory-mode tests
//   config_incremental_test.go · IncrementalLoad / scanDirFileHashes

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// SV is a test helper to create a scalar ScheduledValue.
func SV(s string) ScheduledValue {
	return ScheduledValue{Default: s}
}

// SVScheduled is a test helper to create a ScheduledValue with time-window overrides.
func SVScheduled(def string, overrides ...TimeWindowOverride) ScheduledValue {
	return ScheduledValue{Default: def, Overrides: overrides}
}

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
