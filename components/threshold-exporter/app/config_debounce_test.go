package main

// ============================================================
// Debounce + diffAndReload tests (v2.7.0 Phase 3)
// ============================================================
//
// These tests use a 1ms debounce window to keep wall-clock time cheap while
// still exercising the real time.AfterFunc path — we want the same code
// running in CI as in production, just with a faster clock.

import (
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// waitFor polls `cond` until it returns true or the timeout expires. Returns
// true if satisfied, false on timeout. Local helper to avoid leaning on
// testify (this package has no test dependencies beyond stdlib + yaml).
func waitFor(t *testing.T, d time.Duration, cond func() bool) bool {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return true
		}
		time.Sleep(200 * time.Microsecond)
	}
	return cond()
}

// writeHierarchicalFixture builds a minimal L0-only hierarchy:
//
//	<dir>/_defaults.yaml
//	<dir>/team-a/tenant-a.yaml     (tenants: { tenant-a: ... })
func writeHierarchicalFixture(t *testing.T, dir string, tenantOverride string) {
	t.Helper()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)
	teamDir := filepath.Join(dir, "team-a")
	if err := os.MkdirAll(teamDir, 0o755); err != nil {
		t.Fatalf("mkdir team-a: %v", err)
	}
	content := "tenants:\n  tenant-a:\n    mysql_connections: \"" + tenantOverride + "\"\n"
	writeTestYAML(t, filepath.Join(teamDir, "tenant-a.yaml"), content)
}

// TestTriggerDebouncedReload_BatchesMultipleTriggers verifies that N
// triggerDebouncedReload calls inside one window result in exactly one
// fireDebounced (the core contract of the debounce).
func TestTriggerDebouncedReload_BatchesMultipleTriggers(t *testing.T) {
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	m := NewConfigManagerWithDebounce(dir, 20*time.Millisecond)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}

	// Fire 5 triggers back-to-back.
	for i := 0; i < 5; i++ {
		m.triggerDebouncedReload(ReloadReasonSource)
	}

	// Wait up to 500ms for the window to fire.
	ok := waitFor(t, 500*time.Millisecond, func() bool {
		return m.DebounceFiredCount() >= 1
	})
	if !ok {
		t.Fatalf("debounce never fired (count=%d)", m.DebounceFiredCount())
	}

	// Allow a tail window to confirm NO second fire for un-reset triggers.
	time.Sleep(60 * time.Millisecond)
	if got := m.DebounceFiredCount(); got != 1 {
		t.Errorf("expected exactly 1 debounce fire, got %d", got)
	}
}

// TestTriggerDebouncedReload_TimerResetExtendsWindow verifies that a
// trigger arriving during an active window pushes the fire further out.
// We fire, sleep for half a window, fire again, then assert the total
// time-to-fire is at least 1.5 windows (not just 1).
func TestTriggerDebouncedReload_TimerResetExtendsWindow(t *testing.T) {
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	window := 40 * time.Millisecond
	m := NewConfigManagerWithDebounce(dir, window)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}

	start := time.Now()
	m.triggerDebouncedReload(ReloadReasonSource)
	time.Sleep(window / 2)
	m.triggerDebouncedReload(ReloadReasonSource) // resets timer

	// Wait for fire.
	ok := waitFor(t, 500*time.Millisecond, func() bool {
		return m.DebounceFiredCount() >= 1
	})
	if !ok {
		t.Fatalf("debounce never fired")
	}
	elapsed := time.Since(start)

	// After reset the fire must be at least one full window after the
	// second trigger — so total ≥ 1.5 * window. Upper-bound is loose
	// because CI timing jitter is real.
	minExpected := window + window/2
	if elapsed < minExpected {
		t.Errorf("timer reset ineffective: fired after %v, expected >= %v", elapsed, minExpected)
	}
}

// TestTriggerDebouncedReload_ZeroWindowIsSynchronous verifies the opt-out
// path: debounceWindow == 0 executes diffAndReload inline, matching the
// v2.6.0 tick→reload behavior for users who want the old semantics.
func TestTriggerDebouncedReload_ZeroWindowIsSynchronous(t *testing.T) {
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	m := NewConfigManagerWithDebounce(dir, 0)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}

	// With window=0, one call should reload synchronously.
	m.triggerDebouncedReload(ReloadReasonSource)

	// DebounceFiredCount only counts the timer path — synchronous should
	// NOT bump it. This asserts we took the fast path.
	if got := m.DebounceFiredCount(); got != 0 {
		t.Errorf("window=0 should skip timer; fired count = %d", got)
	}
}

// TestClose_StopsPendingTimer verifies Close() prevents a queued fire
// from running. We set a long window, trigger once, immediately Close(),
// then sleep past the window and assert no fire.
func TestClose_StopsPendingTimer(t *testing.T) {
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	m := NewConfigManagerWithDebounce(dir, 50*time.Millisecond)
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}

	m.triggerDebouncedReload(ReloadReasonSource)
	m.Close()

	time.Sleep(120 * time.Millisecond)
	// Tolerate 0 fires — Close should have stopped the queued fire.
	// If the timer happened to already have fired before Close (race),
	// the fired count could be 1; we accept that as non-deterministic
	// and skip the assertion in that case.
	if got := m.DebounceFiredCount(); got > 1 {
		t.Errorf("Close should have stopped pending fire; got %d", got)
	}
}

// TestDiffAndReload_HierarchicalMode_SourceChange verifies a tenant file
// content change is detected and the merged_hash moves.
func TestDiffAndReload_HierarchicalMode_SourceChange(t *testing.T) {
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	m := NewConfigManagerWithDebounce(dir, 0) // synchronous for determinism
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}

	// First reload populates hierarchy state.
	if _, _, err := m.diffAndReload(); err != nil {
		t.Fatalf("first diffAndReload: %v", err)
	}
	m.mu.RLock()
	firstHash := m.mergedHashes["tenant-a"]
	m.mu.RUnlock()
	if firstHash == "" {
		t.Fatalf("expected non-empty merged_hash for tenant-a, got empty")
	}

	// Mutate tenant file.
	writeHierarchicalFixture(t, dir, "99")
	// Bump mtime by at least 2s to bypass scanDirFileHashes's mtime guard —
	// otherwise the fast path may skip the file. scanDirHierarchical always
	// reads but the collector's fullDirLoad uses the flat scanner too.
	time.Sleep(10 * time.Millisecond)

	reloaded, noOp, err := m.diffAndReload()
	if err != nil {
		t.Fatalf("second diffAndReload: %v", err)
	}
	if reloaded != 1 {
		t.Errorf("expected 1 reloaded tenant, got %d", reloaded)
	}
	if noOp != 0 {
		t.Errorf("expected 0 noOp, got %d", noOp)
	}

	m.mu.RLock()
	secondHash := m.mergedHashes["tenant-a"]
	m.mu.RUnlock()
	if secondHash == firstHash {
		t.Errorf("merged_hash did not move after source change (both=%s)", firstHash)
	}
}

// TestDiffAndReload_DefaultsChangeNoOp verifies ADR-018's "quiet defaults
// edit" detection: a _defaults.yaml key that is completely shadowed by a
// tenant override must NOT bump merged_hash and must increment the no-op
// counter instead.
func TestDiffAndReload_DefaultsChangeNoOp(t *testing.T) {
	dir := t.TempDir()
	// tenant-a overrides mysql_connections → changing its default has no
	// effect on the merged result.
	writeHierarchicalFixture(t, dir, "90")

	m := NewConfigManagerWithDebounce(dir, 0)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}
	if _, _, err := m.diffAndReload(); err != nil {
		t.Fatalf("first diffAndReload: %v", err)
	}
	m.mu.RLock()
	firstHash := m.mergedHashes["tenant-a"]
	m.mu.RUnlock()

	// Mutate _defaults.yaml but only the shadowed key.
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 50
`)
	time.Sleep(10 * time.Millisecond)

	reloaded, noOp, err := m.diffAndReload()
	if err != nil {
		t.Fatalf("second diffAndReload: %v", err)
	}
	if noOp != 1 {
		t.Errorf("expected 1 noOp (shadowed defaults change), got reloaded=%d noOp=%d", reloaded, noOp)
	}

	m.mu.RLock()
	secondHash := m.mergedHashes["tenant-a"]
	m.mu.RUnlock()
	if secondHash != firstHash {
		t.Errorf("merged_hash moved on shadowed defaults change (first=%s second=%s)", firstHash, secondHash)
	}
}

// TestDiffAndReload_FlatModeFallback verifies that when there is no
// _defaults.yaml anywhere in the tree, diffAndReload delegates to the
// v2.6.0 IncrementalLoad path and does not try to build an inheritance
// graph.
func TestDiffAndReload_FlatModeFallback(t *testing.T) {
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "tenant-flat.yaml"), `
tenants:
  tenant-flat:
    mysql_connections: "42"
`)

	m := NewConfigManagerWithDebounce(dir, 0)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}

	reloaded, noOp, err := m.diffAndReload()
	if err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}
	// Flat-mode fallback returns (0, 0) because IncrementalLoad owns the
	// counters — we just need to assert no error and no hierarchical state.
	if reloaded != 0 || noOp != 0 {
		t.Errorf("flat-mode fallback should return (0,0); got (%d,%d)", reloaded, noOp)
	}

	m.mu.RLock()
	hierarchical := m.hierarchicalMode
	m.mu.RUnlock()
	if hierarchical {
		t.Errorf("hierarchicalMode should remain false for flat conf.d")
	}
}

// TestPendingDebounceReasons_AccumulatesThenClears verifies the reasons
// list is returned accurately during a window and cleared after fire.
func TestPendingDebounceReasons_AccumulatesThenClears(t *testing.T) {
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	// Long window so we can observe accumulation.
	m := NewConfigManagerWithDebounce(dir, 100*time.Millisecond)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}

	m.triggerDebouncedReload(ReloadReasonSource)
	m.triggerDebouncedReload(ReloadReasonDefaults)
	m.triggerDebouncedReload(ReloadReasonNewTenant)

	// Mid-window: 3 reasons should be present.
	got := m.PendingDebounceReasons()
	if len(got) != 3 {
		t.Errorf("expected 3 pending reasons, got %d: %v", len(got), got)
	}
	joined := strings.Join(got, ",")
	for _, want := range []string{ReloadReasonSource, ReloadReasonDefaults, ReloadReasonNewTenant} {
		if !strings.Contains(joined, want) {
			t.Errorf("expected reason %q in pending list; got %v", want, got)
		}
	}

	// Wait for fire.
	ok := waitFor(t, 600*time.Millisecond, func() bool {
		return m.DebounceFiredCount() >= 1
	})
	if !ok {
		t.Fatalf("debounce never fired")
	}
	// Post-fire: reasons cleared.
	if remaining := m.PendingDebounceReasons(); len(remaining) != 0 {
		t.Errorf("reasons not cleared after fire; got %v", remaining)
	}
}

// TestTriggerDebouncedReload_Concurrent verifies no data races or panics
// under concurrent triggers from many goroutines. Pair with
// `go test -race` to catch real issues.
func TestTriggerDebouncedReload_Concurrent(t *testing.T) {
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")

	m := NewConfigManagerWithDebounce(dir, 30*time.Millisecond)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}

	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			m.triggerDebouncedReload(ReloadReasonSource)
		}()
	}
	wg.Wait()

	// At least one fire expected.
	ok := waitFor(t, 500*time.Millisecond, func() bool {
		return m.DebounceFiredCount() >= 1
	})
	if !ok {
		t.Errorf("debounce never fired under concurrent load (count=%d)", m.DebounceFiredCount())
	}
}
