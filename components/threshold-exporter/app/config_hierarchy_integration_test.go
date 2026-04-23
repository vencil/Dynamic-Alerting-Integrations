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
	"runtime"
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

	// Wait up to 3s for the new hash to differ. CI runners under load occasionally
	// need >1s for the 20ms WatchLoop tick + 10ms debounce + fsync + diff to
	// complete; 3s is still bounded and well under the test-suite timeout.
	ok := waitFor(t, 3*time.Second, func() bool {
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

// TestConfigManager_DeletedTenantCleanup (A-8c, planning §12.2) locks the
// behavior contract that when a tenant file is removed from disk and a
// reload is triggered, every ConfigManager per-tenant map entry for that
// tenant is cleared **in the same atomic swap** — not only the visible
// `tenantSources` / `mergedHashes` maps but also the internal
// `hierarchyHashes` / `hierarchyMtimes` lookups and the
// `inheritanceGraph.TenantDefaults` chain entry.
//
// The test is behavior-lock; no product code changes are required — the
// atomic-swap at config_debounce.go L346-353 already clears all four maps.
// But A-10 (GitHub issue #52) surfaced that the WatchLoop → diffAndReload
// path has an empty window on slow CI runners; A-8c extends coverage to
// the deletion path so any future regression of the swap logic is caught
// before it ships.
//
// Also asserts no goroutine leak: the test fires an entire reload cycle
// then closes the manager; runtime.NumGoroutine() before/after should
// differ by at most a small epsilon accounting for the Go scheduler's
// per-test framework churn.
func TestConfigManager_DeletedTenantCleanup(t *testing.T) {
	dir := t.TempDir()
	// L0 defaults + three sibling tenants under team-a/.
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)
	teamDir := filepath.Join(dir, "team-a")
	if err := os.MkdirAll(teamDir, 0o755); err != nil {
		t.Fatalf("mkdir team-a: %v", err)
	}
	for _, tid := range []string{"tenant-a", "tenant-b", "tenant-c"} {
		writeTestYAML(t, filepath.Join(teamDir, tid+".yaml"),
			"tenants:\n  "+tid+":\n    mysql_connections: \"90\"\n")
	}

	// Snapshot goroutine count before manager spawns any background work.
	goBefore := runtime.NumGoroutine()

	m := NewConfigManagerWithDebounce(dir, 10*time.Millisecond)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}

	// Baseline invariant: all 3 tenants populated across every per-tenant map.
	m.mu.RLock()
	for _, tid := range []string{"tenant-a", "tenant-b", "tenant-c"} {
		if _, ok := m.tenantSources[tid]; !ok {
			t.Errorf("baseline: tenantSources missing %s", tid)
		}
		if _, ok := m.mergedHashes[tid]; !ok {
			t.Errorf("baseline: mergedHashes missing %s", tid)
		}
		if m.inheritanceGraph == nil || len(m.inheritanceGraph.TenantDefaults[tid]) == 0 {
			t.Errorf("baseline: inheritanceGraph.TenantDefaults missing %s", tid)
		}
	}
	m.mu.RUnlock()

	// Delete tenant-b's source file on disk and trigger a reload. Use
	// triggerDebouncedReload (not filesystem watcher) so the test is
	// deterministic — we're not measuring WatchLoop latency here, just
	// the state invariant after diffAndReload completes.
	bFile := filepath.Join(dir, "team-a", "tenant-b.yaml")
	if err := os.Remove(bFile); err != nil {
		t.Fatalf("remove tenant-b.yaml: %v", err)
	}
	m.triggerDebouncedReload(ReloadReasonDelete)

	// Wait for the reload to land; deletion is observable when tenantSources
	// no longer has tenant-b.
	ok := waitFor(t, 2*time.Second, func() bool {
		m.mu.RLock()
		defer m.mu.RUnlock()
		_, stillHere := m.tenantSources["tenant-b"]
		return !stillHere
	})
	if !ok {
		t.Fatalf("tenant-b still in tenantSources after 2s reload wait")
	}

	// Post-delete invariants — atomic swap must clear tenant-b from EVERY
	// per-tenant data structure. If any of these regress, a stale read
	// (e.g. in GET /api/v1/tenants/tenant-b/effective) will return data
	// for a tenant that no longer exists.
	m.mu.RLock()
	if _, stillHere := m.tenantSources["tenant-b"]; stillHere {
		t.Errorf("tenantSources still has tenant-b")
	}
	if _, stillHere := m.mergedHashes["tenant-b"]; stillHere {
		t.Errorf("mergedHashes still has tenant-b")
	}
	if _, stillHere := m.hierarchyHashes["team-a/tenant-b.yaml"]; stillHere {
		t.Errorf("hierarchyHashes still has team-a/tenant-b.yaml")
	}
	if _, stillHere := m.hierarchyMtimes["team-a/tenant-b.yaml"]; stillHere {
		t.Errorf("hierarchyMtimes still has team-a/tenant-b.yaml")
	}
	if m.inheritanceGraph == nil {
		t.Errorf("inheritanceGraph became nil after delete")
	} else if chain := m.inheritanceGraph.TenantDefaults["tenant-b"]; chain != nil {
		t.Errorf("inheritanceGraph.TenantDefaults[tenant-b] = %v, want nil", chain)
	}
	// Surviving tenants must NOT be collateral damage.
	for _, tid := range []string{"tenant-a", "tenant-c"} {
		if _, ok := m.tenantSources[tid]; !ok {
			t.Errorf("collateral damage: tenantSources missing surviving %s", tid)
		}
		if _, ok := m.mergedHashes[tid]; !ok {
			t.Errorf("collateral damage: mergedHashes missing surviving %s", tid)
		}
	}
	m.mu.RUnlock()

	// Goroutine leak check: close manager and give the scheduler a moment
	// to drain. Small epsilon tolerates test-runtime goroutines (pprof,
	// race detector, etc.) that aren't ours.
	m.Close()
	time.Sleep(50 * time.Millisecond)
	goAfter := runtime.NumGoroutine()
	if delta := goAfter - goBefore; delta > 2 {
		t.Errorf("goroutine leak suspected: before=%d after=%d (delta=%d > 2)",
			goBefore, goAfter, delta)
	}
}
