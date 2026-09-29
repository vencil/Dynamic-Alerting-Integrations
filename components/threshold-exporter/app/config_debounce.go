package main

// ============================================================
// Debounced reload + hierarchical diff (v2.7.0, Phase 3)
// ============================================================
//
// This file wires the single conf.d tree walk (pkg/config/tree_scan.go) and the
// deep merge + dual-hash engine (config_inheritance.go) into ConfigManager's
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
//     incrementalLoadFrom (fed the scan it already took) so legacy flat
//     conf.d/ layouts keep their existing semantics untouched.
//   - When hierarchicalMode == true, diffAndReload owns the reload pipeline
//     end-to-end: ONE scan → diff → per-tenant merged_hash → atomic swap of
//     mergedHashes + inheritanceGraph, then commitFlatFrom on the SAME scan
//     for the ThresholdConfig view consumed by the collector (#1568: a
//     reload tick walks the tree once).
//
// Trap #12 from §8.11.2 (Debounce timer leak): Close() stops the timer;
// time.AfterFunc (vs. NewTimer + goroutine) avoids the receive-channel
// race documented in the "Stop + drain" Go FAQ (GODEBUG=gctrace=1 showed
// an orphaned timer channel in early prototypes; AfterFunc is cleaner).

import (
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"runtime/debug"
	"slices"
	"strings"
	"sync/atomic"
	"time"

	"github.com/jonboulle/clockwork"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// Reload trigger reasons — label values for the
// da_config_reload_trigger_total CounterVec (collector.go Phase 4).
//
// Kept as constants (not an enum type) so the metric label and the log line
// always agree string-for-string. Python describe_tenant.py doesn't emit
// these; they are Go-exporter internal observability.
const (
	ReloadReasonSource    = "source"   // a tenant YAML changed
	ReloadReasonDefaults  = "defaults" // a _defaults.yaml changed
	ReloadReasonNewTenant = "new"      // scan discovered a tenant ID absent previously
	ReloadReasonDelete    = "delete"   // a tenant file disappeared
	ReloadReasonForced    = "forced"   // manual trigger (SIGHUP-style, reserved)
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
	// Defensive: tests that build ConfigManager via struct literal won't
	// have a clock; install a real one rather than nil-panic. Production
	// constructors set it up via NewConfigManagerWithDebounce.
	if m.clock == nil {
		m.clock = clockwork.NewRealClock()
	}
	if m.debounce.window <= 0 {
		// Synchronous fallback — useful for v2.6.0 parity tests and for
		// the initial Load() bootstrap where we don't want to gate startup
		// on a timer. Observe reload duration so callers using the
		// zero-window opt-out still feed the SLO histogram (B-3).
		m.recordReason(reason)
		// Outermost reload entry → take reloadMu here (see the field doc in
		// config.go, #2122). debounce.mu is not held at this point.
		m.reloadMu.Lock()
		t0 := time.Now()
		_, _, err := m.diffAndReload()
		m.getMetrics().ObserveReloadDuration(time.Since(t0))
		m.reloadMu.Unlock()
		if err != nil {
			m.getLogger().Printf("ERROR: synchronous reload failed: %v", err)
		}
		m.maybeFreeOSMemory()
		return
	}

	m.debounce.mu.Lock()
	m.debounce.pendingReasons = append(m.debounce.pendingReasons, reason)
	if m.debounce.timer == nil {
		// First event in this window — arm the timer.
		m.debounce.timer = m.clock.AfterFunc(m.debounce.window, m.fireDebounced)
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
			m.debounce.timer = m.clock.AfterFunc(m.debounce.window, m.fireDebounced)
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
//
// Because debounce.timer is cleared before the reload runs, a trigger that
// arrives mid-reload arms a NEW timer, and that timer can fire while this
// reload is still running. The reload therefore runs under reloadMu
// (#2122): the second fire waits for the first to finish instead of
// interleaving its two install windows with ours. It is taken only after
// debounce.mu is released — reloadMu is the outermost lock, and holding
// debounce.mu while waiting on it would also stall every trigger for the
// length of a reload.
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
	m.getMetrics().ObserveDebounceBatch(len(reasons))
	atomic.AddUint64(&m.debounce.fired, 1)
	// t0 is taken after the lock so the reload-duration histogram keeps
	// measuring the reload, not the time spent queued behind another one.
	m.reloadMu.Lock()
	t0 := time.Now()
	_, _, err := m.diffAndReload()
	m.getMetrics().ObserveReloadDuration(time.Since(t0))
	m.reloadMu.Unlock()
	if err != nil {
		m.getLogger().Printf("ERROR: debounced reload failed: %v", err)
	}
	m.maybeFreeOSMemory()
}

// maybeFreeOSMemory returns idle heap pages to the OS after a reload when
// the -free-os-mem-after-reload lever is on (#459). No-op by default.
//
// debug.FreeOSMemory() forces a GC and an immediate scavenge, so it is
// deliberately called OUTSIDE m.mu and only once per fired reload window
// (not per affected tenant). It runs even on reload error: a failed reload
// still allocated scan/parse buffers whose backing pages are exactly what
// we want returned. The cost is one extra STW GC per reload, which is
// acceptable because production reload cadence is hours-to-days; the lever
// exists so the #459 soak can quantify whether aggressive return-to-OS
// bounds the sys_bytes / heap_idle high-water creep.
func (m *ConfigManager) maybeFreeOSMemory() {
	if !m.freeOSMemEnabled() {
		return
	}
	debug.FreeOSMemory()
	m.getMetrics().IncFreeOSMemory()
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
	hashes           map[string]string
	mergedHashes     map[string]string
	tenantSources    map[string]string
	parsedDefaults   map[string]map[string]any // Issue #61: shadow-vs-cosmetic baseline
	hierarchicalMode bool
	// tree is the last commit's scan, the prior of this tick's walk
	// (flatScanState.tree). It carries the mtimes the fast-path compares
	// against AND the tenant declarations it must carry across.
	tree *treeScan
	// graph is the last commit's inheritance graph. classifyTenant compares
	// each tenant's prior defaults chain (path sequence) against this tick's:
	// a chain whose MEMBERSHIP changed — a `_defaults` file deleted, or the
	// selected carrier of a co-located `.yaml`/`.yml` pair switching to a
	// file whose hash was already known — moves merged_hash even though no
	// entry of the new chain changed hash (#1964). nil before hierarchical
	// mode first commits.
	graph *InheritanceGraph
	// platform is the last commit's root platform files' `tenants:`
	// blocks (#2019): the second input set classifyTenant compares, and
	// the decode cache this tick's LoadRootPlatformTenants reuses by hash.
	platform []config.PlatformTenants
	// profiles is the last commit's root platform files' `profiles:` set
	// (#2117): what this tick's is compared with, and reused as is when no
	// root platform file moved.
	profiles *config.PlatformProfiles
	// mergeRetry is the tenants the last tick could not recompute because
	// a read failed (#2100): recomputed on this tick even if nothing that
	// feeds them moved.
	mergeRetry map[string]struct{}
}

// reloadScanState bundles the hierarchy projection of this tick's scan
// when the scan committed to the hierarchical path, plus the scan itself
// so the flat view is rebuilt from the same walk. When the scan resolves
// to the flat path, scanAndCheckHierarchical returns fallback=true and
// the caller short-circuits without populating this struct.
type reloadScanState struct {
	tenants  map[string]string
	defaults map[string]bool
	hashes   map[string]string
	graph    *InheritanceGraph
	tree     *treeScan
	// platform is this tick's root platform files' `tenants:` blocks
	// (#2019); platformMoved is platformFilesMoved(prior, this) — false on
	// the common tick, which skips the per-tenant comparison.
	platform      []config.PlatformTenants
	platformMoved bool
	// profiles is this tick's root platform files' `profiles:` set
	// (#2117) — the prior's own pointer when no root platform file moved,
	// so the common tick decodes nothing. changedProfiles is
	// config.ChangedProfiles(prior, this): nil on every tick where no
	// profile's content moved, which skips the per-tenant question.
	profiles        *config.PlatformProfiles
	changedProfiles map[string]struct{}
}

// reloadResult bundles classifyAndCount's output for installNewHierarchyState
// and the diffAndReload return value. blast-radius histogram observations
// are emitted inside classifyAndCount before it returns; this struct only
// carries the state the install step needs to atom-swap.
type reloadResult struct {
	newMergedHashes   map[string]string
	newParsedDefaults map[string]map[string]any
	// newMergeRetry is this tick's failed-read tenants (#2100), installed
	// as hierarchy.mergeRetry. nil = none.
	newMergeRetry map[string]struct{}
	reloaded      int
	noOp          int
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
		hashes:           m.hierarchy.hashes,
		mergedHashes:     m.hierarchy.mergedHashes,
		tenantSources:    m.hierarchy.tenantSources,
		parsedDefaults:   m.hierarchy.parsedDefaults, // Issue #61
		graph:            m.hierarchy.graph,          // #1964: prior chain membership
		platform:         m.hierarchy.platform,       // #2019: prior platform per-tenant blocks
		profiles:         m.hierarchy.profiles,       // #2117: prior profiles
		mergeRetry:       m.hierarchy.mergeRetry,     // #2100: failed-read tenants to retry
		hierarchicalMode: m.hierarchy.enabled,
		tree:             m.flat.tree,
	}
}

// scanAndCheckHierarchical walks the tree ONCE (scanDirTree with the
// retained prior) and decides the path:
//
//	hierarchical → return scan, fallback=false; caller continues
//	flat         → incrementalLoadFrom(this scan), return fallback=true; caller short-circuits
//	error        → return fallback=true + err; caller propagates
//
// A duplicate tenant is a scan error here, as the hierarchical wrapper
// always reported it. hierarchicalMode is sticky once activated: a config
// that introduces `_defaults.yaml` flips the bit ON, and even if the file
// is later deleted we keep using the hierarchical path because
// computeMergedHash with an empty chain is well-defined.
func (m *ConfigManager) scanAndCheckHierarchical(prior reloadPriorState) (reloadScanState, bool, error) {
	scan, scanErr := scanDirTree(m.path, prior.tree, m.getMetrics(), m.getLogger())
	if scanErr == nil && scan.Conflict != nil {
		scanErr = scan.Conflict
	}
	if scanErr != nil {
		m.getLogger().Printf("ERROR: hierarchical scan failed: %v", scanErr)
		return reloadScanState{}, true, scanErr
	}

	// If no _defaults.yaml was discovered AND we haven't activated
	// hierarchical mode yet, stay on the flat path — fed this scan, so the
	// flat tick does not walk again. IncrementalLoad's own cold-start
	// guard (no flat cache yet → full load) is kept here for the same
	// reason it exists there.
	if !prior.hierarchicalMode && len(scan.Defaults) == 0 {
		m.mu.RLock()
		hasCache := len(m.flat.hashes) > 0
		m.mu.RUnlock()
		var ierr error
		if hasCache {
			ierr = m.incrementalLoadFrom(scan)
		} else {
			ierr = m.fullDirLoadFrom(scan)
		}
		if ierr != nil {
			m.getLogger().Printf("ERROR: incremental load failed: %v", ierr)
			return reloadScanState{}, true, ierr
		}
		return reloadScanState{}, true, nil
	}

	platform := config.LoadRootPlatformTenants(scan, prior.platform, readTreeFile)
	platformMoved := platformFilesMoved(prior.platform, platform)
	// #2117: the profiles come from the same files. Unmoved files ⇒ the
	// prior set as is (no decode on the common tick); moved ⇒ decoded
	// again and compared, per profile name.
	// No prior set (hierarchical mode not yet committed) ⇒ nothing to
	// compare against: every tenant is computed fresh on that tick anyway.
	profiles, changedProfiles := prior.profiles, map[string]struct{}(nil)
	if platformMoved || profiles == nil {
		profiles = config.LoadRootPlatformProfiles(scan, readTreeFile)
		if prior.profiles != nil {
			changedProfiles = config.ChangedProfiles(prior.profiles, profiles)
		}
	}
	return reloadScanState{
		tenants:         scan.Tenants,
		defaults:        scan.Defaults,
		hashes:          scan.AbsHashes(),
		graph:           scan.InheritanceGraph(),
		tree:            scan,
		platform:        platform,
		platformMoved:   platformMoved,
		profiles:        profiles,
		changedProfiles: changedProfiles,
	}, false, nil
}

// reloadEmissionKey is the group-by key for the per-tick blast-radius
// histogram: each tenant contributes one increment to exactly one
// (reason, scope, effect) bucket, and classifyAndCount emits a single
// Observe(N=count) per non-empty bucket afterwards. Preserves dimensional
// detail (a tick can fire applied/shadowed/cosmetic concurrently) without
// conflating distinct events into one sample.
type reloadEmissionKey struct{ reason, scope, effect string }

// rebuildParsedDefaults rebuilds the parsedDefaults cache for a reload tick
// (Issue #61): it reuses the prior parse for any defaults file whose hash did
// not move and re-parses the rest. Read/parse failures are log-and-skip (same
// policy as populateHierarchyStateFrom cold start). Extracted from classifyAndCount.
func (m *ConfigManager) rebuildParsedDefaults(prior reloadPriorState, scan reloadScanState) map[string]map[string]any {
	out := make(map[string]map[string]any, len(scan.defaults))
	for dp := range scan.defaults {
		if scan.hashes[dp] == prior.hashes[dp] {
			if cached, ok := prior.parsedDefaults[dp]; ok && cached != nil {
				out[dp] = cached
				continue
			}
		}
		b, rerr := os.ReadFile(dp)
		if rerr != nil {
			m.getLogger().Printf("WARN: parsedDefaults: read %q: %v", dp, rerr)
			continue
		}
		parsed, perr := parseDefaultsBytes(b)
		if perr != nil {
			m.getLogger().Printf("WARN: parsedDefaults: parse %q: %v", dp, perr)
			continue
		}
		out[dp] = parsed
	}
	return out
}

// classifyTenant classifies one tenant during a reload tick into its
// reload/no-op category, updating res (merged_hash, reloaded/noOp counters) and
// the per-tick emission buckets, and emitting the trigger/shadowed/noop
// counters. Extracted verbatim from the classifyAndCount per-tenant loop body;
// res is mutated through the pointer, buckets through the shared map.
//
// tenantFiles is this tick's tenant files, each read and parsed once for
// every tenant it declares (#2153) — classifyAndCount's, dropped with it.
func (m *ConfigManager) classifyTenant(tid, srcPath string, prior reloadPriorState, scan reloadScanState, res *reloadResult, buckets map[reloadEmissionKey]int, tenantFiles *tenantFilesOnce) {
	prevSrc, wasKnown := prior.tenantSources[tid]
	sourceChanged := !wasKnown || prevSrc != srcPath || scan.hashes[srcPath] != prior.hashes[srcPath]

	defaultsChain := scan.graph.TenantDefaults[tid]
	// scopePaths: the defaults files whose change this tick feeds the
	// tenant — chain entries whose hash moved, plus (#1964) any path that
	// left or joined the chain. Hash comparison alone misses a membership
	// change: deleting a `_defaults` file, or a co-located `.yaml`/`.yml`
	// pair switching its selected carrier to a file whose hash was already
	// in prior.hashes, leaves every entry of the NEW chain hash-stable while
	// the merge input changed.
	scopePaths := hashChangedChainPaths(defaultsChain, scan.hashes, prior.hashes)
	membershipChanged := false
	var removedPaths, addedPaths []string
	if prior.graph != nil {
		// A tenant absent from the prior graph compares as an empty chain;
		// a genuinely new tenant is already sourceChanged, so this only
		// adds signal for tenants that were known.
		priorChain := prior.graph.TenantDefaults[tid]
		if !slices.Equal(priorChain, defaultsChain) {
			membershipChanged = true
			removedPaths, addedPaths = chainMembershipDelta(priorChain, defaultsChain)
			scopePaths = append(scopePaths, removedPaths...)
			scopePaths = append(scopePaths, addedPaths...)
		}
	}
	// #2019: the root platform files' entries for this tenant are merge
	// input too, outside the chain. Compared only on a tick where some
	// root platform file moved, and then per tenant — an edit to another
	// tenant's entry does not touch this one.
	var overlayKeys []string
	if scan.platformMoved {
		var overlayPaths []string
		overlayPaths, overlayKeys = platformOverlayDelta(prior.platform, scan.platform, tid)
		scopePaths = append(scopePaths, overlayPaths...)
	}
	// #2117: so are the root platform files' `profiles:` — for a tenant
	// whose `_profile` names a profile this tick changed. Asked only on a
	// tick where some profile's content moved; a changed tenant file is
	// recomputed (and attributed to its source) anyway.
	var profileKeys []string
	if len(scan.changedProfiles) > 0 && !sourceChanged {
		var profilePaths []string
		profilePaths, profileKeys = profileDeltaFor(tid, srcPath, prior, scan)
		scopePaths = append(scopePaths, profilePaths...)
	}
	defaultsChanged := membershipChanged || len(scopePaths) > 0
	_, retrying := prior.mergeRetry[tid]

	if !sourceChanged && !defaultsChanged && !retrying {
		// Reuse cached merged_hash — nothing that feeds this tenant moved.
		if prev, ok := prior.mergedHashes[tid]; ok {
			res.newMergedHashes[tid] = prev
			return
		}
		// No cached value (first scan after enabling hierarchical mode).
		// Fall through to compute.
	}

	overlay := config.PlatformOverlayFor(scan.platform, tid)
	mh, mergeErr := m.recomputeMergedHashWith(tid, srcPath, defaultsChain, config.TenantLayers{Overlay: overlay, Profiles: scan.profiles}, tenantFiles)
	if mergeErr != nil {
		// #2100: a failed READ is not a fact about the inputs this tick
		// commits (their hashes come from the scan), so the tenant is
		// marked for retry: by tickOnce's retryFailedMergedHashes on a
		// tick that finds no change, or here on one that reloads. A parse/merge
		// error is a fact about those bytes: retrying unchanged bytes
		// fails the same way, so it is not retried (and its parse-failure
		// signal is not re-emitted every tick). One attempt per tick; the
		// skip line is written when the failure starts, not on every
		// retry that fails again.
		if isMergeReadError(mergeErr) {
			if res.newMergeRetry == nil {
				res.newMergeRetry = make(map[string]struct{})
			}
			res.newMergeRetry[tid] = struct{}{}
			if !retrying {
				logMergeSkip(m.getLogger(), tid, "debounced-reload", mergeErr)
			}
		} else {
			logMergeSkip(m.getLogger(), tid, "debounced-reload", mergeErr)
		}
		// Preserve any prior merged_hash we had so the /effective
		// endpoint still serves the last-known-good value. Absent prior
		// → mark empty (tenant will read as merge-failing).
		if prev, ok := prior.mergedHashes[tid]; ok {
			res.newMergedHashes[tid] = prev
		}
		return
	}
	res.newMergedHashes[tid] = mh
	if retrying {
		m.getLogger().Printf("INFO: merged_hash for tenant=%s recomputed after an earlier read failure", tid)
	}
	// A retry that catches up with nothing moved on disk is not a reload:
	// neither branch below fires, so the reload attribution (triggers,
	// no-op/shadowed, blast radius) is what it was before #2100 — the
	// failing tick never counted this tenant, and neither does this one.

	if sourceChanged {
		res.reloaded++
		if wasKnown {
			m.getMetrics().IncReloadTrigger(ReloadReasonSource)
			buckets[reloadEmissionKey{ReloadReasonSource, "tenant", "applied"}]++
		} else {
			m.getMetrics().IncReloadTrigger(ReloadReasonNewTenant)
			buckets[reloadEmissionKey{ReloadReasonNewTenant, "tenant", "applied"}]++
		}
	} else if defaultsChanged {
		scope := widestPathScope(scopePaths, m.path)
		if scope == "" {
			// Defensive: defaultsChanged was true but no path was
			// attributed. Only reachable when the chain was reordered
			// with identical membership and hashes — fall back to unknown.
			scope = "unknown"
		}
		if prev, ok := prior.mergedHashes[tid]; ok && prev == mh {
			// Defaults file changed but the resulting merged_hash
			// didn't — "quiet defaults edit". v2.8.0 Issue #61 splits
			// this into shadowed (tenant override blocked the change)
			// vs cosmetic (comment/reorder/whitespace).
			//
			// #1964: a chain-membership change whose merged_hash did not
			// move lands here too. The classifier counts a file that left
			// the chain as withdrawing every key it set and a file that
			// joined as applying every key it sets — except a same-directory
			// carrier switch, diffed old carrier vs new — so a removal the
			// tenant overrides reads as shadowed and one another chain
			// entry already supplies reads as cosmetic — same two effects,
			// no new label.
			res.noOp++
			tenantBytes, terr := os.ReadFile(srcPath)
			effect := "cosmetic"
			if terr == nil {
				effect = classifyDefaultsNoOpEffect(
					tenantBytes, tid, defaultsChain,
					prior.parsedDefaults, res.newParsedDefaults,
					scan.hashes, prior.hashes,
					removedPaths, addedPaths,
					overlayKeys, overlay,
					profileKeys, scan.profiles,
				)
			}
			switch effect {
			case "shadowed":
				m.getMetrics().IncDefaultsShadowed()
			default:
				m.getMetrics().IncDefaultsNoop()
			}
			buckets[reloadEmissionKey{ReloadReasonDefaults, scope, effect}]++
		} else {
			res.reloaded++
			m.getMetrics().IncReloadTrigger(ReloadReasonDefaults)
			buckets[reloadEmissionKey{ReloadReasonDefaults, scope, "applied"}]++
		}
	}
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
		newMergedHashes: make(map[string]string, len(scan.tenants)),
	}
	for tid := range scan.tenants {
		res.newMergedHashes[tid] = "" // filled below
	}

	// Issue #61: rebuild the parsedDefaults cache, reusing unchanged parses.
	res.newParsedDefaults = m.rebuildParsedDefaults(prior, scan)

	// Per-tick group-by buckets for the blast-radius histogram (see
	// reloadEmissionKey); each tenant increments exactly one bucket.
	buckets := make(map[reloadEmissionKey]int)

	// #2153: one read + parse per tenant file for this tick's merges, not
	// one per tenant it declares — tenants visited file by file. This tick
	// only: the next tick starts empty, so no parse outlives its bytes.
	tenantFiles := &tenantFilesOnce{onParse: m.onReloadTenantParse, read: m.reloadMergeRead}
	for _, tid := range tenantsByFile(scan.tenants) {
		m.classifyTenant(tid, scan.tenants[tid], prior, scan, &res, buckets, tenantFiles)
	}

	// Detect deleted tenants — previously known, absent now. Deletions
	// don't get a merged_hash but we do account them in the reload count
	// so the caller can emit a counter.
	for tid := range prior.tenantSources {
		if _, stillKnown := scan.tenants[tid]; !stillKnown {
			res.reloaded++
			m.getMetrics().IncReloadTrigger(ReloadReasonDelete)
			buckets[reloadEmissionKey{ReloadReasonDelete, "tenant", "applied"}]++
		}
	}

	// Issue #61: emit one observation per non-empty bucket. Order
	// doesn't matter for Histogram observations; the per-key Observe
	// is the only state mutation.
	for k, n := range buckets {
		m.getMetrics().ObserveBlastRadius(k.reason, k.scope, k.effect, n)
	}

	return res
}

// installNewHierarchyState atom-swaps the hierarchy-only fields under
// m.mu, then rebuilds and commits the ThresholdConfig view from the SAME
// scan (commitFlatFrom, which takes m.mu itself), and stamps the
// last-reload gauge. Two lock windows on purpose: the flat rebuild parses
// YAML and we don't want the debounce goroutine to gate scrapes on it.
//
// ⛔ Two windows means a second reload must not run in between: hierW →
// hierB → flatB → flatW would leave the service on W while the hierarchy
// plane (the baseline detectChange compares against) says B, and nothing
// would ever reload it back (#2122). The caller's reload entry holds
// reloadMu for exactly that reason; this function does not take it itself
// because it runs inside diffAndReload, below the outermost entry.
//
// ⛔ Hierarchy FIRST, then the flat commit — the reverse of the historical
// order, for two reasons that both come from having one walk:
//
//   - installConfig hands the commit-time audit the tenantSources standing
//     in the commit's lock window. Committing the new config against the
//     PREVIOUS tenantSources would judge this tick's refused keys against
//     the previous population until the next commit.
//   - commitFlatFrom materialises subtree defaults from the graph and the
//     parsed defaults the manager holds; those must be this tick's, not
//     the last one's, or a tenant added under a subtree would carry the
//     root value for one reload.
//
// The only way the flat commit can fail after a successful scan — an
// empty tree — is checked before anything is installed, so a failure
// leaves BOTH planes on the previous state rather than one ahead of the
// other.
//
// SetLastReloadComplete (v2.8.0 B-1.P2-a) is stamped strictly post
// commit so the gauge cannot advance ahead of observable state.
func (m *ConfigManager) installNewHierarchyState(scan reloadScanState, result reloadResult) error {
	if len(scan.tree.Files) == 0 {
		err := fmt.Errorf("no .yaml files found in %s", m.path)
		m.getLogger().Printf("ERROR: flat rebuild inside diffAndReload refused: %v", err)
		return err
	}

	m.mu.Lock()
	m.hierarchy.enabled = true
	m.hierarchy.tenantSources = scan.tenants
	m.hierarchy.hashes = scan.hashes
	m.hierarchy.mergedHashes = result.newMergedHashes
	m.hierarchy.graph = scan.graph
	m.hierarchy.parsedDefaults = result.newParsedDefaults
	m.hierarchy.platform = scan.platform
	m.hierarchy.profiles = scan.profiles
	m.hierarchy.mergeRetry = result.newMergeRetry
	m.hierarchy.gen++
	afterHierarchyInstall := m.afterHierarchyInstall
	m.mu.Unlock()

	// Test-only seam (nil in production): the planes disagree right here,
	// which is the instant #2122's regression test parks a reload in.
	if afterHierarchyInstall != nil {
		afterHierarchyInstall()
	}

	if err := m.commitFlatFrom(scan.tree); err != nil {
		m.getLogger().Printf("ERROR: flat rebuild (commitFlatFrom) inside diffAndReload failed: %v", err)
		return err
	}

	m.getMetrics().SetLastReloadComplete(time.Now())
	return nil
}

// diffAndReload computes the set of tenants whose merged_hash changed
// since the previous scan and rebuilds the relevant state. Returns the
// count of tenants actually reloaded and the count of no-op defaults
// changes (a defaults file changed but none of its dependent tenants'
// merged_hash moved — the "quiet defaults edit" case described in
// ADR-017 §Reload Decisions).
//
// v2.8.0 PR-3 decomposed the original 216-line implementation into
// four named steps without changing semantics:
//
//  1. snapshotPriorState        — RLock-and-copy m.* hierarchy fields +
//     the retained *treeScan (the prior)
//  2. scanAndCheckHierarchical  — ONE scanDirTree; fall back to
//     incrementalLoadFrom on that same scan
//     if neither hierarchical mode is active
//     nor `_defaults.yaml` was found
//     (sticky once flipped)
//  3. classifyAndCount          — per-tenant dirty detection +
//     Issue #61 effect classification
//     (applied / shadowed / cosmetic) +
//     blast-radius bucket emission
//  4. installNewHierarchyState  — atomic swap of the hierarchy fields,
//     then commitFlatFrom on the same scan,
//     then the SetLastReloadComplete stamp
//
// Single-file mode short-circuits at the very top (no hierarchical
// concept). Trap unchanged from v2.7.0: never hold m.mu across
// recomputeMergedHash — long holds stall /metrics scrapes.
func (m *ConfigManager) diffAndReload() (reloaded, noOp int, err error) {
	if !m.isDir {
		// Single-file mode has no hierarchical concept — just reload.
		if err := m.Load(); err != nil {
			m.getLogger().Printf("ERROR: single-file reload failed: %v", err)
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
//
// `layers` is the tenant's root-platform-file entries (#2019,
// config.PlatformOverlayFor) and the tree's profiles (#2117) — part of what
// merged_hash is a hash of.
func (m *ConfigManager) recomputeMergedHash(tenantID, tenantFile string, defaultsChain []string, layers config.TenantLayers) (string, error) {
	return m.recomputeMergedHashWith(tenantID, tenantFile, defaultsChain, layers, &tenantFilesOnce{})
}

// recomputeMergedHashWith is recomputeMergedHash taking the tenant file from
// tenantFiles (#2153): read and parsed on the first tenant of that file this
// tick, reused for the rest. Same errors in the same order — the tenant
// file's read error first, then the chain's, then the chain's parse errors,
// then the tenant file's (config.ComputeMergedHashDoc keeps a syntax error
// until the merge reaches the tenant file, as the byte form did).
func (m *ConfigManager) recomputeMergedHashWith(tenantID, tenantFile string, defaultsChain []string, layers config.TenantLayers, tenantFiles *tenantFilesOnce) (string, error) {
	read := tenantFiles.reader()
	doc, err := tenantFiles.get(tenantFile, read)
	if err != nil {
		return "", mergeReadError{err}
	}
	chainBytes := make([][]byte, 0, len(defaultsChain))
	for _, dp := range defaultsChain {
		b, rerr := read(dp)
		if rerr != nil {
			return "", mergeReadError{rerr}
		}
		chainBytes = append(chainBytes, b)
	}
	h, mergeErr := config.ComputeMergedHashDoc(doc, tenantID, chainBytes, layers)
	if mergeErr != nil {
		emitParseFailureSignal(m.getMetrics(), m.getLogger(), tenantID, tenantFile, defaultsChain, mergeErr)
	}
	return h, mergeErr
}

// retryFailedMergedHashes recomputes the merged_hash of each tenant in
// hierarchy.mergeRetry (#2100) and installs the ones that now succeed. It is
// tickOnce's path for a tick whose detectChange found nothing: the failing
// reload already committed that tick's inputs, so no reload will run again
// until some other input moves, and this is the only thing that catches the
// tenant up.
//
// Deliberately NOT a reload: no scan, no debounce, no commitFlatFrom (the
// flat config was committed by the failing tick itself — only merged_hash
// is behind), and none of the reload metrics (triggers, blast radius,
// debounce batch / fired count, reload duration, last-reload stamp). While
// the read keeps failing, a tick costs one recompute per marked tenant and
// writes nothing to the log (the skip line was written when the failure
// started); the tick that succeeds writes one INFO line per tenant.
//
// A reload in progress owns the retry set (classifyTenant retries it), so
// this path runs only if it can take reloadMu without waiting — the guard
// that keeps it from interleaving with a reload (pinned by
// TestReload_MergeRetry_ParkedAcrossReload). It also installs only if
// hierarchy.gen is unchanged since its snapshot: a second guard, for an
// install that takes no reloadMu (a direct Load); see the gen field for why
// no production path reaches it today and why it is kept.
func (m *ConfigManager) retryFailedMergedHashes() {
	m.mu.RLock()
	pending := len(m.hierarchy.mergeRetry)
	m.mu.RUnlock()
	if pending == 0 {
		return
	}
	if !m.reloadMu.TryLock() {
		return
	}
	defer m.reloadMu.Unlock()

	m.mu.RLock()
	gen := m.hierarchy.gen
	retry := m.hierarchy.mergeRetry
	sources := m.hierarchy.tenantSources
	graph := m.hierarchy.graph
	platform := m.hierarchy.platform
	profiles := m.hierarchy.profiles
	m.mu.RUnlock()

	subset := make(map[string]string, len(retry))
	for tid := range retry {
		subset[tid] = sources[tid]
	}
	recovered := make(map[string]string, len(retry))
	done := make(map[string]struct{}, len(retry)) // leaves the retry set
	tenantFiles := &tenantFilesOnce{onParse: m.onReloadTenantParse, read: m.reloadMergeRead}
	for _, tid := range tenantsByFile(subset) {
		src, known := sources[tid]
		if !known {
			done[tid] = struct{}{}
			continue
		}
		var chain []string
		if graph != nil {
			chain = graph.TenantDefaults[tid]
		}
		mh, err := m.recomputeMergedHashWith(tid, src, chain, config.TenantLayers{Overlay: config.PlatformOverlayFor(platform, tid), Profiles: profiles}, tenantFiles)
		if err != nil {
			if isMergeReadError(err) {
				continue // still unreadable: stays marked, already logged
			}
			// Read now, but the bytes do not merge: retrying them again
			// cannot change that. Same outcome as a reload's parse
			// failure — last-known-good kept, not retried.
			logMergeSkip(m.getLogger(), tid, "merged-hash-retry", err)
			done[tid] = struct{}{}
			continue
		}
		recovered[tid] = mh
		done[tid] = struct{}{}
	}
	if len(done) == 0 {
		return
	}

	m.mu.Lock()
	if m.hierarchy.gen != gen {
		// Something installed meanwhile without reloadMu (a direct Load —
		// see hierarchyState.gen); its state, and its own retry set,
		// supersede what this was computed from.
		m.mu.Unlock()
		return
	}
	hashes := make(map[string]string, len(m.hierarchy.mergedHashes)+len(recovered))
	for tid, h := range m.hierarchy.mergedHashes {
		hashes[tid] = h
	}
	for tid, h := range recovered {
		hashes[tid] = h
	}
	var next map[string]struct{}
	for tid := range retry {
		if _, ok := done[tid]; ok {
			continue
		}
		if next == nil {
			next = make(map[string]struct{})
		}
		next[tid] = struct{}{}
	}
	m.hierarchy.mergedHashes = hashes
	m.hierarchy.mergeRetry = next
	m.hierarchy.gen++
	m.mu.Unlock()

	for tid := range recovered {
		m.getLogger().Printf("INFO: merged_hash for tenant=%s recomputed after an earlier read failure", tid)
	}
}

// mergeReadError marks a recomputeMergedHashWith error as a failed file
// read rather than a parse/merge failure of the bytes read (#2100): the
// reload retries the former on the next tick. Transparent otherwise —
// same message, and errors.Is/As see the read error through Unwrap.
type mergeReadError struct{ err error }

func (e mergeReadError) Error() string { return e.err.Error() }
func (e mergeReadError) Unwrap() error { return e.err }

func isMergeReadError(err error) bool {
	var re mergeReadError
	return errors.As(err, &re)
}

// tenantFilesOnce is the tenant file one merge pass (a cold load, or one
// reload tick) read and parsed last: the next tenant of the same file reuses
// that read and parse (config.ParseTenantDoc) instead of its own (#2153).
// Before it a file declaring T tenants was read and parsed T times per pass,
// each parse paying yaml.v3's duplicate-key check over the whole `tenants:`
// mapping — quadratic in T.
//
// ONE file is held, not every file of the pass: the passes visit tenants
// grouped by file (tenantsByFile), so one slot parses each file once, while
// memory stays that of one parsed file, as before — a map of every parsed
// file would hold the whole tree's decoded documents until the pass ends.
// A caller that interleaves files is still correct, it only parses again.
//
// ⛔ Lifetime: ONE pass. The owner (populateHierarchyStateWith's
// coldMergeInputs, classifyAndCount's local) drops it with the pass, so a
// parse never outlives the bytes it was taken from and there is nothing to
// invalidate across reloads. One held path names one read: the tenants after
// the first reuse that read — including its error.
//
// Isolation between the tenants sharing one parse: config.TenantDoc hands
// each merge a deep copy of that tenant's own block (see its comment); the
// shared document is never written after the parse.
//
// Not safe for concurrent use: both passes merge on one goroutine. The zero
// value is ready to use.
type tenantFilesOnce struct {
	path    string // the held file; "" = none
	doc     *config.TenantDoc
	readErr error
	// onParse is a test seam, nil in production: called once per parse.
	onParse func(absPath string)
	// read is a test seam, nil in production (= os.ReadFile): the read
	// recomputeMergedHashWith makes for the tenant file AND for each
	// defaults-chain file of the same merge (#2100). Set from
	// ConfigManager.reloadMergeRead.
	read func(string) ([]byte, error)
}

// reader is t.read, or os.ReadFile when no seam is set.
func (t *tenantFilesOnce) reader() func(string) ([]byte, error) {
	if t.read != nil {
		return t.read
	}
	return os.ReadFile
}

// get is absPath's parsed document, reading it with read unless it is the
// held file.
func (t *tenantFilesOnce) get(absPath string, read func(string) ([]byte, error)) (*config.TenantDoc, error) {
	if t.path != "" && t.path == absPath {
		return t.doc, t.readErr
	}
	t.path, t.doc, t.readErr = absPath, nil, nil
	b, err := read(absPath)
	if err != nil {
		t.readErr = err
		return nil, err
	}
	t.doc = config.ParseTenantDoc(b)
	if t.onParse != nil {
		t.onParse(absPath)
	}
	return t.doc, nil
}

// tenantsByFile is tenants' IDs grouped by source file — every tenant of a
// file next to the others — the order in which tenantFilesOnce parses each
// file once. Grouped, not sorted: it runs on every reload tick, a no-change
// tick included, and a sort whose comparator looks up two map entries per
// comparison cost that tick more than grouping does (bench gate on #2255).
// Counted then placed into one pre-sized slice, not appended per file: a
// slice per file grew hundreds of allocations per tick (bench gate on
// #2261). Neither the file order nor the order within a file is defined; a
// merge pass's result does not depend on it (each tenant's merge is
// independent; the maps it fills are keyed by tenant), which was Go's
// random map order before.
func tenantsByFile(tenants map[string]string) []string {
	next := make(map[string]int) // file → its tenants' count, then next slot
	for _, file := range tenants {
		next[file]++
	}
	start := 0
	for file, n := range next {
		next[file] = start
		start += n
	}
	ids := make([]string, len(tenants))
	for tid, file := range tenants {
		ids[next[file]] = tid
		next[file]++
	}
	return ids
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
//
// metrics + logger are plumbed in (not the package globals) so the
// caller's ConfigManager.metrics + ConfigManager.logger instances are
// the routed ones — foundation for per-test isolation (#4a + #4b).
// Either may be nil → falls back to the package singleton.
func emitParseFailureSignal(metrics *configMetrics, logger *log.Logger, tenantID, tenantFile string, defaultsChain []string, mergeErr error) {
	if metrics == nil {
		metrics = getConfigMetrics()
	}
	if logger == nil {
		logger = log.Default()
	}
	msg := mergeErr.Error()
	for i, dp := range defaultsChain {
		needle := fmt.Sprintf("parse defaults[%d]:", i)
		if strings.Contains(msg, needle) {
			metrics.IncParseFailure(filepath.Base(dp))
			logger.Printf(
				"ERROR: skip unparseable defaults/profiles file %q (chain index %d) for tenant=%s: %v (entire block dropped — fix file or remove)",
				dp, i, tenantID, mergeErr,
			)
			return
		}
	}
	// Tenant-file parse failure: kept at WARN-class via the upstream
	// logMergeSkip caller path; still bump the per-file counter so ops
	// can detect persistently broken tenant files.
	if strings.Contains(msg, "parse tenant:") {
		metrics.IncParseFailure(filepath.Base(tenantFile))
	}
}
