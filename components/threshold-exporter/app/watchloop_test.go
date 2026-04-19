package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// ============================================================
// WatchLoop Tests
// ============================================================

func TestWatchLoop_StopChannel(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)
	writeTestYAML(t, filepath.Join(dir, "db-a.yaml"), `
tenants:
  db-a:
    mysql_connections: "90"
`)

	m := &ConfigManager{
		path:  dir,
		isDir: true,
	}
	if err := m.fullDirLoad(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}

	stopCh := make(chan struct{})
	done := make(chan struct{})

	go func() {
		m.WatchLoop(50*time.Millisecond, stopCh)
		close(done)
	}()

	// Let it tick at least once
	time.Sleep(100 * time.Millisecond)

	// Stop it
	close(stopCh)

	select {
	case <-done:
		// WatchLoop exited — success
	case <-time.After(2 * time.Second):
		t.Error("WatchLoop did not stop within timeout")
	}
}

func TestWatchLoop_DetectsFileChange(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)
	writeTestYAML(t, filepath.Join(dir, "db-a.yaml"), `
tenants:
  db-a:
    mysql_connections: "90"
`)

	m := &ConfigManager{
		path:  dir,
		isDir: true,
	}
	if err := m.fullDirLoad(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}

	cfg := m.GetConfig()
	if cfg == nil || cfg.Defaults["mysql_connections"] != 80 {
		t.Fatalf("initial config mismatch: defaults=%v", cfg.Defaults)
	}

	stopCh := make(chan struct{})
	go m.WatchLoop(50*time.Millisecond, stopCh)

	// Update a file
	time.Sleep(100 * time.Millisecond)
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 95
`)

	// Wait for reload
	time.Sleep(200 * time.Millisecond)
	close(stopCh)

	cfg = m.GetConfig()
	if cfg == nil {
		t.Fatal("config is nil after reload")
	}
	if cfg.Defaults["mysql_connections"] != 95 {
		t.Errorf("expected mysql_connections=95 after reload, got %v", cfg.Defaults["mysql_connections"])
	}
}

func TestWatchLoop_SingleFileMode(t *testing.T) {
	dir := t.TempDir()
	configFile := filepath.Join(dir, "config.yaml")
	writeTestYAML(t, configFile, `
defaults:
  mysql_connections: 80
tenants:
  db-a:
    mysql_connections: "90"
`)

	m := &ConfigManager{
		path:  configFile,
		isDir: false,
	}
	if err := m.Load(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}

	stopCh := make(chan struct{})
	go m.WatchLoop(50*time.Millisecond, stopCh)

	// Update file
	time.Sleep(100 * time.Millisecond)
	writeTestYAML(t, configFile, `
defaults:
  mysql_connections: 100
tenants:
  db-a:
    mysql_connections: "110"
`)

	// Wait for reload
	time.Sleep(200 * time.Millisecond)
	close(stopCh)

	cfg := m.GetConfig()
	if cfg == nil {
		t.Fatal("config is nil")
	}
	if cfg.Defaults["mysql_connections"] != 100 {
		t.Errorf("expected 100, got %v", cfg.Defaults["mysql_connections"])
	}
}

func TestWatchLoop_Dir_InvalidFile(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)

	m := &ConfigManager{
		path:  dir,
		isDir: true,
	}
	if err := m.fullDirLoad(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}

	stopCh := make(chan struct{})
	go m.WatchLoop(50*time.Millisecond, stopCh)

	// Add an invalid YAML file — should not crash
	time.Sleep(100 * time.Millisecond)
	writeTestYAML(t, filepath.Join(dir, "bad.yaml"), `
invalid: [yaml
`)

	time.Sleep(200 * time.Millisecond)
	close(stopCh)

	// Should still have a valid config (partial load)
	cfg := m.GetConfig()
	if cfg == nil {
		t.Error("config should not be nil after bad file added")
	}
}

// ============================================================
// fullDirLoad error paths
// ============================================================

func TestFullDirLoad_EmptyDir(t *testing.T) {
	dir := t.TempDir()
	m := &ConfigManager{
		path:  dir,
		isDir: true,
	}
	err := m.fullDirLoad()
	if err == nil {
		t.Error("expected error for empty dir")
	}
}

func TestFullDirLoad_NonexistentDir(t *testing.T) {
	m := &ConfigManager{
		path:  "/nonexistent/path/does/not/exist",
		isDir: true,
	}
	err := m.fullDirLoad()
	if err == nil {
		t.Error("expected error for nonexistent dir")
	}
}

// ============================================================
// IncrementalLoad edge cases
// ============================================================

func TestIncrementalLoad_AddAndRemoveFiles(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)
	writeTestYAML(t, filepath.Join(dir, "db-a.yaml"), `
tenants:
  db-a:
    mysql_connections: "90"
`)

	m := &ConfigManager{
		path:  dir,
		isDir: true,
	}
	if err := m.fullDirLoad(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}

	// Verify initial state
	cfg := m.GetConfig()
	if _, ok := cfg.Tenants["db-a"]; !ok {
		t.Fatal("db-a should exist in initial config")
	}

	// Add a new tenant file
	writeTestYAML(t, filepath.Join(dir, "db-b.yaml"), `
tenants:
  db-b:
    mysql_connections: "100"
`)

	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("incremental load after add failed: %v", err)
	}

	cfg = m.GetConfig()
	if _, ok := cfg.Tenants["db-b"]; !ok {
		t.Error("db-b should exist after add")
	}

	// Remove db-a file
	os.Remove(filepath.Join(dir, "db-a.yaml"))

	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("incremental load after remove failed: %v", err)
	}

	cfg = m.GetConfig()
	if _, ok := cfg.Tenants["db-a"]; ok {
		t.Error("db-a should NOT exist after remove")
	}
	if _, ok := cfg.Tenants["db-b"]; !ok {
		t.Error("db-b should still exist after remove of db-a")
	}
}

// ============================================================
// IncrementalLoad specific paths
// ============================================================

func TestIncrementalLoad_SingleFileFallback(t *testing.T) {
	dir := t.TempDir()
	configFile := filepath.Join(dir, "config.yaml")
	writeTestYAML(t, configFile, `
defaults:
  mysql_connections: 80
tenants:
  db-a:
    mysql_connections: "90"
`)

	m := &ConfigManager{
		path:  configFile,
		isDir: false,
	}
	// IncrementalLoad on single-file mode falls back to Load()
	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad single-file fallback failed: %v", err)
	}
	cfg := m.GetConfig()
	if cfg == nil || cfg.Defaults["mysql_connections"] != 80 {
		t.Errorf("expected mysql_connections=80, got %v", cfg.Defaults)
	}
}

func TestIncrementalLoad_NoCacheFallsBackToFullLoad(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)

	m := &ConfigManager{
		path:  dir,
		isDir: true,
		// No fileHashes cache — first load
	}
	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad no-cache fallback failed: %v", err)
	}
	cfg := m.GetConfig()
	if cfg == nil || cfg.Defaults["mysql_connections"] != 80 {
		t.Errorf("expected mysql_connections=80, got %v", cfg.Defaults)
	}
}

func TestIncrementalLoad_NoChangeReturnsNil(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)

	m := &ConfigManager{
		path:  dir,
		isDir: true,
	}
	if err := m.fullDirLoad(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}

	// Second incremental load with no changes should be a no-op
	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad no-change should succeed: %v", err)
	}
}

// ============================================================
// WatchLoop — error in scan (dir deleted)
//
// TECH-DEBT-017: this test and TestWatchLoop_SingleFile_ErrorOnRead
// below use time.Sleep as a synchronisation primitive and are
// flake-prone under `go test -race` on Go 1.26 CI. Planned fix is
// m.Stop()+sync.WaitGroup so the watcher goroutine is guaranteed
// to have exited before os.Remove runs.
// ============================================================

func TestWatchLoop_Dir_ErrorOnScan(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)

	m := &ConfigManager{
		path:  dir,
		isDir: true,
	}
	if err := m.fullDirLoad(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}

	stopCh := make(chan struct{})
	go m.WatchLoop(50*time.Millisecond, stopCh)

	// Delete the dir to cause scan error
	time.Sleep(80 * time.Millisecond)
	os.RemoveAll(dir)
	time.Sleep(100 * time.Millisecond)

	close(stopCh)
	// Should not crash — just log warning
}

// ============================================================
// WatchLoop — single-file mode error (file deleted)
// ============================================================

func TestWatchLoop_SingleFile_ErrorOnRead(t *testing.T) {
	dir := t.TempDir()
	configFile := filepath.Join(dir, "config.yaml")
	writeTestYAML(t, configFile, `
defaults:
  mysql_connections: 80
`)

	m := &ConfigManager{
		path:  configFile,
		isDir: false,
	}
	if err := m.Load(); err != nil {
		t.Fatalf("initial load failed: %v", err)
	}

	stopCh := make(chan struct{})
	go m.WatchLoop(50*time.Millisecond, stopCh)

	// Delete the file to cause read error
	time.Sleep(80 * time.Millisecond)
	os.Remove(configFile)
	time.Sleep(100 * time.Millisecond)

	close(stopCh)
	// Should not crash
}

// ============================================================
// ConfigManager accessor tests
// ============================================================

func TestConfigManager_Mode(t *testing.T) {
	m := &ConfigManager{isDir: true}
	if m.Mode() != "directory" {
		t.Errorf("expected 'directory', got %q", m.Mode())
	}

	m2 := &ConfigManager{isDir: false}
	if m2.Mode() != "single-file" {
		t.Errorf("expected 'single-file', got %q", m2.Mode())
	}
}

func TestConfigManager_LastReload(t *testing.T) {
	m := &ConfigManager{}
	if !m.LastReload().IsZero() {
		t.Error("expected zero time for new manager")
	}
}

func TestConfigManager_IsLoaded(t *testing.T) {
	m := &ConfigManager{}
	if m.IsLoaded() {
		t.Error("expected not loaded for new manager")
	}

	m.loaded = true
	if !m.IsLoaded() {
		t.Error("expected loaded after setting flag")
	}
}

// ============================================================
// Helper
// ============================================================

func writeTestYAML(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatalf("failed to write %s: %v", path, err)
	}
}
