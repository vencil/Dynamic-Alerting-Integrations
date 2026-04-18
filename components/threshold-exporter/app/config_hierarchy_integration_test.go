package main

// ============================================================
// Phase 5 integration tests — hierarchy wired into Load path
// ============================================================
//
// These tests exercise the end-to-end pipeline:
//   1. fullDirLoad populates hierarchical state when _defaults.yaml exists
//   2. Resolve(tenantID) returns the correct merged config + hashes
//   3. WatchLoop → triggerDebouncedReload → diffAndReload updates state
//      atomically when files change
//
// They complement the pure-scanner tests in config_hierarchy_test.go and
// the pure-merge tests in config_inheritance_test.go by covering the
// integration seams that previously existed only in the §8.11.2 pre-plan.

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestFullDirLoad_PopulatesHierarchyState verifies that a fresh Load on
// a directory with _defaults.yaml populates tenantSources, mergedHashes,
// and inheritanceGraph — so the /effective handler works without needing
// a reload cycle first.
func TestFullDirLoad_PopulatesHierarchyState(t *testing.T) {
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	m := NewConfigManager(dir)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	m.mu.RLock()
	hierarchical := m.hierarchicalMode
	tenantCount := len(m.tenantSources)
	hashCount := len(m.mergedHashes)
	graph := m.inheritanceGraph
	m.mu.RUnlock()

	if !hierarchical {
		t.Errorf("hierarchicalMode should be true after Load with _defaults.yaml")
	}
	if tenantCount == 0 {
		t.Errorf("tenantSources empty after Load; expected tenant-a")
	}
	if hashCount == 0 {
		t.Errorf("mergedHashes empty after Load")
	}
	if graph == nil {
		t.Errorf("inheritanceGraph nil after Load")
	} else if chain := graph.TenantDefaults["tenant-a"]; len(chain) == 0 {
		t.Errorf("tenant-a should have at least one defaults in chain")
	}
}

// TestFullDirLoad_FlatMode_NoHierarchyActivation verifies that a flat
// conf.d (no _defaults.yaml anywhere) does NOT flip hierarchicalMode —
// we preserve the v2.6.0 fast path for legacy deployments.
func TestFullDirLoad_FlatMode_NoHierarchyActivation(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "tenant-only.yaml"), `
tenants:
  tenant-only:
    mysql_connections: "42"
`)

	m := NewConfigManager(dir)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	m.mu.RLock()
	hierarchical := m.hierarchicalMode
	m.mu.RUnlock()

	if hierarchical {
		t.Errorf("hierarchicalMode should stay false for flat conf.d")
	}
}

// TestResolve_ReturnsEffectiveConfig verifies the /effective happy path:
// the merged config contains both L0 defaults (inherited) and tenant
// override applied correctly.
func TestResolve_ReturnsEffectiveConfig(t *testing.T) {
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90") // tenant-a overrides mysql_connections=90

	m := NewConfigManager(dir)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	ec, ok := m.Resolve("tenant-a")
	if !ok {
		t.Fatalf("tenant-a not resolved")
	}
	if ec.TenantID != "tenant-a" {
		t.Errorf("TenantID = %q, want tenant-a", ec.TenantID)
	}
	if ec.SourceFile == "" {
		t.Errorf("SourceFile empty")
	}
	if len(ec.SourceHash) != 16 {
		t.Errorf("SourceHash length = %d, want 16 (hex[:16])", len(ec.SourceHash))
	}
	if len(ec.MergedHash) != 16 {
		t.Errorf("MergedHash length = %d, want 16 (hex[:16])", len(ec.MergedHash))
	}
	if len(ec.DefaultsChain) == 0 {
		t.Errorf("DefaultsChain empty; expected at least 1 (L0 _defaults.yaml)")
	}
	if ec.Config == nil {
		t.Errorf("Config nil")
	} else if got := ec.Config["mysql_connections"]; got != "90" {
		// Tenant override wins — the scalar "90" (string, because
		// ScheduledValue → string after parse) should be the merged value.
		// Note: the low-level computeEffectiveConfig produces raw YAML
		// values (yaml.v3 → map[string]any), where mysql_connections: "90"
		// parses as string "90".
		t.Errorf("mysql_connections = %v (%T), want \"90\"", got, got)
	}
}

// TestResolve_UnknownTenant_Returns404Signal verifies the (nil, false)
// shape used by the /effective handler to emit 404 Not Found.
func TestResolve_UnknownTenant_Returns404Signal(t *testing.T) {
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	m := NewConfigManager(dir)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	ec, ok := m.Resolve("nonexistent-tenant")
	if ok {
		t.Errorf("Resolve should return ok=false for unknown tenant, got ec=%v", ec)
	}
	if ec != nil {
		t.Errorf("ec should be nil for unknown tenant, got %v", ec)
	}
}

// TestWatchLoop_DebouncedReload_DetectsFileChange verifies the full
// WatchLoop → triggerDebouncedReload → diffAndReload → new mergedHash
// pipeline. Uses a 10ms debounce window and 20ms tick interval so the
// test runs in well under 1 second.
func TestWatchLoop_DebouncedReload_DetectsFileChange(t *testing.T) {
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	m := NewConfigManagerWithDebounce(dir, 10*time.Millisecond)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	m.mu.RLock()
	firstHash := m.mergedHashes["tenant-a"]
	m.mu.RUnlock()
	if firstHash == "" {
		t.Fatalf("expected first merged_hash to be populated by Load")
	}

	stopCh := make(chan struct{})
	done := make(chan struct{})
	go func() {
		m.WatchLoop(20*time.Millisecond, stopCh)
		close(done)
	}()

	// Change tenant file content.
	time.Sleep(30 * time.Millisecond) // wait a tick so WatchLoop baseline is armed
	writeTestYAML(t, filepath.Join(dir, "team-a", "tenant-a.yaml"), `
tenants:
  tenant-a:
    mysql_connections: "77"
`)
	// Bump mtime explicitly so the flat scanDirFileHashes composite hash
	// picks up the change past its 2s age guard.
	os.Chtimes(filepath.Join(dir, "team-a", "tenant-a.yaml"),
		time.Now().Add(-5*time.Second), time.Now().Add(-5*time.Second))

	// Wait up to 1s for the new hash to differ.
	ok := waitFor(t, 1*time.Second, func() bool {
		m.mu.RLock()
		defer m.mu.RUnlock()
		curr := m.mergedHashes["tenant-a"]
		return curr != "" && curr != firstHash
	})
	close(stopCh)
	<-done

	if !ok {
		m.mu.RLock()
		curr := m.mergedHashes["tenant-a"]
		m.mu.RUnlock()
		t.Errorf("merged_hash did not update via WatchLoop: first=%s current=%s", firstHash, curr)
	}
}
