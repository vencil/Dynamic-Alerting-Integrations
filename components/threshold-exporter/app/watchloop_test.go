package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/jonboulle/clockwork"
	"github.com/vencil/threshold-exporter/internal/testutil"
)

// startWatchLoopWithFakeClock spins up m.WatchLoop on a fresh
// clockwork.FakeClock and returns the clock + a stop function. The
// stop function closes stopCh and waits for the WatchLoop goroutine
// to exit, eliminating the goroutine-leak risk that the previous
// time.Sleep-based pattern had.
//
// Caller pattern (mirrors clockwork test idiom):
//
//	fakeClock, stop := startWatchLoopWithFakeClock(t, m, 50*time.Millisecond)
//	defer stop()
//	fakeClock.Advance(50 * time.Millisecond) // fire one tick
//	fakeClock.BlockUntil(1)                  // wait until tickOnce returns + ticker re-arms
//	// assert side effect via state poll, not via time.Sleep
//
// Replaces the TRK-217 sleep-as-sync pattern (#4c-F).
func startWatchLoopWithFakeClock(t *testing.T, m *ConfigManager, interval time.Duration) (*clockwork.FakeClock, func()) {
	t.Helper()
	fakeClock := clockwork.NewFakeClock()
	m.SetClock(fakeClock)

	stopCh := make(chan struct{})
	done := make(chan struct{})
	go func() {
		m.WatchLoop(interval, stopCh)
		close(done)
	}()

	// Wait for WatchLoop's NewTicker to register a sleeper before any
	// Advance call lands; otherwise Advance happens before the ticker
	// exists and the next tick is delayed by `interval` of fake-time
	// rather than firing immediately.
	_ = fakeClock.BlockUntilContext(context.Background(), 1)

	stop := func() {
		close(stopCh)
		// WatchLoop is parked on <-ticker.Chan(); fire one tick so the
		// select races with <-stopCh and exits. Without this, close(done)
		// never lands and the test deadlocks (or t.Cleanup races).
		fakeClock.Advance(interval)
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			t.Error("WatchLoop did not exit within 2s after stopCh close")
		}
	}
	return fakeClock, stop
}

// ============================================================
// WatchLoop Tests
// ============================================================

func TestWatchLoop_StopChannel(t *testing.T) {
	t.Parallel()
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

	// FakeClock + helper handles deterministic ticker registration
	// AND clean goroutine shutdown. The previous time.Sleep(100ms)
	// was non-deterministic flake bait (TRK-217).
	fakeClock, stop := startWatchLoopWithFakeClock(t, m, 50*time.Millisecond)

	// Fire one tick so the loop is observably alive before we stop it.
	fakeClock.Advance(50 * time.Millisecond)
	_ = fakeClock.BlockUntilContext(context.Background(), 1) // wait for tickOnce → ticker re-arm

	stop() // closes stopCh + asserts WatchLoop exited within 2s
}

func TestWatchLoop_DetectsFileChange(t *testing.T) {
	t.Parallel()
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

	fakeClock, stop := startWatchLoopWithFakeClock(t, m, 50*time.Millisecond)
	defer stop()

	// Update file BEFORE firing the tick so detectChange sees it on
	// the very first tickOnce call.
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 95
`)

	// Fire one tick → tickOnce → detectChange (true) →
	// triggerDebouncedReload (sync mode since debounce.window=0 on
	// struct-literal manager) → diffAndReload → cfg committed.
	fakeClock.Advance(50 * time.Millisecond)

	// State-poll for the reload to commit. Polling on observable
	// state (not on time) is the deterministic substitute for the
	// previous time.Sleep(200ms).
	if !waitFor(t, 2*time.Second, func() bool {
		c := m.GetConfig()
		return c != nil && c.Defaults["mysql_connections"] == 95
	}) {
		t.Errorf("expected mysql_connections=95 after reload, got %v", m.GetConfig().Defaults["mysql_connections"])
	}
}

func TestWatchLoop_SingleFileMode(t *testing.T) {
	t.Parallel()
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

	fakeClock, stop := startWatchLoopWithFakeClock(t, m, 50*time.Millisecond)
	defer stop()

	writeTestYAML(t, configFile, `
defaults:
  mysql_connections: 100
tenants:
  db-a:
    mysql_connections: "110"
`)

	// Single-file mode: tickOnce takes the loadFile branch directly
	// (no debounce involvement).
	fakeClock.Advance(50 * time.Millisecond)

	if !waitFor(t, 2*time.Second, func() bool {
		c := m.GetConfig()
		return c != nil && c.Defaults["mysql_connections"] == 100
	}) {
		t.Errorf("expected mysql_connections=100 after reload, got %v", m.GetConfig().Defaults["mysql_connections"])
	}
}

func TestWatchLoop_Dir_InvalidFile(t *testing.T) {
	t.Parallel()
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

	fakeClock, stop := startWatchLoopWithFakeClock(t, m, 50*time.Millisecond)
	defer stop()

	// Add an invalid YAML file — should not crash.
	writeTestYAML(t, filepath.Join(dir, "bad.yaml"), `
invalid: [yaml
`)

	// Fire one tick and wait for the iteration to complete (ticker
	// re-arms once tickOnce returns), proving WatchLoop survived the
	// bad file without panicking.
	fakeClock.Advance(50 * time.Millisecond)
	_ = fakeClock.BlockUntilContext(context.Background(), 1)

	// Should still have a valid config (partial load).
	if cfg := m.GetConfig(); cfg == nil {
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
// TRK-217 (CLOSED in #4c-F): both this test and
// TestWatchLoop_SingleFile_ErrorOnRead below previously used
// time.Sleep as a synchronisation primitive and were flake-prone
// under `go test -race` on Go 1.26 CI. Now uses
// startWatchLoopWithFakeClock + Advance + BlockUntil for
// deterministic tick fires + clean goroutine shutdown.
// ============================================================

func TestWatchLoop_Dir_ErrorOnScan(t *testing.T) {
	t.Parallel()
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

	fakeClock, stop := startWatchLoopWithFakeClock(t, m, 50*time.Millisecond)
	defer stop()

	// Delete the dir to cause scan error.
	os.RemoveAll(dir)

	// Fire one tick → tickOnce sees the missing dir → logs warn →
	// returns cleanly. BlockUntil proves we made it through the
	// iteration without panic.
	fakeClock.Advance(50 * time.Millisecond)
	_ = fakeClock.BlockUntilContext(context.Background(), 1)
	// Should not crash — just log warning. (No assertion; survival = pass.)
}

// ============================================================
// WatchLoop — single-file mode error (file deleted)
// ============================================================

func TestWatchLoop_SingleFile_ErrorOnRead(t *testing.T) {
	t.Parallel()
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

	fakeClock, stop := startWatchLoopWithFakeClock(t, m, 50*time.Millisecond)
	defer stop()

	// Delete the file to cause read error.
	os.Remove(configFile)

	// Fire one tick; tickOnce hits the loadFile error branch and
	// returns. Survival of the BlockUntil = no panic = pass.
	fakeClock.Advance(50 * time.Millisecond)
	_ = fakeClock.BlockUntilContext(context.Background(), 1)
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

// writeTestYAML wraps testutil.WriteFilePathMode with the package's
// 0600 mode convention, keeping call sites 2-arg.
func writeTestYAML(t *testing.T, path, content string) {
	t.Helper()
	testutil.WriteFilePathMode(t, path, content, 0600)
}
