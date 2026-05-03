package main

// ============================================================
// B-7 — Slow-write torn-state stress (v2.8.0 Phase .b)
// ============================================================
//
// Validates `config_debounce.go` sliding-debounce semantics under a
// realistic ops scenario: an operator runs `git pull` against a repo
// that updates 50+ tenant files, where individual file rsync writes
// land with random inter-arrival jitter (a few ms to a few tens of
// ms apart). The contract under test:
//
//   * Every triggerDebouncedReload coalesces into SOME fired window
//     (no lost triggers). The B-3 batch histogram's `_sum` equals
//     numFiles regardless of how many windows fired.
//   * Every mutated tenant's merged_hash advances past its baseline
//     post-quiescence (no torn-state observation that loses a tenant's
//     final value).
//   * No goroutine leaks (Close() drains).
//
// Production parameters (planning §B-7): 200 files, 50-500ms gaps.
// Test parameters: scaled-down (50 files, 5-25ms gaps, 100ms window)
// so CI completes in ~2s but the ratio (gap < window) is preserved.
//
// Determinism: rand seeded with a fixed constant. Re-running the test
// produces identical inter-arrival patterns; observed-state is checked
// post-quiescence so wall-clock scheduler jitter doesn't flake.
//
// **History (issue #157)**: this test originally asserted "fire count
// stays at 0 throughout the burst" + "exactly 1 fire after settle".
// Both are wall-clock claims — they test scheduler timing, not the
// debounce contract. On loaded CI runners the burst occasionally split
// into 2 windows, failing the "exactly 0 / exactly 1" assertions even
// though final convergence held. PRs #151 + #155 each hit this flake;
// codified rewrite per testing-playbook §"Race-flake battles" lesson §2
// (canonical quiescence pattern). The new invariants are stable across
// runner contention while still catching the genuine bug class
// (lost-trigger / lost-write).

import (
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// waitForQuiescence polls counterFn until its observed value is
// unchanged for stableWindow consecutive ms, or until deadline.
// Returns true on quiescence, false on timeout.
//
// Use this for "wait for async work to settle" patterns where the
// alternative — `time.Sleep(window + buffer)` — flakes under loaded
// CI runners. See `docs/internal/testing-playbook.md` §v2.8.0 Lessons
// Learned — Race-flake battles, lesson §2 for the pattern rationale.
//
// Caller pitfall: a counter that NEVER moves (e.g. broken trigger,
// dead system) also satisfies "unchanged for stableWindow" and the
// function returns true. Callers MUST follow up with a domain-level
// "did the expected work happen" check (e.g. `fireCount > 0`) to
// distinguish "settled at the right value" from "settled at zero
// because nothing fired".
func waitForQuiescence(
	t *testing.T,
	deadline, stableWindow time.Duration,
	counterFn func() uint64,
) bool {
	t.Helper()
	last := counterFn()
	stableSince := time.Now()
	timeoutAt := time.Now().Add(deadline)
	pollInterval := 20 * time.Millisecond
	for time.Now().Before(timeoutAt) {
		time.Sleep(pollInterval)
		now := counterFn()
		if now != last {
			last = now
			stableSince = time.Now()
			continue
		}
		if time.Since(stableSince) >= stableWindow {
			return true
		}
	}
	return false
}

// TestSlowWriteTornStateStress_FinalConvergence writes 50 tenant files
// with random 5-25ms inter-arrival gaps under a 100ms debounce window
// and verifies the post-quiescence contract:
//
//	(1) Every triggerDebouncedReload coalesced into some fired window —
//	    debounceBatch histogram _sum == numFiles. Catches the
//	    "lost-trigger" bug class directly.
//	(2) Every mutated tenant's merged_hash advanced past its baseline
//	    after quiescence. Catches the "lost-write" bug class (a fire
//	    that observed an incomplete state and didn't get re-driven).
//	(3) Final fire count is logged for observability — informational,
//	    NOT asserted exact (1 = sliding window held throughout;
//	    2+ = burst legitimately split across windows under load,
//	    which doesn't violate any contract as long as 1 + 2 hold).
//	(4) No goroutine leak: Close() drains.
//
// Test name retains `TornStateStress` for git-blame continuity even
// though the rewrite no longer hard-asserts "no torn fires" — see
// the file-level comment for the issue #157 history.
func TestSlowWriteTornStateStress_FinalConvergence(t *testing.T) {
	const (
		numFiles      = 50
		debounceWin   = 100 * time.Millisecond
		minGap        = 5 * time.Millisecond
		maxGap        = 25 * time.Millisecond
		settleTimeout = 5 * time.Second
		// stableWindow: how long DebounceFiredCount() must be unchanged
		// before we declare the system quiescent. Set to ~2.5× debounceWin
		// so a freshly-trigger'd window still has time to fire+settle
		// before we sample.
		stableWindow = 250 * time.Millisecond
		seed         = int64(0xB7_5_0_5_0)
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
	for tid, h := range m.hierarchy.mergedHashes {
		baseline[tid] = h
	}
	m.mu.RUnlock()
	if len(baseline) < numFiles {
		t.Fatalf("expected baseline ≥ %d tenants, got %d", numFiles, len(baseline))
	}

	rng := rand.New(rand.NewSource(seed))

	// Drive the slow-write sequence. We do NOT sample DebounceFiredCount()
	// during the burst — that introduces wall-clock-timing-fragile
	// assertions (testing-playbook §Race-flake lesson §2). Post-burst
	// quiescence detection + per-fire invariants below catch the genuine
	// bug classes without flaking on runner contention.
	for i := 0; i < numFiles; i++ {
		tid := fmt.Sprintf("tenant-%04d", i)
		path := filepath.Join(dir, "team-a", tid+".yaml")
		content := fmt.Sprintf("tenants:\n  %s:\n    mysql_connections: \"%d\"\n", tid, 100+i)
		if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
			t.Fatalf("write tenant %s: %v", tid, err)
		}
		// Synthetic fsnotify-equivalent: drive the trigger explicitly so
		// the test stays deterministic across OS fsnotify implementations.
		m.triggerDebouncedReload(ReloadReasonSource)

		gap := minGap + time.Duration(rng.Int63n(int64(maxGap-minGap)))
		time.Sleep(gap)
	}

	// Quiescence: wait for DebounceFiredCount() to stabilize, indicating
	// no further fires are pending. This is the canonical replacement
	// for "sleep + assert exactly N" per testing-playbook §Race-flake
	// lesson §2.
	if !waitForQuiescence(t, settleTimeout, stableWindow, m.DebounceFiredCount) {
		t.Fatalf("DebounceFiredCount never stabilized within %v "+
			"(last value: %d) — debounce may be misbehaving",
			settleTimeout, m.DebounceFiredCount())
	}
	fireCount := m.DebounceFiredCount()
	if fireCount < 1 {
		t.Fatalf("expected at least 1 fire after settle, got %d", fireCount)
	}

	// (3) Fire-count observability — informational only.
	t.Logf("debounce fires after quiescence: %d "+
		"(1 = sliding window held throughout the burst; "+
		"2+ = burst legitimately split across windows under runner load — "+
		"both states are contract-compliant as long as invariants below pass)",
		fireCount)

	// (2) Final convergence: every mutated tenant's merged_hash advanced.
	//     Catches the "lost-write" bug class — a fire that observes an
	//     incomplete state but doesn't get re-driven would leave at least
	//     one tenant's hash matching baseline after settle.
	m.mu.RLock()
	mismatches := 0
	for tid, prev := range baseline {
		curr := m.hierarchy.mergedHashes[tid]
		if curr == "" {
			t.Errorf("tenant %s: missing merged_hash post-reload", tid)
			mismatches++
			continue
		}
		if curr == prev {
			t.Errorf("tenant %s: merged_hash unchanged (baseline=%s); "+
				"slow-write batch lost a tenant", tid, prev)
			mismatches++
		}
	}
	m.mu.RUnlock()
	if mismatches > 0 {
		t.Fatalf("slow-write convergence: %d/%d tenants did not advance",
			mismatches, len(baseline))
	}

	// (1) Batch histogram: every trigger coalesced into SOME fire.
	//     `_sum` MUST equal numFiles regardless of how many windows
	//     fired (contract-stable assertion). `_count` is intentionally
	//     NOT asserted exact — it can legitimately be 1 or 2 under
	//     loaded CI; both states are compliant per the rewrite rationale
	//     (see file header for issue #157 history). If `_count > 2` we
	//     do flag — that's outside the realistic CI-jitter envelope and
	//     suggests debounce is genuinely misbehaving.
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
			if h.GetSampleSum() != float64(numFiles) {
				t.Errorf("debounceBatch: expected _sum=%d (every trigger "+
					"coalesced into some fire), got %v",
					numFiles, h.GetSampleSum())
			}
			if h.GetSampleCount() < 1 {
				t.Errorf("debounceBatch: expected at least 1 fired window, got %d",
					h.GetSampleCount())
			}
			if h.GetSampleCount() > 2 {
				t.Errorf("debounceBatch: _count=%d exceeds runner-jitter envelope (≤2); "+
					"debounce may be misbehaving (sliding window not coalescing)",
					h.GetSampleCount())
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
