package main

// Regression for #2122: two overlapping reloads must not leave the service
// on an older config than the hierarchy plane.
//
// installNewHierarchyState writes the manager in two m.mu windows (the
// hierarchy plane, then the service config via commitFlatFrom). Before
// reloadMu, fireDebounced ran diffAndReload with no lock, so a second fire
// could land between R1's windows: hierW → hierB → flatB → flatW. The
// collector then served W while hierarchy.hashes said B, and detectChange —
// which compares the tree against hierarchy.hashes — reported "no change"
// forever, so the manager never healed.
//
// The test parks R1 in that gap with SetAfterHierarchyInstallForTest, writes
// B and starts R2, then releases R1. With reloadMu R2 queues behind R1 and
// runs last; without it R2 runs to completion inside R1's gap and R1's flat
// commit then overwrites B with W.
//
// The oracle is a separate manager Load()ing its own copy of the B tree: it
// does not share the commit path under test, so a regression in that path
// cannot also move the expected value.

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const (
	serializeTenantID = "tenant-alpha"
	serializeKey      = "mysql_connections"
	serializeTimeout  = 10 * time.Second
	// r2Grace bounds how long the test waits for R2 to finish while R1 is
	// parked. Serialised, R2 cannot finish (it is queued on reloadMu) and
	// this only costs wall time. Unserialised, R2 on this two-file tree
	// completes in milliseconds; if a starved machine ever needed longer,
	// R1 would be released early and the test could pass on a broken
	// build — never fail on a correct one.
	r2Grace = 300 * time.Millisecond
)

func serializeTenantYAML(value string) string {
	return fmt.Sprintf("tenants:\n  %s:\n    %s: %q\n", serializeTenantID, serializeKey, value)
}

// writeSerializeTree writes a hierarchical tree (a root _defaults.yaml turns
// hierarchical mode on) whose single tenant carries `value`. Returns the
// tenant file's path.
func writeSerializeTree(t *testing.T, dir, value string) string {
	t.Helper()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
	tenantPath := filepath.Join(dir, serializeTenantID+".yaml")
	writeTestYAML(t, tenantPath, serializeTenantYAML(value))
	return tenantPath
}

type serializeOracle struct {
	configHash string
	mergedHash string
}

// loadSerializeOracle builds the expected state from an independent manager
// loading its own copy of the tree with `value`.
func loadSerializeOracle(t *testing.T, value string) serializeOracle {
	t.Helper()
	dir := t.TempDir()
	writeSerializeTree(t, dir, value)
	fresh, _ := freshMetrics(t)
	var buf bytes.Buffer
	ref := NewConfigManagerWithDebounce(dir, 0)
	ref.SetMetrics(fresh)
	ref.SetLogger(log.New(&buf, "", 0))
	t.Cleanup(ref.Close)
	if err := ref.Load(); err != nil {
		t.Fatalf("oracle Load(): %v", err)
	}
	if got := ref.GetConfig().Tenants[serializeTenantID][serializeKey].Default; got != value {
		t.Fatalf("oracle setup: served %q, want %q", got, value)
	}
	ref.mu.RLock()
	merged := ref.hierarchy.mergedHashes[serializeTenantID]
	ref.mu.RUnlock()
	if merged == "" {
		t.Fatalf("oracle setup: no merged_hash for %s — hierarchical mode did not engage", serializeTenantID)
	}
	return serializeOracle{configHash: ref.Identity().ConfigHash, mergedHash: merged}
}

func waitClosed(t *testing.T, ch <-chan struct{}, what string) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(serializeTimeout):
		t.Fatalf("timed out after %s waiting for %s", serializeTimeout, what)
	}
}

// TestReloadSerialize_OverlappingReloadsConvergeOnLatestTree runs the
// interleaving through each outermost reload entry that takes reloadMu.
func TestReloadSerialize_OverlappingReloadsConvergeOnLatestTree(t *testing.T) {
	t.Parallel()
	const (
		valueInitial = "10"
		valueW       = "20" // R1's tree
		valueB       = "30" // R2's tree — the one on disk at the end
	)

	entries := []struct {
		name   string
		window time.Duration
		reload func(m *ConfigManager)
	}{
		{
			// Production path: the debounce timer's callback. The window is
			// irrelevant because the test calls the callback directly.
			name:   "fireDebounced",
			window: time.Hour,
			reload: func(m *ConfigManager) { m.fireDebounced() },
		},
		{
			name:   "triggerDebouncedReload window<=0",
			window: 0,
			reload: func(m *ConfigManager) { m.triggerDebouncedReload(ReloadReasonForced) },
		},
	}

	for _, e := range entries {
		t.Run(e.name, func(t *testing.T) {
			t.Parallel()
			want := loadSerializeOracle(t, valueB)

			dir := t.TempDir()
			tenantPath := writeSerializeTree(t, dir, valueInitial)
			fresh, _ := freshMetrics(t)
			var logBuf bytes.Buffer
			m := NewConfigManagerWithDebounce(dir, e.window)
			m.SetMetrics(fresh)
			m.SetLogger(log.New(&logBuf, "", 0))
			t.Cleanup(m.Close)
			if err := m.Load(); err != nil {
				t.Fatalf("Load(): %v", err)
			}

			// Park the FIRST reload between its two install windows.
			var hookCalls atomic.Int32
			paused := make(chan struct{})
			release := make(chan struct{})
			var releaseOnce sync.Once
			releaseR1 := func() { releaseOnce.Do(func() { close(release) }) }
			// Never leave R1 parked if the test bails out early.
			t.Cleanup(releaseR1)
			m.SetAfterHierarchyInstallForTest(func() {
				if hookCalls.Add(1) == 1 {
					close(paused)
					<-release
				}
			})

			writeTestYAML(t, tenantPath, serializeTenantYAML(valueW))
			r1Done := make(chan struct{})
			go func() { defer close(r1Done); e.reload(m) }()
			waitClosed(t, paused, "R1 to reach the gap between its install windows")

			// R1 has installed hierW and not yet flatW. Put B on disk and
			// start R2 in that gap.
			writeTestYAML(t, tenantPath, serializeTenantYAML(valueB))
			r2Done := make(chan struct{})
			go func() { defer close(r2Done); e.reload(m) }()

			r2Overtook := false
			select {
			case <-r2Done:
				r2Overtook = true // only possible when reloads are not serialised
			case <-time.After(r2Grace):
			}
			releaseR1()
			waitClosed(t, r1Done, "R1 to finish")
			waitClosed(t, r2Done, "R2 to finish")

			// 1. Service content is B.
			if got := m.GetConfig().Tenants[serializeTenantID][serializeKey].Default; got != valueB {
				t.Errorf("service serves %s=%q, want %q (latest tree); R2 finished while R1 was parked: %v "+
					"— an overlapping reload's flat commit overwrote the newer one (#2122)",
					serializeKey, got, valueB, r2Overtook)
			}
			// 2. Identity reports B's hash, per the independent oracle.
			if got := m.Identity().ConfigHash; got != want.configHash {
				t.Errorf("Identity().ConfigHash = %s, want %s (independent Load of the B tree)", got, want.configHash)
			}
			// 3. The hierarchy plane agrees with the service: B's file hash
			// (computed here from the bytes, not read from the manager) and
			// B's merged_hash per the oracle.
			sum := sha256.Sum256([]byte(serializeTenantYAML(valueB)))
			wantFileHash := hex.EncodeToString(sum[:])
			m.mu.RLock()
			gotFileHash := m.hierarchy.hashes[filepath.Clean(tenantPath)]
			gotMerged := m.hierarchy.mergedHashes[serializeTenantID]
			m.mu.RUnlock()
			if gotFileHash != wantFileHash {
				t.Errorf("hierarchy.hashes[tenant] = %q, want sha256(B) %q", gotFileHash, wantFileHash)
			}
			if gotMerged != want.mergedHash {
				t.Errorf("hierarchy.mergedHashes[%s] = %q, want %q (oracle)", serializeTenantID, gotMerged, want.mergedHash)
			}
			// 4. detectChange says "nothing to do" — and after 1–3 that
			// verdict is sound, because service, hierarchy and tree all say B.
			// (In the #2122 state it said the same thing while serving W,
			// which is why the stale state never healed.)
			changed, _, err := m.detectChange()
			if err != nil {
				t.Fatalf("detectChange: %v", err)
			}
			if changed {
				t.Errorf("detectChange() = changed after both reloads; the tree was not modified again")
			}
			if n := hookCalls.Load(); n != 2 {
				t.Errorf("hook fired %d times, want 2 (one per hierarchical reload)", n)
			}
		})
	}
}
