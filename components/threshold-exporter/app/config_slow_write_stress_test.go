package main

// ============================================================
// B-7 — Slow-write torn-state stress (v2.8.0 Phase .b)
// ============================================================
//
// Validates `config_debounce.go` sliding-debounce semantics under a
// realistic ops scenario: an operator runs `git pull` against a repo
// that updates 50+ tenant files, where individual file rsync writes
// land with random inter-arrival jitter (a few ms to a few tens of
// ms apart). The contract is:
//
//   * While inter-arrival gaps stay < debounceWindow, the timer keeps
//     resetting (sliding window), and **no** intermediate fire
//     happens — i.e. there is never a torn-state reload that observes
//     half the writes.
//   * Once writes stop, the next debounce window elapses and exactly
//     ONE fire converges to the post-write merged_hash for every
//     tenant whose source moved.
//   * No goroutine leaks (Close() drains).
//
// Production parameters (planning §B-7): 200 files, 50-500ms gaps.
// Test parameters: scaled-down (50 files, 5-25ms gaps, 50ms window)
// so CI completes in ~2s but the ratio (gap < window) is preserved.
//
// Determinism: rand seeded with a fixed constant. Re-running the test
// produces identical inter-arrival patterns; flaky? File a real bug.
//
// This is NOT a benchmark — it asserts correctness, not performance.
// The reload-duration / debounce-batch histograms added in B-3 are
// observed as a side effect; we sanity-check them at the end.

import (
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// TestSlowWriteTornStateStress_FinalConvergenceNoTornFires writes 50
// tenant files with random 5-25ms inter-arrival gaps under a 50ms
// debounce window, and verifies:
//
//	(1) sliding debounce holds: fire count stays at 0 while writes are
//	    arriving (because each new write resets the timer).
//	(2) exactly one fire after writes stop.
//	(3) every mutated tenant's merged_hash differs from its baseline
//	    (final state correctly observed).
//	(4) the B-3 batch histogram observes one sample with sum == #fires
//	    seen by triggerDebouncedReload (proxy for coalescing efficacy).
func TestSlowWriteTornStateStress_FinalConvergenceNoTornFires(t *testing.T) {
	// Window/gap ratio chosen so even a CI runner overshooting a
	// `time.Sleep(gap)` by ~3× still keeps the inter-arrival under
	// debounceWin (max gap 25ms × 3 = 75ms < 100ms window). Tightening
	// gaps further saves no wall-clock time and chokes on scheduler
	// jitter; loosening the window beyond 100ms adds settle latency
	// without further margin.
	const (
		numFiles      = 50
		debounceWin   = 100 * time.Millisecond
		minGap        = 5 * time.Millisecond
		maxGap        = 25 * time.Millisecond
		settleTimeout = 2 * time.Second
		seed          = int64(0xB7_5_0_5_0)
	)

	fresh, _ := withIsolatedMetrics(t)
	dir := buildSlowWriteFixture(t, numFiles)

	m := NewConfigManagerWithDebounce(dir, debounceWin)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}

	// Snapshot baseline merged_hash for every tenant we plan to mutate.
	baseline := make(map[string]string, numFiles)
	m.mu.RLock()
	for tid, h := range m.mergedHashes {
		baseline[tid] = h
	}
	m.mu.RUnlock()
	if len(baseline) < numFiles {
		t.Fatalf("expected baseline ≥ %d tenants, got %d", numFiles, len(baseline))
	}

	rng := rand.New(rand.NewSource(seed))
	fireSnapshots := make([]uint64, 0, numFiles)

	// Drive the slow-write sequence. After EACH write we sample
	// DebounceFiredCount(); if the sliding window is honored, every
	// sample should be 0 — the timer resets faster than it can fire.
	for i := 0; i < numFiles; i++ {
		tid := fmt.Sprintf("tenant-%04d", i)
		path := filepath.Join(dir, "team-a", tid+".yaml")
		content := fmt.Sprintf("tenants:\n  %s:\n    mysql_connections: \"%d\"\n", tid, 100+i)
		if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
			t.Fatalf("write tenant %s: %v", tid, err)
		}
		// Synthetic fsnotify-equivalent: drive the trigger explicitly so
		// the test stays deterministic (real fsnotify timing is OS-
		// dependent and would defeat the seed-based reproducibility).
		m.triggerDebouncedReload(ReloadReasonSource)
		fireSnapshots = append(fireSnapshots, m.DebounceFiredCount())

		gap := minGap + time.Duration(rng.Int63n(int64(maxGap-minGap)))
		time.Sleep(gap)
	}

	// (1) Before settle: across ALL inter-write samples, fire count
	// MUST stay 0. Even one nonzero sample means a torn-state fire
	// observed an incomplete write batch.
	for i, c := range fireSnapshots {
		if c != 0 {
			t.Fatalf("sliding debounce violated: fire count = %d after write %d (expected 0 throughout the burst)", c, i+1)
		}
	}

	// (2) After settle: exactly one fire (window elapses post-final-write).
	if !waitFor(t, settleTimeout, func() bool {
		return m.DebounceFiredCount() >= 1
	}) {
		t.Fatalf("debounce never fired after settle (count=%d)", m.DebounceFiredCount())
	}
	// Tail-sleep so any rogue extra fire is also visible.
	time.Sleep(2 * debounceWin)
	if got := m.DebounceFiredCount(); got != 1 {
		t.Errorf("expected exactly 1 debounce fire after settle, got %d", got)
	}

	// (3) Final convergence: every mutated tenant's merged_hash moved.
	m.mu.RLock()
	defer m.mu.RUnlock()
	mismatches := 0
	for tid, prev := range baseline {
		curr := m.mergedHashes[tid]
		if curr == "" {
			t.Errorf("tenant %s: missing merged_hash post-reload", tid)
			mismatches++
			continue
		}
		if curr == prev {
			t.Errorf("tenant %s: merged_hash unchanged (baseline=%s); slow-write batch lost a tenant", tid, prev)
			mismatches++
		}
	}
	if mismatches > 0 {
		t.Fatalf("slow-write convergence: %d/%d tenants did not advance", mismatches, len(baseline))
	}

	// (4) B-3 sanity: exactly one debounce-batch sample with sum equal
	// to numFiles (every triggerDebouncedReload during the burst).
	reg := prometheus.NewRegistry()
	if err := reg.Register(fresh.debounceBatch); err != nil {
		t.Fatalf("register debounceBatch: %v", err)
	}
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, fam := range families {
		for _, metric := range fam.Metric {
			h := metric.Histogram
			if h.GetSampleCount() != 1 {
				t.Errorf("debounceBatch: expected _count=1 (one fired window), got %d", h.GetSampleCount())
			}
			if h.GetSampleSum() != float64(numFiles) {
				t.Errorf("debounceBatch: expected _sum=%d (all triggers coalesced), got %v", numFiles, h.GetSampleSum())
			}
		}
	}
}

// buildSlowWriteFixture creates a minimal hierarchy with `numFiles`
// tenant YAMLs under <dir>/team-a/, each with a baseline mysql_connections
// override. Returns the fixture root.
func buildSlowWriteFixture(t *testing.T, numFiles int) string {
	t.Helper()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)
	teamDir := filepath.Join(dir, "team-a")
	if err := os.MkdirAll(teamDir, 0o755); err != nil {
		t.Fatalf("mkdir team-a: %v", err)
	}
	for i := 0; i < numFiles; i++ {
		tid := fmt.Sprintf("tenant-%04d", i)
		// Baseline value: 50 (different from the post-write 100+i so
		// every tenant's merged_hash necessarily moves).
		content := fmt.Sprintf("tenants:\n  %s:\n    mysql_connections: \"50\"\n", tid)
		writeTestYAML(t, filepath.Join(teamDir, tid+".yaml"), content)
	}
	return dir
}
