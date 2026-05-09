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
	hierarchical := m.hierarchy.enabled
	tenantCount := len(m.hierarchy.tenantSources)
	hashCount := len(m.hierarchy.mergedHashes)
	graph := m.hierarchy.graph
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
	hierarchical := m.hierarchy.enabled
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

// TestWatchLoop_DebouncedReload_DetectsFileChange verifies the
// detect-change → trigger debounced reload → diffAndReload → new
// mergedHash pipeline.
//
// HA-11 (PR #325 follow-up): the previous version of this test was the
// canonical entry in flaky-tests.yaml — it spun up WatchLoop in a
// goroutine with a real 20ms ticker + 10ms debounce window, then polled
// with a 3s deadline. CI runner load occasionally pushed the
// tick + debounce + fsync + diff above 3s, producing a flake that ate
// ~1h of release toil in v2.7.0 PR #26.
//
// The fix is architectural, not retry-based: route the test through
// `tickOnce()` (extracted from WatchLoop's body) with a synchronous
// debounce window (`NewConfigManagerWithDebounce(dir, 0)` makes
// triggerDebouncedReload call diffAndReload inline). No goroutine, no
// ticker, no time.Sleep, no waitFor — the test asserts deterministically
// after a single tickOnce() call that the hash changed.
//
// The flat-scan/hierarchical-scan + debounce + atomic-swap pipeline is
// still exercised end-to-end; only the timing primitives are replaced
// with synchronous calls. Goroutine + ticker behavior is the WatchLoop
// orchestration layer's concern, covered separately in tests that don't
// need to assert content propagation.
func TestWatchLoop_DebouncedReload_DetectsFileChange(t *testing.T) {
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	// debounce=0 makes triggerDebouncedReload synchronous (see
	// config_debounce.go L82-92): the inline diffAndReload completes
	// before triggerDebouncedReload returns. Combined with manual
	// tickOnce() calls, the entire detect→reload pipeline is
	// observable from a single synchronous test goroutine.
	m := NewConfigManagerWithDebounce(dir, 0)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	m.mu.RLock()
	firstHash := m.hierarchy.mergedHashes["tenant-a"]
	m.mu.RUnlock()
	if firstHash == "" {
		t.Fatalf("expected first merged_hash to be populated by Load")
	}

	// Establish baseline: first tickOnce after Load should be a no-op
	// (nothing has changed since Load populated the hierarchy maps).
	m.tickOnce()

	// Change tenant file content. detectChange uses scanDirHierarchical
	// content hashes (not mtime), so we don't need mtime-tricks even on
	// fast filesystems.
	writeTestYAML(t, filepath.Join(dir, "team-a", "tenant-a.yaml"), `
tenants:
  tenant-a:
    mysql_connections: "77"
`)

	// Drive the polling cycle synchronously: detect → trigger →
	// diffAndReload (inline because debounce==0) → atomic swap.
	m.tickOnce()

	m.mu.RLock()
	curr := m.hierarchy.mergedHashes["tenant-a"]
	m.mu.RUnlock()
	if curr == "" {
		t.Errorf("merged_hash unset after tickOnce; first=%s", firstHash)
	}
	if curr == firstHash {
		t.Errorf("merged_hash did not update: first=%s current=%s", firstHash, curr)
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
		if _, ok := m.hierarchy.tenantSources[tid]; !ok {
			t.Errorf("baseline: tenantSources missing %s", tid)
		}
		if _, ok := m.hierarchy.mergedHashes[tid]; !ok {
			t.Errorf("baseline: mergedHashes missing %s", tid)
		}
		if m.hierarchy.graph == nil || len(m.hierarchy.graph.TenantDefaults[tid]) == 0 {
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
		_, stillHere := m.hierarchy.tenantSources["tenant-b"]
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
	if _, stillHere := m.hierarchy.tenantSources["tenant-b"]; stillHere {
		t.Errorf("tenantSources still has tenant-b")
	}
	if _, stillHere := m.hierarchy.mergedHashes["tenant-b"]; stillHere {
		t.Errorf("mergedHashes still has tenant-b")
	}
	if _, stillHere := m.hierarchy.hashes["team-a/tenant-b.yaml"]; stillHere {
		t.Errorf("hierarchyHashes still has team-a/tenant-b.yaml")
	}
	if _, stillHere := m.hierarchy.mtimes["team-a/tenant-b.yaml"]; stillHere {
		t.Errorf("hierarchyMtimes still has team-a/tenant-b.yaml")
	}
	if m.hierarchy.graph == nil {
		t.Errorf("inheritanceGraph became nil after delete")
	} else if chain := m.hierarchy.graph.TenantDefaults["tenant-b"]; chain != nil {
		t.Errorf("inheritanceGraph.TenantDefaults[tenant-b] = %v, want nil", chain)
	}
	// Surviving tenants must NOT be collateral damage.
	for _, tid := range []string{"tenant-a", "tenant-c"} {
		if _, ok := m.hierarchy.tenantSources[tid]; !ok {
			t.Errorf("collateral damage: tenantSources missing surviving %s", tid)
		}
		if _, ok := m.hierarchy.mergedHashes[tid]; !ok {
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
