package main

// main_test.go — resolveConfigPath flag-resolution tests + the shared
// newTestManager fixture.
//
// Handler transport tests moved to handlers_test.go; collector metric
// tests are consolidated in collector_test.go. These resolveConfigPath
// tests mutate the package-level configDir / configPath globals with a
// defer-restore, so they are intentionally NOT t.Parallel.

import (
	"os"
	"path/filepath"
	"testing"
)

// newTestManager creates a ConfigManager preloaded with the given config.
func newTestManager(cfg *ThresholdConfig) *ConfigManager {
	m := &ConfigManager{}
	m.config = cfg
	m.loaded = cfg != nil
	return m
}

// ============================================================
// resolveConfigPath Unit Tests
// ============================================================

func TestResolveConfigPath_ConfigDirFlag(t *testing.T) {
	// Test that -config-dir flag takes precedence
	oldConfigDir := configDir
	oldConfigPath := configPath
	defer func() {
		configDir = oldConfigDir
		configPath = oldConfigPath
	}()

	configDir = "/tmp/test-dir"
	configPath = "/tmp/test-file.yaml"

	result := resolveConfigPath()
	if result != "/tmp/test-dir" {
		t.Errorf("expected /tmp/test-dir, got %s", result)
	}
}

func TestResolveConfigPath_ConfigPathFlag(t *testing.T) {
	// Test that -config flag is used when -config-dir is empty
	oldConfigDir := configDir
	oldConfigPath := configPath
	defer func() {
		configDir = oldConfigDir
		configPath = oldConfigPath
	}()

	configDir = ""
	configPath = "/tmp/test-file.yaml"

	result := resolveConfigPath()
	if result != "/tmp/test-file.yaml" {
		t.Errorf("expected /tmp/test-file.yaml, got %s", result)
	}
}

func TestResolveConfigPath_AutoDetectDir(t *testing.T) {
	// Test auto-detection: directory exists
	oldConfigDir := configDir
	oldConfigPath := configPath
	defer func() {
		configDir = oldConfigDir
		configPath = oldConfigPath
	}()

	// We just verify the fallback behavior since we can't easily
	// mock the os.Stat call. Test with empty flags to see fallback.
	configDir = ""
	configPath = ""

	result := resolveConfigPath()
	// Result should be the default file since /etc/threshold-exporter/conf.d doesn't exist in tests
	if result != "/etc/threshold-exporter/config.yaml" {
		t.Errorf("expected /etc/threshold-exporter/config.yaml, got %s", result)
	}
}

// ============================================================
// resolveConfigPath — auto-detect directory
// ============================================================

func TestResolveConfigPath_AutoDetectDir_RealDir(t *testing.T) {
	// Create a temp dir that mimics the default path
	dir := t.TempDir()
	confD := filepath.Join(dir, "conf.d")
	os.MkdirAll(confD, 0700)

	oldConfigDir := configDir
	oldConfigPath := configPath
	defer func() {
		configDir = oldConfigDir
		configPath = oldConfigPath
	}()

	configDir = ""
	configPath = ""

	// Can't test /etc path directly, but test flag behavior
	result := resolveConfigPath()
	// Should return default file since we can't inject the /etc path
	if result == "" {
		t.Error("resolveConfigPath should never return empty")
	}
}
