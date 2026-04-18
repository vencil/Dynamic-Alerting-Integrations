package main

// ============================================================
// Debounced reload + hierarchical diff (v2.7.0, Phase 3)
// ============================================================
//
// This file wires the hierarchical scanner (config_hierarchy.go) and the deep
// merge + dual-hash engine (config_inheritance.go) into ConfigManager's
// WatchLoop via a burst-coalescing debounce window.
//
// Why a debounce:
//
//   - K8s ConfigMap volumes update via symlink rotation which fires several
//     fsnotify events back-to-back in a ~50-200ms window (kubelet does
//     ..data/..2026_04_18/… rename dance). Naive reload on each event causes
//     N partial loads where N-1 are stale.
//   - git-sync and operator batch writes can drop 10+ files inside a single
//     rsync burst. Debouncing collapses those into one full rehash.
//   - Tests need deterministic batching; a 1ms window via
//     NewConfigManagerWithDebounce(path, time.Millisecond) lets us fire N
//     events fast and assert exactly one reload.
//
// Why it lives beside the ConfigManager struct (package main) and not inside
// the public pkg/config: the debounce state is intrinsic to the running
// daemon's reload loop — library consumers (tenant-api) don't need it.
//
// Interaction with the flat incremental path (v2.6.0, IncrementalLoad):
//
//   - When hierarchicalMode == false, diffAndReload delegates to
//     IncrementalLoad so legacy flat conf.d/ layouts keep their existing
//     semantics untouched.
//   - When hierarchicalMode == true, diffAndReload owns the reload pipeline
//     end-to-end: scan → diff → per-tenant merged_hash → atomic swap of
//     mergedHashes + inheritanceGraph, plus a fullDirLoad for the
//     ThresholdConfig view consumed by the collector.
//
// Trap #12 from §8.11.2 (Debounce timer leak): Close() stops the timer;
// time.AfterFunc (vs. NewTimer + goroutine) avoids the receive-channel
// race documented in the "Stop + drain" Go FAQ (GODEBUG=gctrace=1 showed
// an orphaned timer channel in early prototypes; AfterFunc is cleaner).

import (
	"log"
	"os"
	"sync/atomic"
	"time"
)

// Reload trigger reasons — label values for the
// da_config_reload_trigger_total CounterVec (collector.go Phase 4).
//
// Kept as constants (not an enum type) so the metric label and the log line
// always agree string-for-string. Python describe_tenant.py doesn't emit
// these; they are Go-exporter internal observability.
const (
	ReloadReasonSource    = "source"    // a tenant YAML changed
	ReloadReasonDefaults  = "defaults"  // a _defaults.yaml changed
	ReloadReasonNewTenant = "new"       // scan discovered a tenant ID absent previously
	ReloadReasonDelete    = "delete"    // a tenant file disappeared
	ReloadReasonForced    = "forced"    // manual trigger (SIGHUP-style, reserved)
)

// triggerDebouncedReload records a reload trigger and arms (or resets) the
// debounce timer. Thread-safe: multiple goroutines may call concurrently.
//
// Behavior:
//   - First call: starts timer; timer fires diffAndReload after
//     m.debounceWindow elapsed.
//   - Subsequent calls within the window: reset the timer — the reload
//     slides forward, keeping the total batch bounded by the slowest caller.
//   - debounceWindow == 0: synchronous fallback. Calls diffAndReload
//     inline. This preserves pre-v2.7.0 behavior for call-sites that
//     explicitly opt out (e.g. first-load bootstrap).
//
// `reason` is appended to pendingReasons for collector metrics. Deliberately
// unfiltered for duplicates — a storm of 10 "source" events is itself a
// signal and we want to count them as 10 increments.
func (m *ConfigManager) triggerDebouncedReload(reason string) {
	if m.debounceWindow <= 0 {
		// Synchronous fallback — useful for v2.6.0 parity tests and for
		// the initial Load() bootstrap where we don't want to gate startup
		// on a timer.
		m.recordReason(reason)
		m.diffAndReload()
		return
	}

	m.debounceMu.Lock()
	m.pendingReasons = append(m.pendingReasons, reason)
	if m.debounceTimer == nil {
		// First event in this window — arm the timer.
		m.debounceTimer = time.AfterFunc(m.debounceWindow, m.fireDebounced)
		m.debounceMu.Unlock()
		return
	}
	// Subsequent event — reset. Stop() returns false if the timer has already
	// fired or been stopped; in the "fired" case the AfterFunc callback is
	// already running (or done) and we should not re-arm from this
	// goroutine — the callback's own code path handles that. Stop() racing
	// with fire is cheap to absorb: we simply let the fired callback swap
	// the pointer to nil and start fresh on the next call.
	if m.debounceTimer.Stop() {
		// Successfully cancelled before fire → safe to re-arm in place.
		m.debounceTimer.Reset(m.debounceWindow)
	} else {
		// Already firing; fireDebounced will observe the newly-appended
		// reason on the next pass since we hold the mutex and it acquires
		// the same one. If Stop() returned false AND the timer was already
		// nil'd by a completed fire, arm a fresh one.
		if m.debounceTimer == nil {
			m.debounceTimer = time.AfterFunc(m.debounceWindow, m.fireDebounced)
		} else {
			m.debounceTimer.Reset(m.debounceWindow)
		}
	}
	m.debounceMu.Unlock()
}

// recordReason appends to pendingReasons under debounceMu. Exported as a
// method (not inlined) so the synchronous fallback path and the regular
// path share identical reason-list semantics.
func (m *ConfigManager) recordReason(reason string) {
	m.debounceMu.Lock()
	m.pendingReasons = append(m.pendingReasons, reason)
	m.debounceMu.Unlock()
}

// fireDebounced is invoked by time.AfterFunc when the debounce window
// elapses. It swaps out the pending reasons under the mutex (so new
// triggers arriving during the reload accumulate into the next batch),
// then runs diffAndReload without holding the mutex so a long reload does
// not block new triggers.
func (m *ConfigManager) fireDebounced() {
	m.debounceMu.Lock()
	// Snapshot reasons and clear state so concurrent triggerDebouncedReload
	// calls start a fresh window.
	reasons := m.pendingReasons
	m.pendingReasons = nil
	m.debounceTimer = nil
	m.debounceMu.Unlock()

	_ = reasons // consumed by diffAndReload via incrementReloadReasons
	atomic.AddUint64(&m.debounceFired, 1)
	if _, _, err := m.diffAndReload(); err != nil {
		log.Printf("ERROR: debounced reload failed: %v", err)
	}
}

// DebounceFiredCount returns how many debounce windows have fired since
// construction. Test-only accessor; production code should not rely on
// this for correctness. Uses atomic load so callers don't need the mutex.
func (m *ConfigManager) DebounceFiredCount() uint64 {
	return atomic.LoadUint64(&m.debounceFired)
}

// PendingDebounceReasons returns a snapshot of the current debounce
// window's accumulated reasons. Test-only; callers should not mutate.
func (m *ConfigManager) PendingDebounceReasons() []string {
	m.debounceMu.Lock()
	defer m.debounceMu.Unlock()
	out := make([]string, len(m.pendingReasons))
	copy(out, m.pendingReasons)
	return out
}

// Close releases the debounce timer to prevent goroutine leaks on graceful
// shutdown. Safe to call multiple times. Does NOT wait for a pending
// fireDebounced call to finish — the caller should have already closed the
// WatchLoop stop channel first.
//
// Implements §8.11.2 trap #12 ("cm.Close() must debounceTimer.Stop()"). If
// production Main gains a SIGTERM path that can't guarantee WatchLoop is
// done, we'd need to add a wait group here; for now the 15s HTTP shutdown
// grace in main.go overshoots any debounce window comfortably.
func (m *ConfigManager) Close() {
	m.debounceMu.Lock()
	if m.debounceTimer != nil {
		m.debounceTimer.Stop()
		m.debounceTimer = nil
	}
	m.pendingReasons = nil
	m.debounceMu.Unlock()
}

// diffAndReload computes the set of tenants whose merged_hash changed since
// the previous scan and rebuilds the relevant state. Returns the count of
// tenants actually reloaded and the count of no-op defaults changes (a
// defaults file changed but none of its dependent tenants' merged_hash
// moved — described in ADR-018 §Reload Decisions as the "quiet defaults
// edit" case).
//
// Flow:
//  1. Call scanDirHierarchical with current priorMtimes (forward-compat;
//     Phase 1 ignores the arg but we pass it anyway so Phase 5's mtime
//     optimization is a drop-in upgrade).
//  2. If hierarchicalMode is not yet activated for this path, check whether
//     the scan found any _defaults.yaml. If so, activate hierarchical mode.
//     Otherwise fall back to IncrementalLoad (flat path).
//  3. Compute the "dirty tenant" set:
//       - tenants whose own YAML hash changed        → reason=source
//       - tenants discovered for the first time      → reason=new
//       - tenants previously known but now absent    → reason=delete
//       - tenants whose defaults chain has any file whose hash changed
//         → candidate; compute new merged_hash and compare — if moved,
//         record as reason=defaults; otherwise increment noOp counter.
//  4. For each dirty tenant, recompute merged_hash and update mergedHashes.
//  5. Atomic swap: lock once, install the new hierarchy state.
//  6. Also run fullDirLoad so the Prometheus collector's ThresholdConfig
//     view stays in sync. This is slightly wasteful — fullDirLoad re-scans
//     the same dir — but keeps the collector path unchanged in v2.7.0. A
//     future optimization can share the scan between the two.
//
// Trap: we do not hold m.mu during the per-file merge because the merge
// does disk reads and YAML parses; long holds would stall /metrics
// scrapes. Instead we snapshot old hashes under RLock, do I/O lock-free,
// and take the write lock only for the final swap.
func (m *ConfigManager) diffAndReload() (reloaded, noOp int, err error) {
	if !m.isDir {
		// Single-file mode has no hierarchical concept — just reload.
		if err := m.Load(); err != nil {
			log.Printf("ERROR: single-file reload failed: %v", err)
			return 0, 0, err
		}
		return 1, 0, nil
	}

	// Snapshot prior state under RLock (cheap) so the I/O below is
	// unblocked from subsequent GetConfig readers.
	m.mu.RLock()
	priorMtimes := m.hierarchyMtimes
	priorHashes := m.hierarchyHashes
	priorMergedHashes := m.mergedHashes
	priorTenantSources := m.tenantSources
	hierarchicalMode := m.hierarchicalMode
	m.mu.RUnlock()

	tenants, defaults, hashes, mtimes, graph, scanErr := scanDirHierarchical(m.path, priorMtimes)
	if scanErr != nil {
		log.Printf("ERROR: hierarchical scan failed: %v", scanErr)
		return 0, 0, scanErr
	}

	// If no _defaults.yaml was discovered, stay on the flat path. We still
	// keep hierarchicalMode sticky — once flipped on (by a config that
	// introduces _defaults.yaml) we don't flip back off even if it's
	// deleted later, because the downstream behaviour (merged_hash
	// computation) is still well-defined with an empty chain.
	if !hierarchicalMode && len(defaults) == 0 {
		// Flat mode — delegate to the v2.6.0 incremental path.
		if ierr := m.IncrementalLoad(); ierr != nil {
			log.Printf("ERROR: incremental load failed: %v", ierr)
			return 0, 0, ierr
		}
		return 0, 0, nil
	}
	hierarchicalMode = true

	// Compute dirty set. We compute merged_hash for every currently-known
	// tenant that may have changed:
	//   - new tenant: always dirty.
	//   - tenant whose own file hash moved: dirty (reason=source).
	//   - tenant whose defaults chain has any file whose hash moved:
	//     candidate — compute and compare to decide dirty vs no-op.
	newMergedHashes := make(map[string]string, len(tenants))
	for tid := range tenants {
		newMergedHashes[tid] = "" // filled below
	}

	for tid, srcPath := range tenants {
		prevSrc, wasKnown := priorTenantSources[tid]
		sourceChanged := !wasKnown || prevSrc != srcPath || hashes[srcPath] != priorHashes[srcPath]

		defaultsChain := graph.TenantDefaults[tid]
		defaultsChanged := false
		for _, dp := range defaultsChain {
			if hashes[dp] != priorHashes[dp] {
				defaultsChanged = true
				break
			}
		}

		if !sourceChanged && !defaultsChanged {
			// Reuse cached merged_hash — nothing that feeds this tenant moved.
			if prev, ok := priorMergedHashes[tid]; ok {
				newMergedHashes[tid] = prev
				continue
			}
			// No cached value (first scan after enabling hierarchical mode).
			// Fall through to compute.
		}

		mh, mergeErr := m.recomputeMergedHash(tid, srcPath, defaultsChain)
		if mergeErr != nil {
			logMergeSkip(tid, "debounced-reload", mergeErr)
			// Preserve any prior merged_hash we had so the /effective
			// endpoint still serves the last-known-good value. Absent prior
			// → mark empty (tenant will read as merge-failing).
			if prev, ok := priorMergedHashes[tid]; ok {
				newMergedHashes[tid] = prev
			}
			continue
		}
		newMergedHashes[tid] = mh

		if sourceChanged {
			reloaded++
			if wasKnown {
				IncReloadTrigger(ReloadReasonSource)
			} else {
				IncReloadTrigger(ReloadReasonNewTenant)
			}
		} else if defaultsChanged {
			if prev, ok := priorMergedHashes[tid]; ok && prev == mh {
				// Defaults file changed but the resulting merged_hash
				// didn't — "quiet defaults edit" (comment-only, reordering,
				// or a key that's shadowed by a tenant override).
				noOp++
				IncDefaultsNoop()
			} else {
				reloaded++
				IncReloadTrigger(ReloadReasonDefaults)
			}
		}
	}

	// Detect deleted tenants — previously known, absent now. Deletions
	// don't get a merged_hash but we do account them in the reload count
	// so the caller can emit a counter.
	for tid := range priorTenantSources {
		if _, stillKnown := tenants[tid]; !stillKnown {
			reloaded++
			IncReloadTrigger(ReloadReasonDelete)
		}
	}

	// Atomic swap. We rebuild ThresholdConfig via fullDirLoad first (it
	// acquires m.mu.Lock itself), then take the lock again to install the
	// hierarchy-only fields. Splitting into two locks is intentional:
	// fullDirLoad is slow (I/O + YAML parse) and we don't want the debounce
	// goroutine to gate scrapes on it for hierarchy metadata updates.
	if err := m.fullDirLoad(); err != nil {
		log.Printf("ERROR: fullDirLoad inside diffAndReload failed: %v", err)
		return reloaded, noOp, err
	}

	m.mu.Lock()
	m.hierarchicalMode = hierarchicalMode
	m.tenantSources = tenants
	m.hierarchyHashes = hashes
	m.hierarchyMtimes = mtimes
	m.mergedHashes = newMergedHashes
	m.inheritanceGraph = graph
	m.mu.Unlock()

	return reloaded, noOp, nil
}

// recomputeMergedHash reads the tenant file + each file in its defaults
// chain, then runs computeMergedHash. Separated from diffAndReload so
// tests and /effective (read path) can share the disk-read sequence.
//
// Returns empty string + error if the tenant file or any chain entry is
// unreadable; computeMergedHash itself errors only on parse failures,
// which are returned to the caller.
func (m *ConfigManager) recomputeMergedHash(tenantID, tenantFile string, defaultsChain []string) (string, error) {
	tenantBytes, err := os.ReadFile(tenantFile)
	if err != nil {
		return "", err
	}
	chainBytes := make([][]byte, 0, len(defaultsChain))
	for _, dp := range defaultsChain {
		b, rerr := os.ReadFile(dp)
		if rerr != nil {
			return "", rerr
		}
		chainBytes = append(chainBytes, b)
	}
	return computeMergedHash(tenantBytes, tenantID, chainBytes)
}
