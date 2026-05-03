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
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
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
//     m.debounce.window elapsed.
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
	if m.debounce.window <= 0 {
		// Synchronous fallback — useful for v2.6.0 parity tests and for
		// the initial Load() bootstrap where we don't want to gate startup
		// on a timer. Observe reload duration so callers using the
		// zero-window opt-out still feed the SLO histogram (B-3).
		m.recordReason(reason)
		t0 := time.Now()
		m.diffAndReload()
		ObserveReloadDuration(time.Since(t0))
		return
	}

	m.debounce.mu.Lock()
	m.debounce.pendingReasons = append(m.debounce.pendingReasons, reason)
	if m.debounce.timer == nil {
		// First event in this window — arm the timer.
		m.debounce.timer = time.AfterFunc(m.debounce.window, m.fireDebounced)
		m.debounce.mu.Unlock()
		return
	}
	// Subsequent event — reset. Stop() returns false if the timer has already
	// fired or been stopped; in the "fired" case the AfterFunc callback is
	// already running (or done) and we should not re-arm from this
	// goroutine — the callback's own code path handles that. Stop() racing
	// with fire is cheap to absorb: we simply let the fired callback swap
	// the pointer to nil and start fresh on the next call.
	if m.debounce.timer.Stop() {
		// Successfully cancelled before fire → safe to re-arm in place.
		m.debounce.timer.Reset(m.debounce.window)
	} else {
		// Already firing; fireDebounced will observe the newly-appended
		// reason on the next pass since we hold the mutex and it acquires
		// the same one. If Stop() returned false AND the timer was already
		// nil'd by a completed fire, arm a fresh one.
		if m.debounce.timer == nil {
			m.debounce.timer = time.AfterFunc(m.debounce.window, m.fireDebounced)
		} else {
			m.debounce.timer.Reset(m.debounce.window)
		}
	}
	m.debounce.mu.Unlock()
}

// recordReason appends to pendingReasons under debounceMu. Exported as a
// method (not inlined) so the synchronous fallback path and the regular
// path share identical reason-list semantics.
func (m *ConfigManager) recordReason(reason string) {
	m.debounce.mu.Lock()
	m.debounce.pendingReasons = append(m.debounce.pendingReasons, reason)
	m.debounce.mu.Unlock()
}

// fireDebounced is invoked by time.AfterFunc when the debounce window
// elapses. It swaps out the pending reasons under the mutex (so new
// triggers arriving during the reload accumulate into the next batch),
// then runs diffAndReload without holding the mutex so a long reload does
// not block new triggers.
func (m *ConfigManager) fireDebounced() {
	m.debounce.mu.Lock()
	// Snapshot reasons and clear state so concurrent triggerDebouncedReload
	// calls start a fresh window.
	reasons := m.debounce.pendingReasons
	m.debounce.pendingReasons = nil
	m.debounce.timer = nil
	m.debounce.mu.Unlock()

	// v2.8.0 B-3: observe debounce batch size (effectiveness signal)
	// before the reload so the sample lands even if diffAndReload
	// errors out. Sample count == fire count by construction.
	ObserveDebounceBatch(len(reasons))
	atomic.AddUint64(&m.debounce.fired, 1)
	t0 := time.Now()
	_, _, err := m.diffAndReload()
	ObserveReloadDuration(time.Since(t0))
	if err != nil {
		log.Printf("ERROR: debounced reload failed: %v", err)
	}
}

// DebounceFiredCount returns how many debounce windows have fired since
// construction. Test-only accessor; production code should not rely on
// this for correctness. Uses atomic load so callers don't need the mutex.
func (m *ConfigManager) DebounceFiredCount() uint64 {
	return atomic.LoadUint64(&m.debounce.fired)
}

// PendingDebounceReasons returns a snapshot of the current debounce
// window's accumulated reasons. Test-only; callers should not mutate.
func (m *ConfigManager) PendingDebounceReasons() []string {
	m.debounce.mu.Lock()
	defer m.debounce.mu.Unlock()
	out := make([]string, len(m.debounce.pendingReasons))
	copy(out, m.debounce.pendingReasons)
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
	m.debounce.mu.Lock()
	if m.debounce.timer != nil {
		m.debounce.timer.Stop()
		m.debounce.timer = nil
	}
	m.debounce.pendingReasons = nil
	m.debounce.mu.Unlock()
}

// reloadPriorState bundles every m.* hierarchy field captured under
// RLock at the start of diffAndReload. Snapshotting up-front lets the
// I/O below run lock-free so /metrics scrapes (which take RLock via
// GetConfig) aren't blocked by YAML parses or disk reads.
//
// v2.8.0 PR-3: extracted from the original 216-line diffAndReload to
// give the snapshot/scan/classify/install seams readable names.
type reloadPriorState struct {
	mtimes           map[string]fileStat
	hashes           map[string]string
	mergedHashes     map[string]string
	tenantSources    map[string]string
	parsedDefaults   map[string]map[string]any // Issue #61: shadow-vs-cosmetic baseline
	hierarchicalMode bool
}

// reloadScanState bundles the result of scanDirHierarchical when the
// scan committed to the hierarchical path. When the scan resolves to
// the flat path, scanAndCheckHierarchical returns fallback=true and
// the caller short-circuits without populating this struct.
type reloadScanState struct {
	tenants  map[string]string
	defaults map[string]bool
	hashes   map[string]string
	mtimes   map[string]fileStat
	graph    *InheritanceGraph
}

// reloadResult bundles classifyAndCount's output for installNewHierarchyState
// and the diffAndReload return value. blast-radius histogram observations
// are emitted inside classifyAndCount before it returns; this struct only
// carries the state the install step needs to atom-swap.
type reloadResult struct {
	newMergedHashes   map[string]string
	newParsedDefaults map[string]map[string]any
	reloaded          int
	noOp              int
}

// snapshotPriorState reads every m.* field that diffAndReload needs into
// a local struct under RLock, then releases the lock so subsequent disk
// I/O + YAML parses run unblocked from /metrics scrapers.
//
// Trap codified in the original v2.7.0 diffAndReload header: never hold
// m.mu across recomputeMergedHash — long holds stall scrapes.
func (m *ConfigManager) snapshotPriorState() reloadPriorState {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return reloadPriorState{
		mtimes:           m.hierarchy.mtimes,
		hashes:           m.hierarchy.hashes,
		mergedHashes:     m.hierarchy.mergedHashes,
		tenantSources:    m.hierarchy.tenantSources,
		parsedDefaults:   m.hierarchy.parsedDefaults, // Issue #61
		hierarchicalMode: m.hierarchy.enabled,
	}
}

// scanAndCheckHierarchical runs scanDirHierarchical and decides the path:
//
//	hierarchical → return scan, fallback=false; caller continues
//	flat         → IncrementalLoad here, return fallback=true; caller short-circuits
//	error        → return fallback=true + err; caller propagates
//
// hierarchicalMode is sticky once activated: a config that introduces
// `_defaults.yaml` flips the bit ON, and even if the file is later deleted
// we keep using the hierarchical path because computeMergedHash with an
// empty chain is well-defined.
func (m *ConfigManager) scanAndCheckHierarchical(prior reloadPriorState) (reloadScanState, bool, error) {
	tenants, defaults, hashes, mtimes, graph, scanErr := scanDirHierarchical(m.path, prior.mtimes)
	if scanErr != nil {
		log.Printf("ERROR: hierarchical scan failed: %v", scanErr)
		return reloadScanState{}, true, scanErr
	}

	// If no _defaults.yaml was discovered AND we haven't activated
	// hierarchical mode yet, stay on the flat path.
	if !prior.hierarchicalMode && len(defaults) == 0 {
		if ierr := m.IncrementalLoad(); ierr != nil {
			log.Printf("ERROR: incremental load failed: %v", ierr)
			return reloadScanState{}, true, ierr
		}
		return reloadScanState{}, true, nil
	}

	return reloadScanState{
		tenants:  tenants,
		defaults: defaults,
		hashes:   hashes,
		mtimes:   mtimes,
		graph:    graph,
	}, false, nil
}

// classifyAndCount is the heart of the reload pipeline:
//
//  1. Maintain parsedDefaults cache incrementally — reuse prior parse for
//     any defaults file whose hash didn't move (Issue #61).
//  2. For each tenant, classify into one of:
//     - source-changed         (sourceChanged=true) → applied
//     - new tenant             (wasKnown=false)     → applied
//     - defaults-changed-applied (merged_hash moved) → applied
//     - defaults-changed-noop  (merged_hash steady) → cosmetic | shadowed
//     - clean                  (nothing moved)      → reuse cached merged_hash
//  3. Account deleted tenants — previously known, absent now → applied.
//  4. Emit one ObserveBlastRadius per non-empty (reason, scope, effect)
//     bucket so a tick that fires multiple effect classes preserves the
//     dimensional detail.
//
// Counter increments (IncReloadTrigger / IncDefaultsShadowed /
// IncDefaultsNoop / ObserveBlastRadius) are side-effects emitted here
// — installNewHierarchyState only does the atomic swap.
func (m *ConfigManager) classifyAndCount(prior reloadPriorState, scan reloadScanState) reloadResult {
	res := reloadResult{
		newMergedHashes:   make(map[string]string, len(scan.tenants)),
		newParsedDefaults: make(map[string]map[string]any, len(scan.defaults)),
	}
	for tid := range scan.tenants {
		res.newMergedHashes[tid] = "" // filled below
	}

	// Issue #61: parsedDefaults cache rebuild. Reuse prior parse where
	// the hash didn't move; re-parse the rest. Parse failures are
	// log-and-skip (same policy as populateHierarchyState cold start).
	for dp := range scan.defaults {
		if scan.hashes[dp] == prior.hashes[dp] {
			if cached, ok := prior.parsedDefaults[dp]; ok && cached != nil {
				res.newParsedDefaults[dp] = cached
				continue
			}
		}
		b, rerr := os.ReadFile(dp)
		if rerr != nil {
			log.Printf("WARN: parsedDefaults: read %s: %v", dp, rerr)
			continue
		}
		parsed, perr := parseDefaultsBytes(b)
		if perr != nil {
			log.Printf("WARN: parsedDefaults: parse %s: %v", dp, perr)
			continue
		}
		res.newParsedDefaults[dp] = parsed
	}

	// Per-tick group-by emission for the blast-radius histogram. Each
	// tenant contributes one increment to exactly one (reason, scope,
	// effect) bucket; after the loop each non-empty bucket emits a single
	// Observe(N=count). Preserves dimensional detail (a tick can fire
	// applied/shadowed/cosmetic concurrently) without conflating distinct
	// events into a single sample.
	type emissionKey struct{ reason, scope, effect string }
	buckets := make(map[emissionKey]int)

	for tid, srcPath := range scan.tenants {
		prevSrc, wasKnown := prior.tenantSources[tid]
		sourceChanged := !wasKnown || prevSrc != srcPath || scan.hashes[srcPath] != prior.hashes[srcPath]

		defaultsChain := scan.graph.TenantDefaults[tid]
		defaultsChanged := false
		for _, dp := range defaultsChain {
			if scan.hashes[dp] != prior.hashes[dp] {
				defaultsChanged = true
				break
			}
		}

		if !sourceChanged && !defaultsChanged {
			// Reuse cached merged_hash — nothing that feeds this tenant moved.
			if prev, ok := prior.mergedHashes[tid]; ok {
				res.newMergedHashes[tid] = prev
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
			if prev, ok := prior.mergedHashes[tid]; ok {
				res.newMergedHashes[tid] = prev
			}
			continue
		}
		res.newMergedHashes[tid] = mh

		if sourceChanged {
			res.reloaded++
			if wasKnown {
				IncReloadTrigger(ReloadReasonSource)
				buckets[emissionKey{ReloadReasonSource, "tenant", "applied"}]++
			} else {
				IncReloadTrigger(ReloadReasonNewTenant)
				buckets[emissionKey{ReloadReasonNewTenant, "tenant", "applied"}]++
			}
		} else if defaultsChanged {
			scope := widestChangedScope(defaultsChain, scan.hashes, prior.hashes, m.path)
			if scope == "" {
				// Defensive: defaultsChanged was true but no chain entry
				// differs by hash. Shouldn't happen (defaultsChanged is
				// derived from the same comparison) — fall back to unknown.
				scope = "unknown"
			}
			if prev, ok := prior.mergedHashes[tid]; ok && prev == mh {
				// Defaults file changed but the resulting merged_hash
				// didn't — "quiet defaults edit". v2.8.0 Issue #61 splits
				// this into shadowed (tenant override blocked the change)
				// vs cosmetic (comment/reorder/whitespace).
				res.noOp++
				tenantBytes, terr := os.ReadFile(srcPath)
				effect := "cosmetic"
				if terr == nil {
					effect = classifyDefaultsNoOpEffect(
						tenantBytes, tid, defaultsChain,
						prior.parsedDefaults, res.newParsedDefaults,
						scan.hashes, prior.hashes,
					)
				}
				switch effect {
				case "shadowed":
					IncDefaultsShadowed()
				default:
					IncDefaultsNoop()
				}
				buckets[emissionKey{ReloadReasonDefaults, scope, effect}]++
			} else {
				res.reloaded++
				IncReloadTrigger(ReloadReasonDefaults)
				buckets[emissionKey{ReloadReasonDefaults, scope, "applied"}]++
			}
		}
	}

	// Detect deleted tenants — previously known, absent now. Deletions
	// don't get a merged_hash but we do account them in the reload count
	// so the caller can emit a counter.
	for tid := range prior.tenantSources {
		if _, stillKnown := scan.tenants[tid]; !stillKnown {
			res.reloaded++
			IncReloadTrigger(ReloadReasonDelete)
			buckets[emissionKey{ReloadReasonDelete, "tenant", "applied"}]++
		}
	}

	// Issue #61: emit one observation per non-empty bucket. Order
	// doesn't matter for Histogram observations; the per-key Observe
	// is the only state mutation.
	for k, n := range buckets {
		ObserveBlastRadius(k.reason, k.scope, k.effect, n)
	}

	return res
}

// installNewHierarchyState rebuilds the ThresholdConfig view via
// fullDirLoad (which acquires m.mu.Lock itself), then re-takes the lock
// to atom-swap the hierarchy-only fields and stamps the last-reload
// gauge. Splitting into two locks is intentional: fullDirLoad is slow
// (I/O + YAML parse) and we don't want the debounce goroutine to gate
// scrapes on it for hierarchy metadata updates.
//
// SetLastReloadComplete (v2.8.0 B-1.P2-a) is stamped strictly post
// atomic-swap so the gauge cannot advance ahead of observable state.
func (m *ConfigManager) installNewHierarchyState(scan reloadScanState, result reloadResult) error {
	if err := m.fullDirLoad(); err != nil {
		log.Printf("ERROR: fullDirLoad inside diffAndReload failed: %v", err)
		return err
	}

	m.mu.Lock()
	m.hierarchy.enabled = true
	m.hierarchy.tenantSources = scan.tenants
	m.hierarchy.hashes = scan.hashes
	m.hierarchy.mtimes = scan.mtimes
	m.hierarchy.mergedHashes = result.newMergedHashes
	m.hierarchy.graph = scan.graph
	m.hierarchy.parsedDefaults = result.newParsedDefaults
	m.mu.Unlock()

	SetLastReloadComplete(time.Now())
	return nil
}

// diffAndReload computes the set of tenants whose merged_hash changed
// since the previous scan and rebuilds the relevant state. Returns the
// count of tenants actually reloaded and the count of no-op defaults
// changes (a defaults file changed but none of its dependent tenants'
// merged_hash moved — the "quiet defaults edit" case described in
// ADR-018 §Reload Decisions).
//
// v2.8.0 PR-3 decomposed the original 216-line implementation into
// four named steps without changing semantics:
//
//  1. snapshotPriorState        — RLock-and-copy m.* hierarchy fields
//  2. scanAndCheckHierarchical  — scanDirHierarchical, fall back to
//                                 IncrementalLoad if neither hierarchical
//                                 mode is active nor `_defaults.yaml`
//                                 was found (sticky once flipped)
//  3. classifyAndCount          — per-tenant dirty detection +
//                                 Issue #61 effect classification
//                                 (applied / shadowed / cosmetic) +
//                                 blast-radius bucket emission
//  4. installNewHierarchyState  — fullDirLoad + atomic swap +
//                                 SetLastReloadComplete stamp
//
// Single-file mode short-circuits at the very top (no hierarchical
// concept). Trap unchanged from v2.7.0: never hold m.mu across
// recomputeMergedHash — long holds stall /metrics scrapes.
func (m *ConfigManager) diffAndReload() (reloaded, noOp int, err error) {
	if !m.isDir {
		// Single-file mode has no hierarchical concept — just reload.
		if err := m.Load(); err != nil {
			log.Printf("ERROR: single-file reload failed: %v", err)
			return 0, 0, err
		}
		return 1, 0, nil
	}

	prior := m.snapshotPriorState()

	scan, fallback, scanErr := m.scanAndCheckHierarchical(prior)
	if fallback {
		// Either an error (returned to caller) or a successful flat-mode
		// IncrementalLoad. Both cases: nothing more to do here.
		return 0, 0, scanErr
	}

	result := m.classifyAndCount(prior, scan)

	if err := m.installNewHierarchyState(scan, result); err != nil {
		return result.reloaded, result.noOp, err
	}
	return result.reloaded, result.noOp, nil
}

// recomputeMergedHash reads the tenant file + each file in its defaults
// chain, then runs computeMergedHash. Separated from diffAndReload so
// tests and /effective (read path) can share the disk-read sequence.
//
// Returns empty string + error if the tenant file or any chain entry is
// unreadable; computeMergedHash itself errors only on parse failures,
// which are returned to the caller.
//
// v2.8.0 Phase B Track A A4 (hierarchical-path companion of the flat-mode
// fix in config.go): when computeMergedHash fails on a defaults-chain
// parse error, classify the offending file, increment
// `da_config_parse_failure_total` and ERROR-log it. Cycle-6 RCA showed
// that broken `_defaults.yaml` silently dropped the entire defaults
// block — the upstream `WARN: skipping merged_hash for tenant=X` line
// alone (logMergeSkip) was too easy to miss in `gh run view --log`.
// Per-tenant duplication is intentional: ops alerts on the metric
// (`sum(rate(da_config_parse_failure_total{file_basename="_defaults.yaml"}
// [5m])) > 0`) and the count itself is the blast-radius signal.
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
	h, mergeErr := computeMergedHash(tenantBytes, tenantID, chainBytes)
	if mergeErr != nil {
		emitParseFailureSignal(tenantID, tenantFile, defaultsChain, mergeErr)
	}
	return h, mergeErr
}

// emitParseFailureSignal classifies a computeMergedHash error and, if
// it's a defaults-chain parse failure, emits the structured signal pair
// (metric + ERROR log) that ops dashboards depend on. Tenant-file parse
// errors stay at WARN via logMergeSkip — those are per-tenant noise,
// not infra-wide.
//
// Format contract: computeEffectiveConfig wraps defaults parse errors
// with `parse defaults[%d]: %w` and tenant errors with `parse tenant: %w`
// (config_inheritance.go). We string-match the prefix to map the index
// back to defaultsChain[i] for filename attribution.
func emitParseFailureSignal(tenantID, tenantFile string, defaultsChain []string, mergeErr error) {
	msg := mergeErr.Error()
	for i, dp := range defaultsChain {
		needle := fmt.Sprintf("parse defaults[%d]:", i)
		if strings.Contains(msg, needle) {
			IncParseFailure(filepath.Base(dp))
			log.Printf(
				"ERROR: skip unparseable defaults/profiles file %s (chain index %d) for tenant=%s: %v (entire block dropped — fix file or remove)",
				dp, i, tenantID, mergeErr,
			)
			return
		}
	}
	// Tenant-file parse failure: kept at WARN-class via the upstream
	// logMergeSkip caller path; still bump the per-file counter so ops
	// can detect persistently broken tenant files.
	if strings.Contains(msg, "parse tenant:") {
		IncParseFailure(filepath.Base(tenantFile))
	}
}
