package main

import (
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/jonboulle/clockwork"
)

// ============================================================
// ConfigManager — supports both single-file and directory mode
// ============================================================

// flatScanState bundles the v2.1.0 incremental-reload caches used by the
// flat-mode pipeline (`scanDirTree` + `IncrementalLoad`). Per-file
// SHA-256 + parsed partial config + mtime fast-path stat. nil maps when
// the manager is in single-file mode or has not yet completed its first
// directory scan.
type flatScanState struct {
	hashes  map[string]string          // filename → SHA-256
	configs map[string]ThresholdConfig // filename → parsed partial config
	mtimes  map[string]fileStat        // filename → mtime+size for quick skip (v2.1.0)

	// tree is the *treeScan the last COMMIT was built from, and the prior of
	// the next scan (#1568). It is retained here, next to `hashes` and
	// `configs`, and installed with them by installConfig. Correctness of
	// the partial reuse in commitFlatFrom does not depend on that pairing —
	// the reuse is gated on `hashes[k] == scan.Files[k].Hash`, so a prior
	// from an older commit only costs a disk read per file whose bytes the
	// walker did not cache. Keeping the three together is what makes the
	// quiet tick stat-only: every unchanged file is a fast-path hit AND a
	// partial-cache hit.
	//
	// ⛔ It is the scan itself, never a prior rebuilt from `hashes`+`mtimes`
	// (the shape the flat wrapper in scan_wrappers_test.go still uses): a
	// rebuilt prior carries no tenant declarations, so the mtime fast-path
	// would drop every unchanged tenant from the hierarchy on every quiet
	// tick.
	tree *treeScan
}

// hierarchyState bundles the v2.7.0+ ADR-016/017 hierarchical-mode caches.
// `enabled` is auto-detected on first load: if the tree walk finds at
// least one `_defaults.yaml` at any depth AND the top-level scan path is a
// directory, we keep hierarchical state populated alongside the flat
// state. A reload always produces both views from ONE walk (#1568), so
// the two can no longer disagree about which files exist.
//
// When `enabled` is false, all maps/graph are nil and diffAndReload falls
// back to IncrementalLoad (flat path).
//
// All fields atomic-swap together at the end of diffAndReload under
// `ConfigManager.mu.Lock()`. A desync between, e.g., `hashes[X]=H_new`
// and `parsedDefaults[X]=parse(H_old)` would cause
// classifyDefaultsNoOpEffect (Issue #61) to compute against the wrong
// baseline. See `installNewHierarchyState` in config_debounce.go.
//
// Memory: ~432 `_defaults.yaml` × ~2KB parsed dict ≈ 1MB at the
// 1000-tenant baseline; scales linearly with tree size.
type hierarchyState struct {
	enabled        bool
	tenantSources  map[string]string         // tenantID → absolute tenant file path
	hashes         map[string]string         // absolute Clean path → 64-char SHA-256
	mergedHashes   map[string]string         // tenantID → 16-char merged_hash
	graph          *InheritanceGraph         // defaults↔tenants dependency map
	parsedDefaults map[string]map[string]any // absolute Clean path → parsed defaults dict

	// unreachableInherited is tenantID → sorted keys that the tenant's
	// subtree defaults chain supplies but that NO emitter can iterate
	// (absent from both `cfg.Defaults` and `cfg.OptionalOverrides`).
	//
	// ⛔ It lives HERE, next to tenantSources, so the undeliverable audit reads
	// both from one lock window. Reloads are not serialised, and pairing
	// reload N's config with reload N+1's diagnosis is exactly the failure
	// the tenantSources snapshot exists to prevent. (#1569)
	unreachableInherited map[string][]string
}

// debouncerState bundles the v2.7.0 Phase 3 burst-coalescing fields.
// `window` controls fsnotify storm absorption (50-200ms K8s ConfigMap
// symlink rotation + safety margin). 0 disables debouncing and restores
// the v2.6.0 behavior (immediate reload per detected diff).
//
// `mu` is a separate mutex from `ConfigManager.mu` so a long-running
// reload doesn't block trigger registration; trigger accumulation is
// fast (just append to pendingReasons + reset timer).
type debouncerState struct {
	window         time.Duration
	timer          clockwork.Timer // current pending reload; nil when idle
	mu             sync.Mutex      // guards timer + pendingReasons
	pendingReasons []string        // accumulates reload triggers during a window
	fired          uint64          // count of fires; read via DebounceFiredCount()
}

// ConfigManager handles loading and hot-reloading the config.
// Supports two modes:
//   - Single-file mode (legacy): reads one YAML file
//   - Directory mode: scans all *.yaml files in a directory and deep-merges
//
// In directory mode, ConfigManager supports incremental hot-reload (v2.1.0):
// per-file SHA-256 tracking + parsed config cache → only changed files are
// re-parsed on each reload cycle, then all cached partials are merged.
type ConfigManager struct {
	path       string // file path or directory path
	isDir      bool   // true = directory mode
	mu         sync.RWMutex
	config     *ThresholdConfig
	loaded     bool
	lastReload time.Time
	lastHash   string // SHA-256 composite hash for change detection

	// Config info metric state (v2.3.0)
	configSource string // "configmap", "operator", or "git-sync"
	gitCommit    string // git commit hash from .git-revision file, or ""

	// afterCommitUnlock is a TEST-ONLY seam fired by commitConfig as the
	// first statement after installConfig releases m.mu (#1521). Nil in
	// production. Read inside that same lock window so a concurrent
	// SetAfterCommitUnlockForTest can never race the read; per-manager
	// rather than package-level so `t.Parallel()` tests do not share it.
	afterCommitUnlock func()

	// undeliverable tracks what the subtree-undeliverable audit last put in
	// the log, so a persistent condition is stated once per change instead of
	// once per config commit. See config_subtree_undeliverable.go.
	undeliverable undeliverableLogState

	// clock abstracts time.NewTicker / time.AfterFunc so tests can drive
	// the WatchLoop ticker + debounce timer deterministically with a
	// clockwork.FakeClock instead of time.Sleep'ing for real wall-clock
	// elapses. Production constructors plug in clockwork.NewRealClock();
	// test code uses SetClock to swap in a FakeClock and then
	// `Advance(window)` to fire timers synchronously. Origin: TRK-011
	// deeper applied to exporter (mirrors PR #354 in tenant-api).
	clock clockwork.Clock

	// metrics is this manager's *configMetrics instance. Production
	// constructors plug in the package-level singleton via
	// getConfigMetrics() so production behavior is unchanged. Tests can
	// swap a fresh instance via SetMetrics for parallel-safe observation
	// — foundation for #4a (drop withIsolatedMetrics global swap).
	//
	// Until follow-up PRs convert all ConfigManager methods + scanner
	// signatures to use this field, the global getConfigMetrics()
	// helpers are still in use by callsites. The field exists today
	// as the injection seam.
	metrics *configMetrics

	// logger is this manager's *log.Logger instance. Production
	// constructors plug in log.Default() so production behavior is
	// unchanged (every log.Printf in the package code today writes to
	// log.Default's writer; routing through m.logger is a no-op for
	// production). Tests can swap a per-test logger writing to a
	// captured bytes.Buffer via SetLogger for parallel-safe log
	// capture — foundation for #4b (drop log.SetOutput global swap).
	//
	// Until follow-up PRs convert all log.Printf callsites in package
	// methods to m.getLogger().Printf and add WithLogger variants for
	// scanner free functions, the package-level log.Default() is still
	// the routing destination. The field exists today as the injection
	// seam.
	logger *log.Logger

	// freeOSMemAfterReload, when true, makes the reload pipeline call
	// runtime/debug.FreeOSMemory() after each diffAndReload so the Go
	// runtime returns idle heap pages to the OS instead of holding the
	// high-water mark. Opt-in lever for #459 (sys_bytes / heap_idle creep
	// under sustained reload); default false = unchanged behavior. Set
	// once at startup from the -free-os-mem-after-reload flag, before
	// WatchLoop starts.
	freeOSMemAfterReload bool

	// Sub-struct field groups — v2.8.0 PR-5 decomposed the original
	// 14-mixed-fields ConfigManager into named concerns. Field accesses
	// across the codebase use `m.flat.X` / `m.hierarchy.X` / `m.debounce.X`
	// for clarity at the call site.
	flat      flatScanState
	hierarchy hierarchyState
	debounce  debouncerState
}

// DefaultDebounceWindow is the default burst-coalescing window applied by
// NewConfigManager. Chosen to match fsnotify storms from K8s ConfigMap volume
// symlink rotation (~50-200ms) with a safety margin; tunable via the
// --scan-debounce flag (see main.go) and overridable for tests via
// NewConfigManagerWithDebounce.
const DefaultDebounceWindow = 300 * time.Millisecond

func NewConfigManager(path string) *ConfigManager {
	return NewConfigManagerWithDebounce(path, DefaultDebounceWindow)
}

// NewConfigManagerWithDebounce constructs a ConfigManager with a custom
// debounce window. Pass 0 to disable debouncing (WatchLoop reloads
// synchronously on every detected diff, matching v2.6.0 behavior). Used by
// tests to inject a 1ms window for deterministic batch assertions.
func NewConfigManagerWithDebounce(path string, debounceWindow time.Duration) *ConfigManager {
	info, err := os.Stat(path)
	isDir := err == nil && info.IsDir()

	return &ConfigManager{
		path:     path,
		isDir:    isDir,
		clock:    clockwork.NewRealClock(),
		metrics:  getConfigMetrics(),
		logger:   log.Default(),
		debounce: debouncerState{window: debounceWindow},
	}
}

// SetClock swaps the clock used by WatchLoop's ticker + the debounce
// timer. Test-only — production code constructs managers via
// NewConfigManagerWithDebounce which installs a real clock. Calling
// this AFTER WatchLoop has started is undefined (the ticker is bound
// to whichever clock was current at NewTicker time).
func (m *ConfigManager) SetClock(c clockwork.Clock) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.clock = c
}

// SetMetrics swaps the *configMetrics instance this manager bumps.
// Test-only — production code receives the package-level singleton
// from the constructor. Tests use this to inject a fresh instance for
// parallel-safe observation; mirrors SetClock. Foundation for #4a.
func (m *ConfigManager) SetMetrics(metrics *configMetrics) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.metrics = metrics
}

// getMetrics returns m.getMetrics(), lazy-initializing to the global
// singleton if the constructor was bypassed (test struct-literal
// pattern). Always returns a non-nil pointer so callers can write
// `m.getMetrics().IncReloadTrigger(...)` without nil-panic worry.
func (m *ConfigManager) getMetrics() *configMetrics {
	m.mu.RLock()
	mm := m.metrics
	m.mu.RUnlock()
	if mm != nil {
		return mm
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.metrics == nil {
		m.metrics = getConfigMetrics()
	}
	return m.metrics
}

// SetLogger swaps the *log.Logger this manager writes Printf calls to.
// Test-only — production code receives log.Default() from the
// constructor. Tests use this to inject a per-test logger writing to a
// captured bytes.Buffer for parallel-safe log capture; mirrors
// SetMetrics. Foundation for #4b.
func (m *ConfigManager) SetLogger(logger *log.Logger) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.logger = logger
}

// SetAfterCommitUnlockForTest installs a callback fired by commitConfig
// as the FIRST statement after installConfig releases m.mu (#1521).
//
// ⛔ Test-only, and narrow on purpose. It exists to make one specific
// regression observable: the commit-time audit (config_subtree_undeliverable.go,
// the #1521 divergence audit until #1957) must judge the state it
// just installed against the hierarchy AS IT STOOD IN THAT LOCK WINDOW,
// not against whatever the live manager holds by the time the audit runs.
// Adversarial review measured that moving that read back outside the lock
// left the entire package green, so the invariant had no test at all. A
// callback here lets a test change the live hierarchy in exactly the
// instant that separates the two implementations.
//
// Per-manager rather than package-level so `t.Parallel()` tests never
// share it, and read back under m.mu (in installConfig) so setting it
// cannot race the read.
func (m *ConfigManager) SetAfterCommitUnlockForTest(fn func()) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.afterCommitUnlock = fn
}

// getLogger returns m.logger, lazy-initializing to log.Default() if
// the constructor was bypassed (test struct-literal pattern). Always
// returns a non-nil pointer so callers can write
// `m.getLogger().Printf(...)` without nil-panic worry. Mirrors
// getMetrics.
func (m *ConfigManager) getLogger() *log.Logger {
	m.mu.RLock()
	lg := m.logger
	m.mu.RUnlock()
	if lg != nil {
		return lg
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.logger == nil {
		m.logger = log.Default()
	}
	return m.logger
}

// SetFreeOSMemAfterReload toggles the post-reload runtime/debug.FreeOSMemory()
// call (#459). Production wires this from the -free-os-mem-after-reload flag
// in main(); call it before WatchLoop starts. Default (never called) is
// false, preserving the pre-#459 behavior where the Go runtime decides
// return-to-OS pacing on its own.
func (m *ConfigManager) SetFreeOSMemAfterReload(enabled bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.freeOSMemAfterReload = enabled
}

// freeOSMemEnabled reads the lever under RLock. Mirrors getMetrics/getLogger:
// the flag is set once before WatchLoop starts, but the read happens on the
// reload goroutine, so the RLock keeps the race detector quiet against a
// test that toggles it concurrently.
func (m *ConfigManager) freeOSMemEnabled() bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.freeOSMemAfterReload
}

// Mode returns "directory" or "single-file" for diagnostics.
func (m *ConfigManager) Mode() string {
	if m.isDir {
		return "directory"
	}
	return "single-file"
}

// commitConfig installs a freshly-loaded ThresholdConfig + composite
// hash into the manager under m.mu.Lock, then runs the post-commit
// hooks (config-source detection + stats log). Shared by Load,
// fullDirLoad, and IncrementalLoad — every loader's "atomic swap"
// step now goes through this single seam.
//
// flatScan.hashes != nil signals "I have a new flat-scan snapshot to
// install"; nil leaves m.flat untouched (single-file Load path). The
// snapshot is shallow-copied into m.flat — caller may mutate after
// commitConfig returns.
//
// logHeader is the human-readable banner inserted into the
// "Config loaded (...)" log line.
func (m *ConfigManager) commitConfig(cfg *ThresholdConfig, hash string, flatScan *flatScanState, logHeader string) {
	// Detect config source mode and git commit (v2.3.0). Refreshed on
	// every commit because git-sync may rotate .git-revision between
	// reloads. Done BEFORE the lock so the .git-revision disk read never
	// blocks scrape readers; the results are then written under m.mu so
	// GetConfigInfo (RLock) never observes an unsynchronised write, and
	// config + config-info land in one consistent lock window.
	configSource, gitCommit := m.detectConfigSource()

	hierTenantSources, unreachableInherited, afterUnlock := m.installConfig(
		cfg, hash, flatScan, configSource, gitCommit)
	if afterUnlock != nil {
		// Test-only seam, and it must stay the FIRST statement after the
		// swap: it exists so a test can mutate the live hierarchy in the
		// one instant that separates "snapshot taken inside the lock" from
		// "snapshot taken afterwards". Anything inserted above it that
		// reads m.hierarchy would slip past the check.
		afterUnlock()
	}

	logConfigStats(m.getLogger(), cfg, logHeader)

	// #1521: the flat scanner that produced `cfg` is not recursive while
	// the hierarchical scanner behind /effective is. Compare the two
	// tenant populations here — this is the only site in the package that
	// assigns m.config, so hooking the audit in means every publishing
	// path (Load, fullDirLoad, IncrementalLoad, and diffAndReload via
	// installNewHierarchyState → fullDirLoad) is covered by construction
	// rather than by remembering to add a call. Observability only: it
	// never fails the commit — see config_subtree_undeliverable.go for why not.
	m.auditSubtreeUndeliverable(hierTenantSources, unreachableInherited, logHeader)
}

// installConfig performs the atomic swap under m.mu and RETURNS the
// hierarchical tenant population as it stood at that instant, together
// with the test-only after-unlock hook read in the same window.
//
// ⛔ The first return value is the whole reason this function exists, and
// the reason it is a return value rather than a comment. An earlier
// revision assigned m.config here and let the caller read
// m.hierarchy.tenantSources afterwards, outside the lock. Reloads are not
// serialised — `fireDebounced` sets `debounce.timer = nil`, unlocks, and
// only then calls diffAndReload — so the audit could pair reload N's cfg
// with reload N+1's hierarchy and name a HEALTHY tenant as having no
// metrics, from the one check whose entire value is being trustworthy
// about that.
//
// ⛔ The fix was originally a comment saying "read this inside the lock",
// and adversarial review measured what that was worth: moving the read
// back out to its own RLock left the whole package green — no test could
// tell. Returning the snapshot moves the invariant into the signature,
// where deleting it is an edit a reviewer sees rather than one that looks
// like tidying. `TestDivergenceAudit_SnapshotIsTakenInsideTheLockWindow`
// covers the remaining shape (a re-read placed after the swap).
func (m *ConfigManager) installConfig(
	cfg *ThresholdConfig, hash string, flatScan *flatScanState,
	configSource, gitCommit string,
) (hierTenantSources map[string]string, unreachable map[string][]string, afterUnlock func()) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.config = cfg
	m.loaded = true
	m.lastReload = time.Now()
	m.lastHash = hash
	if flatScan != nil {
		m.flat = *flatScan
	}
	m.configSource = configSource
	m.gitCommit = gitCommit
	return m.hierarchy.tenantSources, m.hierarchy.unreachableInherited, m.afterCommitUnlock
}

// rejectDuplicateTenant is the issue-#127 hard reject shared by every full
// load: a tenant declared in two files is a misconfiguration, and the
// caller propagates the typed error without committing any state. The
// walker records the conflict on the scan instead of failing the walk, so
// the flat products of a rejected tree are still available for diagnosis;
// this is the one place that turns the record back into the error callers
// unwrap with errors.As(&DuplicateTenantError{}).
//
// Historically the hierarchical scan was a SECOND walk that could also
// fail on its own (its non-duplicate errors were logged as
// "WARN: hierarchical scan during <loader> failed" and ignored, because
// hierarchical mode is opt-in). With one walk there is no second failure
// to tolerate: a tree the flat plane cannot read is a hard error for the
// load, as it always was.
func rejectDuplicateTenant(scan *treeScan) error {
	if scan.Conflict != nil {
		return fmt.Errorf("config rejected (mixed-mode duplicate tenant): %w", scan.Conflict)
	}
	return nil
}

// Load loads config from either a single file or a directory.
//
// Directory mode delegates to the single fullDirLoad path also used by the
// watch loop and IncrementalLoad's cold-start fallback. Sharing it means the
// initial commit uses the same composite-hash construction (scanDirTree
// hash-of-hashes) that the first watch tick recomputes — so the first tick no
// longer sees a phantom change against a differently-built byte composite — and
// the flat cache plus the retained scan are populated up front so the next
// tick can take the mtime fast-path instead of a full rebuild. fullDirLoad
// already runs the same ApplyProfiles + issue-#127 duplicate-tenant reject +
// commitConfig sequence this path used to inline (mergePartialConfigs
// initialises every map).
func (m *ConfigManager) Load() error {
	if m.isDir {
		return m.fullDirLoad()
	}

	cfg, hash, err := loadFile(m.path)
	if err != nil {
		return err
	}

	// Ensure maps are initialized (single-file loadFile may leave them nil).
	if cfg.Defaults == nil {
		cfg.Defaults = make(map[string]float64)
	}
	if cfg.Tenants == nil {
		cfg.Tenants = make(map[string]map[string]ScheduledValue)
	}
	if cfg.StateFilters == nil {
		cfg.StateFilters = make(map[string]StateFilter)
	}
	if cfg.Profiles == nil {
		cfg.Profiles = make(map[string]map[string]ScheduledValue)
	}

	// Expand profile values into tenant overrides (v1.12.0)
	cfg.ApplyProfiles()

	m.commitConfig(&cfg, hash, nil, fmt.Sprintf("Config loaded (%s)", m.Mode()))
	return nil
}

// anyNestedKey reports whether any scan key names a file below the conf.d
// root. Keys are root-relative slash paths since #1521, so a separator IS the
// test — no path parsing required.
func anyNestedKey(groups ...[]string) bool {
	for _, g := range groups {
		for _, name := range g {
			if strings.Contains(name, "/") {
				return true
			}
		}
	}
	return false
}

// IncrementalLoad performs an incremental reload in directory mode.
// It compares per-file hashes with the cached state, re-parses only
// changed/added files, removes deleted files from cache, then rebuilds
// the merged config from cached partials.
//
// Falls back to full Load() for single-file mode or first-time load.
func (m *ConfigManager) IncrementalLoad() error {
	// Single-file mode or first load: fall back to full Load
	if !m.isDir {
		return m.Load()
	}

	m.mu.RLock()
	hasCache := len(m.flat.hashes) > 0
	m.mu.RUnlock()

	if !hasCache {
		return m.fullDirLoad()
	}

	// Phase 1: ONE walk with the mtime guard (cheap — stat + skip unchanged).
	// The prior is the retained scan, never a rebuilt one (see flatScanState).
	m.mu.RLock()
	tree := m.flat.tree
	m.mu.RUnlock()

	scan, err := scanDirTree(m.path, tree, m.getMetrics(), m.getLogger())
	if err != nil {
		return err
	}
	return m.incrementalLoadFrom(scan)
}

// incrementalLoadFrom is IncrementalLoad from phase 2 on, fed a scan the
// caller already has. scanAndCheckHierarchical (config_debounce.go) calls
// it with the scan it took to decide the path, so a flat-mode reload tick
// walks the tree once instead of twice. The caller has already checked
// that a flat cache exists.
//
// ⛔ EVERY KEY BELOW IS A ROOT-RELATIVE SLASH PATH (scan.relHashes()), never
// the hierarchy plane's absolute keys: `anyNestedKey` and
// `isNestedPlatformFile` read a separator as "below the root", so an
// absolute key would make every file nested, redirect every reload into
// fullDirLoadFrom, and say nothing — no error, no WARN.
func (m *ConfigManager) incrementalLoadFrom(scan *treeScan) error {
	m.mu.RLock()
	prevHash := m.lastHash
	m.mu.RUnlock()

	// Quick check FIRST: composite hash unchanged → no work needed. The
	// three projection maps below are ~1000 entries each on a 1000-tenant
	// tree; building them before this check charged every quiet tick for
	// maps it then threw away (the bench gate's +37% bytes on
	// IncrementalLoad_1000_NoChange_MtimeGuard).
	compositeHash := scan.Composite
	if compositeHash == prevHash {
		return nil
	}
	newHashes, newMtimes, dataCache := scan.RelHashes(), scan.RelMtimes(), scan.DataCache()

	// ⛔ A SCAN THAT FINDS NOTHING IS AN ERROR, NEVER AN EMPTY CONFIG.
	// `fullDirLoad` has always treated `len(perFileHashes) == 0` as a hard
	// error; this path did not, and the asymmetry was a silent total outage.
	// `diffFileHashes` classifies every known file as REMOVED, the merge of
	// nothing commits cleanly, and the watch loop logs
	// "Config reloaded (incremental, 0 changed, 0 added, N removed)" at INFO.
	// Measured on a symlinked `-config-dir` re-pointed at a directory holding
	// no YAML — an ordinary step of a blue/green or `..data` swap, no race
	// needed: `IncrementalLoad` returned nil and `GetConfig().Tenants` went
	// from 1 to 0. Every tenant's thresholds vanish, every alert stops firing,
	// and nothing says so. (#1569 blind review.)
	//
	// Returning an error keeps the PREVIOUS config live, which is the
	// fail-safe direction: a stale threshold still protects, an absent one
	// does not.
	if len(newHashes) == 0 {
		return fmt.Errorf(
			"no .yaml files found in %s during incremental reload — refusing to "+
				"commit an empty config (previous config kept)", m.path)
	}

	// Phase 2: diff per-file hashes → identify changed/added/removed
	m.mu.RLock()
	oldHashes := m.flat.hashes
	oldConfigs := m.flat.configs
	m.mu.RUnlock()

	changed, added, removed := diffFileHashes(oldHashes, newHashes)

	// ⛔ #1521: a change under a SUBDIRECTORY takes the full path. This one is
	// not about the merge — it is about `m.hierarchy`, which IncrementalLoad
	// never refreshes (measured: zero references to it in this function, and
	// `tenantSources` has exactly two writers, neither on this path). A nested
	// tenant added here would land in the config with no inheritance chain
	// known for it, so `applySubtreeDefaults` could not give it the subtree
	// values `/effective` reports — presence without the right number, which is
	// the residual this ticket exists to close.
	//
	// ⚠️ Cost stated rather than hidden: nested trees lose the tenant-patch
	// fast path entirely. Flat trees — every deployment that never nests — are
	// unaffected, because no key contains a separator. The full load is fed
	// THIS scan (its hierarchy products are already there), not a second walk.
	if anyNestedKey(changed, added, removed) {
		return m.fullDirLoadFrom(scan)
	}
	// ⛔ #1674: an unselected root carrier is never parsed or cached
	// (isUnselectedRootCarrier), and this path only re-parses changed/added
	// files — so a carrier that BECOMES selected (`_defaults.yml` once its
	// `_defaults.yaml` sibling is deleted) would be missing from the cache it
	// merges, and one that is unselected would be parsed and merged here. The
	// full load re-reads whatever is uncached and skips the unselected.
	//
	// ⚠️ NARROW ON PURPOSE: only when a root carrier was added or removed
	// (the selection can move) or the root holds more than one (there is an
	// unselected one). An ordinary edit of the root's only `_defaults.yaml`
	// keeps the incremental full-rebuild branch below — redirecting every
	// such edit changed which hierarchy state that branch leaves behind
	// (measured: TestAnUnparseableFileKeepsItsTenantAttributed went red).
	// The scan's selection is only consulted when a root carrier moved, so a
	// tenant-only reload pays nothing for it.
	if anyRootCarrierKey(changed, added, removed) &&
		(anyRootCarrierKey(added, removed) || len(scan.DefaultsCarriers().Ambiguous[scan.AbsRoot]) > 1) {
		return m.fullDirLoadFrom(scan)
	}

	// Copy cache for mutation — deferred until after diff to avoid
	// unnecessary allocation when the per-file diff shows no changes
	// (composite hash collision or race condition edge case).
	newConfigs := make(map[string]ThresholdConfig, len(oldConfigs))
	for k, v := range oldConfigs {
		newConfigs[k] = v
	}

	// Phase 3: re-parse only changed + added files.
	// Reuse file bytes from scan phase (dataCache) to avoid double disk read.
	// ⛔ THE COPY IS LOAD-BEARING. `append(changed, added...)` reuses
	// `changed`'s backing array whenever it has spare capacity, and the sort
	// below then reorders `changed` IN PLACE. `patchFiles` further down rebuilds
	// the same list and got `[a.yaml, a.yaml]` — the changed file was never
	// applied. Measured on one reload that both adds a file and edits another:
	// `flat.configs["b.yaml"]` held the new value while the published config
	// kept the old one, with no log and no divergence ERROR; a restart gave the
	// new value. That is a K8s ConfigMap swap or a git-sync pull carrying two
	// files, which is the ordinary case.
	//
	// ⚠️ IT ONLY REPRODUCES WITH OPTIMISATIONS ON. Under `-gcflags=all=-N -l`
	// or `-race` the capacity works out differently and the alias disappears —
	// so does adding a `Printf` anywhere nearby. That is why nine rounds of
	// tests never saw it: it is invisible to exactly the builds a person
	// reaches for when investigating. Found by a randomised fast-path-vs-full-
	// load differential (9 of 300 seeds), not by reading. (#1569 blind review.)
	reparse := append(append([]string{}, changed...), added...)
	sort.Strings(reparse)
	for _, name := range reparse {
		// Rejected by the walker on this scan: already logged and counted
		// there (see commitFlatFrom). Dropped from the cache so the merge
		// below treats it as the full load does.
		if f := scan.Files[name]; f != nil && f.ParseFailed {
			delete(newConfigs, name)
			continue
		}
		// The walker's decode of this file, when it parsed one (#1957; see
		// commitFlatFrom). A changed or added tenant file always has one
		// unless its parse failed: its hash moved against the prior, so the
		// walker could not carry it.
		if partial, ok := scan.Partials[name]; ok {
			applyBoundaryRules(name, &partial, m.getLogger())
			newConfigs[name] = partial
			continue
		}
		fullPath := filepath.Join(m.path, name)
		data, ok := dataCache[name]
		if !ok {
			// Fallback: file not in cache (shouldn't happen, but be safe)
			var rerr error
			data, rerr = os.ReadFile(fullPath)
			if rerr != nil {
				m.getLogger().Printf("WARN: skip unreadable file %s: %v", fullPath, rerr)
				delete(newConfigs, name)
				continue
			}
		}
		// ⛔ A nested `_` file is scanned (change detection must see it) but
		// contributes NOTHING to the merged config. `ThresholdConfig.Defaults`
		// is ONE global map with no subtree scope, and the merge is
		// last-writer-wins over sorted keys — so `nested/_defaults.yaml` sorts
		// after the root's and would re-price every tenant in the tree,
		// including tenants in unrelated subtrees. Measured; see
		// `TestASubtreeDefaultNeverLeaksIntoTheGlobalOnes`.
		//
		// ⚠️ UNREACHABLE TODAY and kept deliberately: `anyNestedKey` above
		// redirects any change under a subdirectory to `fullDirLoad`, so no
		// nested key reaches this loop. It stays as the second line of the
		// same defence — if that redirect is ever narrowed, the leak it
		// prevents is silent. Measured unreachable: a `panic` in this branch
		// does not fire across the whole package suite.
		//
		// ⛔ NOT `parsePartialConfig`, BUT NOT SILENT EITHER. Running the full
		// parse here logs `ERROR: skip unparseable defaults/profiles file …`
		// for a tree that is entirely valid: `Defaults` is
		// `map[string]float64`, so a subtree defaults file in the SCHEDULE
		// form (`{default: "90", overrides: […]}`) — which the hierarchical
		// plane accepts and `/effective` renders — cannot decode into it.
		// Skipping outright, though, dropped the parse-failure counter and the
		// ERROR for files that are GENUINELY broken (measured: a nested
		// `_defaults.yaml` containing `defaults: [this is not a map` scored 0
		// on the counter and produced no ERROR — a severity downgrade the
		// recursion introduced). The probe below separates the two: a syntax
		// error is still counted and still loud; content this plane simply
		// does not want is skipped in silence. (#1569 blind review.)
		if isNestedPlatformFile(name) {
			reportUnparseableNestedPlatformFile(fullPath, data, m.getMetrics(), m.getLogger())
			reportNestedPlatformTenants(fullPath, data, m.getLogger())
			delete(newConfigs, name)
			continue
		}
		partial, ok := parsePartialConfig(name, fullPath, data, m.getMetrics(), m.getLogger())
		if !ok {
			delete(newConfigs, name)
			continue
		}
		// Apply boundary enforcement (same rules as fullDirLoad)
		applyBoundaryRules(name, &partial, m.getLogger())
		newConfigs[name] = partial
	}

	// Remove deleted files from cache
	for _, name := range removed {
		delete(newConfigs, name)
	}

	// Phase 4: merge — incremental tenant patch when only tenant files changed,
	// full rebuild when a _defaults/_profiles/_state_filters file changed.
	var merged ThresholdConfig
	// ⛔ #1569: THIS PATH MUST REFRESH THE REFUSED-KEY SET TOO. `m.hierarchy`
	// is not re-scanned here (a nested change was redirected to `fullDirLoad`
	// above), but `cfg.Defaults` CAN change on this path — a root
	// `_defaults.yaml` edit is a flat key — and the refused set is derived
	// from it. Leaving the field alone made the audit report a set belonging
	// to an earlier config: measured, the gauge stayed at 1 after the tree was
	// repaired and only a later full `Load()` cleared it.
	//
	// ⚠️ Latent rather than live: the production watch path reaches
	// `fullDirLoad` via `installNewHierarchyState`, so a real deployment
	// refreshes it. Fixed anyway — the asymmetry between the two fields
	// `installConfig` returns is exactly the kind that becomes live later.
	refreshRefused := func(merged *ThresholdConfig) {
		m.mu.RLock()
		var td map[string][]string
		if m.hierarchy.graph != nil {
			td = m.hierarchy.graph.TenantDefaults
		}
		pd := m.hierarchy.parsedDefaults
		m.mu.RUnlock()
		_, unreachable := applySubtreeDefaults(merged, m.path, td, pd)
		m.mu.Lock()
		m.hierarchy.unreachableInherited = unreachable
		m.mu.Unlock()
	}

	// ⛔ ITS SIBLING FIELD WENT STALE THE SAME WAY, AND THAT ONE IS LIVE.
	// The block above fixed `unreachableInherited` and named the risk —
	// "the asymmetry between the two fields `installConfig` returns is exactly
	// the kind that becomes live later". It already was. `tenantSources` is
	// the population /effective serves (and the one the commit-time audit
	// iterates), so a tenant that leaves the tree lingered there — still
	// resolvable while /metrics had dropped it. Until #1957 the audit then
	// reported it as a scanner divergence pointing at a parse-failure line
	// that did not exist, because nothing was broken: the operator deleted
	// the tenant. Measured both ways it can leave: removing one of two root
	// tenant files, and emptying a root file that stays on disk.
	//
	// ⛔ THE THREE CASES ARE TOLD APART BY THIS ROUND'S PARSE RESULT.
	// `newHashes` says whether the file is still on disk and `newConfigs` says
	// whether it parsed this round (upstream deletes the entry when it did
	// not), so:
	//
	//	gone from disk                 → prune (operator deleted the file)
	//	on disk, parsed, no longer declares the tenant → prune (operator deleted the tenant)
	//	on disk, did NOT parse         → keep exactly while the merged config keeps it
	//
	// ⛔ THE THIRD ROW FOLLOWS THE MERGED CONFIG, NOT THE FILE (#1957). A file
	// that fails the one decode (config.ParseConfigFile) declares no tenant on
	// the walker's verdict, and a full load drops its tenants from BOTH planes.
	// This path may instead keep them in the merged config — patchTenants'
	// "keep the last good values" on the tenant-only branch — or drop them —
	// the full-rebuild branch. Whichever it did, /effective must answer for
	// the same tenant set /metrics serves: before #1957 this row was "KEEP,
	// so cause (a) still fires", i.e. the rule deliberately MADE the two
	// planes disagree so the divergence audit could report it.
	//
	// Additions are attributed from the flat scan rather than left blank: a
	// tenant absent from `tenantSources` is invisible to /effective while
	// /metrics serves it. Never OVERWRITES an existing attribution — where
	// the two disagree about which file owns a tenant (a duplicate the fast
	// path accepts), the hierarchical one stands.
	refreshTenantSources := func() {
		scanRoot := absScanRoot(m.path)
		scanKey := func(absPath string) (string, bool) {
			rel, err := filepath.Rel(scanRoot, filepath.Clean(absPath))
			if err != nil {
				return "", false
			}
			return filepath.ToSlash(rel), true
		}
		m.mu.Lock()
		defer m.mu.Unlock()
		if m.hierarchy.tenantSources == nil {
			return // the hierarchical scan never ran; nothing to keep honest
		}
		next := make(map[string]string, len(m.hierarchy.tenantSources))
		for tid, src := range m.hierarchy.tenantSources {
			key, ok := scanKey(src)
			if !ok {
				next[tid] = src // cannot relate it to the scan — leave it alone
				continue
			}
			if _, onDisk := newHashes[key]; !onDisk {
				continue // the file is gone
			}
			if partial, parsed := newConfigs[key]; parsed {
				if _, declared := partial.Tenants[tid]; !declared {
					continue // the file parsed and no longer names this tenant
				}
			} else if _, served := merged.Tenants[tid]; !served {
				continue // the file failed to parse and the merged config dropped it
			}
			next[tid] = src
		}
		for key, partial := range newConfigs {
			// ⛔ Tenant files only. A root platform file's `tenants:` block
			// supplies per-tenant DEFAULTS and cannot attribute (or create)
			// a tenant: before this, an entry naming a tenant no tenant file
			// declares was attributed to `_defaults.yaml` here, so the
			// incremental path made /effective answer for a tenant the full
			// load never locates.
			if isPlatformKey(key) {
				continue
			}
			for tid := range partial.Tenants {
				if _, known := next[tid]; known {
					continue
				}
				next[tid] = filepath.Join(scanRoot, filepath.FromSlash(key))
			}
		}
		m.hierarchy.tenantSources = next
	}

	// Existence is THIS scan's verdict, on both branches (see
	// declaredTenantIDs): the patch branch can orphan a platform entry too,
	// by removing the only tenant file that declared the tenant.
	//
	// ⛔ ON THE PATCH BRANCH, "EXISTS" IS WHAT THIS RELOAD WILL SERVE. A
	// tenant file that fails to parse keeps its tenants' last good values
	// there (patchTenants' fail-safe, #1980), platform-supplied keys
	// included; the scan has no declaration for it, so without this the
	// orphan WARN named a tenant the same commit kept serving with the
	// platform value. The full-rebuild branch drops such a tenant, so there
	// the scan's verdict alone is right.
	exists := tenantExistenceFor(newConfigs, scan)
	patchBranch := isTenantOnlyChange(changed, added, removed) && m.config != nil
	if patchBranch && exists != nil {
		for _, tid := range m.failSafeHeldTenants(newHashes, newConfigs) {
			exists[tid] = struct{}{}
		}
	}
	reportPlatformOrphans(newConfigs, exists, m.getLogger())
	if patchBranch {
		// Incremental patch: copy existing merged config, patch only affected
		// tenants. Avoids the O(N) merge for the common "1 tenant file changed"
		// case. prev is read under the lock; patchTenants is otherwise pure.
		m.mu.RLock()
		prev := m.config
		m.mu.RUnlock()
		merged = patchTenants(prev, newConfigs, oldConfigs, changed, added, removed, exists)
	} else {
		// Full rebuild: _defaults or _profiles changed, must re-merge everything
		merged = mergePartialConfigs(newConfigs, exists)
	}
	// ⛔ BOTH BRANCHES, NOT JUST THE REBUILD. `ApplyProfiles` used to sit inside
	// the else above, so the tenant-patch path published tenants exactly as
	// their files spell them — without the profile overlay `Resolve` expects to
	// find already merged in. Measured on a tenant whose `_profile: gold` supplies
	// mysql_connections=95: adding an unrelated key to that tenant's own file
	// dropped it to the platform default 80, silently, until a restart put it
	// back. A threshold LOOSENED with no signal is this ticket's own shape.
	//
	// Hoisted out of the branch rather than duplicated into it: two call sites
	// is how it drifted in the first place. Re-running is safe — ApplyProfiles
	// is fill-in-not-overwrite, so a tenant that already carries the key keeps
	// its own value. (#1569 blind review.)
	merged.ApplyProfiles()
	refreshRefused(&merged)
	refreshTenantSources()

	scan.ReleaseData()
	m.commitConfig(&merged, compositeHash, &flatScanState{
		hashes:  newHashes,
		configs: newConfigs,
		mtimes:  newMtimes,
		tree:    scan,
	}, fmt.Sprintf("Config reloaded (incremental, %d changed, %d added, %d removed)", len(changed), len(added), len(removed)))
	return nil
}

// failSafeHeldTenants lists the tenants the patch branch keeps serving on
// last good values: attributed (tenantSources) to a file that is still on
// disk (newHashes) but did not parse this round (absent from newConfigs) —
// the "on disk, did NOT parse" row refreshTenantSources keeps. Read from
// the attribution BEFORE this reload refreshes it, because that is the
// population m.config (the patch's prev) was built for.
func (m *ConfigManager) failSafeHeldTenants(newHashes map[string]string, newConfigs map[string]ThresholdConfig) []string {
	scanRoot := absScanRoot(m.path)
	m.mu.RLock()
	defer m.mu.RUnlock()
	if m.config == nil {
		return nil
	}
	var out []string
	for tid, src := range m.hierarchy.tenantSources {
		rel, err := filepath.Rel(scanRoot, filepath.Clean(src))
		if err != nil {
			continue
		}
		key := filepath.ToSlash(rel)
		if _, onDisk := newHashes[key]; !onDisk {
			continue
		}
		if _, parsed := newConfigs[key]; parsed {
			continue
		}
		if _, served := m.config.Tenants[tid]; served {
			out = append(out, tid)
		}
	}
	return out
}

// diffFileHashes compares the previous and current per-file hash maps and
// classifies each file as changed (hash differs), added (new), or removed
// (gone from the new scan). Pure helper extracted from IncrementalLoad Phase 2.
func diffFileHashes(oldHashes, newHashes map[string]string) (changed, added, removed []string) {
	for name, newHash := range newHashes {
		oldHash, exists := oldHashes[name]
		if !exists {
			added = append(added, name)
		} else if newHash != oldHash {
			changed = append(changed, name)
		}
	}
	for name := range oldHashes {
		if _, exists := newHashes[name]; !exists {
			removed = append(removed, name)
		}
	}
	return changed, added, removed
}

// isTenantOnlyChange reports whether an incremental reload touched only tenant
// files — i.e. no underscore-prefixed file (_defaults.yaml / _profiles.yaml /
// _state_filters). When true, IncrementalLoad can patch just the affected
// tenants instead of re-merging the whole tree. Extracted from Phase 4.
//
// An underscore prefix already subsumes the specific _defaults.yaml /
// _profiles.yaml names, so the prefix test alone is sufficient.
func isTenantOnlyChange(changed, added, removed []string) bool {
	for _, group := range [][]string{changed, added, removed} {
		for _, name := range group {
			// basename: a nested `_defaults.yaml` is still a platform file, and
			// routing it into the tenant-patch fast path would apply a defaults
			// edit as if it were a tenant edit (#1521).
			if strings.HasPrefix(scanKeyBase(name), "_") {
				return false
			}
		}
	}
	return true
}

// indexTenantDeclarations maps each tenant to the filenames declaring it, in
// sortFlatMergeOrder (platform files first — the order mergePartialConfigs
// merges in, so reclaimTenantFrom's "later wins" is the tenant file's win),
// built ONCE per reload.
//
// ⛔ THIS EXISTS BECAUSE THE OBVIOUS VERSION WAS QUADRATIC. `reclaimTenantFrom`
// originally rescanned and re-sorted all of `newConfigs` per patched tenant.
// With one tenant file changed that is invisible — which is the only case the
// cost comment measured — but a reload that rewrites the whole tree is
// O(tenants x files). Measured on 1000 tenant files, all changed:
// `IncrementalLoad` went 182-195 ms against a full `Load` of 80-94 ms on the
// same tree, i.e. the fast path became more than twice as slow as the rebuild
// it exists to avoid. Reachable by anything that regenerates the tree:
// `assemble_config_dir`, a `da-batchpr` sweep, a formatting migration.
// (#1569 blind review.)
func indexTenantDeclarations(newConfigs map[string]ThresholdConfig) tenantDeclarations {
	names := make([]string, 0, len(newConfigs))
	for name := range newConfigs {
		names = append(names, name)
	}
	sortFlatMergeOrder(names)

	idx := tenantDeclarations{single: make(map[string]string, len(newConfigs))}
	for _, name := range names {
		for tenant := range newConfigs[name].Tenants {
			if _, seen := idx.single[tenant]; !seen {
				if _, dup := idx.multi[tenant]; !dup {
					idx.single[tenant] = name
					continue
				}
			}
			// Second or later declaration: promote to the slice form.
			if idx.multi == nil {
				idx.multi = map[string][]string{}
			}
			if first, wasSingle := idx.single[tenant]; wasSingle {
				idx.multi[tenant] = []string{first}
				delete(idx.single, tenant)
			}
			idx.multi[tenant] = append(idx.multi[tenant], name)
		}
	}
	return idx
}

// tenantDeclarations answers "which files declare this tenant" without paying
// for the answer where it is boring.
//
// ⛔ A `map[string][]string` COSTS A SLICE PER TENANT, and a tenant declared
// in two files is invalid — `rejectDuplicateTenant` refuses it on a full load
// — so the slice is waste on essentially every entry. Measured at 1000 tenants:
// the slice-per-tenant form added ~1005 allocs/op to every incremental reload
// (16242 → 17247 on BenchmarkIncrementalLoad_1000_OneFileChanged); splitting
// the rare case out brings it back. (#1569 blind review.)
type tenantDeclarations struct {
	single map[string]string   // tenant → its only declaring file
	multi  map[string][]string // tenant → sorted files, only when >1 declares it
}

// reclaimTenantFrom returns the overrides the surviving files declare for this
// tenant, and whether any file does. `sources` comes from
// indexTenantDeclarations and is in `mergePartialConfigs` order.
//
// ⛔ "IS IT STILL DECLARED" IS HALF THE QUESTION; THE OTHER HALF IS "WHOSE
// VALUE". An earlier version answered only the first, by consulting
// `patchedTenants` — the tenants reintroduced by files reparsed THIS round.
// That is "did it move in this reload", a different question again: a tenant
// declared in two files at once (invalid, hard-rejected by
// `rejectDuplicateTenant` on a full load, but silently accepted by this fast
// path) vanished the moment either owning file was edited, because the other
// file did not change and so was absent from `patchedTenants`.
//
// ⛔ AND SIMPLY NOT DELETING IT WAS WORSE THAN DELETING IT. Measured: with
// `a.yaml` declaring the tenant at 11 and `b.yaml` transiently at 22, dropping
// it from `b.yaml` left the merged config holding 22 — a value NO file on disk
// declares — where a restart gives 11, and where the previous behaviour at
// least emitted a divergence ERROR. Loud-and-wrong became silent-and-wrong.
//
// ⛔ THE UNION IS PER KEY, NOT WHOLE-MAP REPLACE. `mergePartialInto` copies
// key-by-key, so two files each contributing a different key both survive a
// full load. Returning just the last file's map dropped the other's: measured
// with a platform file's `tenants:` block supplying one key and the tenant's
// own file another — a legitimate shape the loader documents — one ordinary
// edit silently dropped the first key back to the platform default, and took
// the tenant's `_profile` with it.
//
// ⛔ EVERY BRANCH RETURNS A FRESH MAP, THE SINGLE-SOURCE ONE INCLUDED. It
// used to hand back `newConfigs[only].Tenants[tenant]` — the parsed partial's
// OWN map — and patchTenants hung that map in the merged config, where
// ApplyProfiles and applySubtreeDefaults write into it in place. The cached
// partial in `flat.configs` then carried the materialised profile value as
// if the tenant had written it, and every later rebuild that reused the
// partial (mergePartialConfigs on a `_profiles.yaml` edit, commitFlatFrom
// on any full load) merged that stale value back in ahead of the profile —
// a profile edit never reached a tenant that had once been served, with no
// log. The map copy is O(keys of one tenant) per patched tenant; the
// quadratic cost the note above describes was rescanning `newConfigs` per
// tenant, not this.
func reclaimTenantFrom(newConfigs map[string]ThresholdConfig, declaredIn tenantDeclarations, tenant string) (map[string]ScheduledValue, bool) {
	if only, single := declaredIn.single[tenant]; single {
		src := newConfigs[only].Tenants[tenant]
		overrides := make(map[string]ScheduledValue, len(src))
		for k, v := range src {
			overrides[k] = v
		}
		return overrides, true
	}
	sources := declaredIn.multi[tenant]
	if len(sources) == 0 {
		return nil, false
	}
	overrides := make(map[string]ScheduledValue)
	for _, name := range sources {
		for k, v := range newConfigs[name].Tenants[tenant] {
			overrides[k] = v // later in sortFlatMergeOrder wins, same as mergePartialConfigs
		}
	}
	return overrides, true
}

// patchTenants builds a merged config from the tenant-only incremental fast
// path: it shallow-copies prev (Defaults/StateFilters/Profiles shared, Tenants
// map cloned) then overwrites tenants from changed/added files and drops
// tenants from removed files. Extracted from IncrementalLoad Phase 4; the
// caller reads prev under the lock, this function is otherwise pure.
//
// Two invariants keep this fast path equivalent to the full-rebuild path
// (mergePartialConfigs + ApplyProfiles):
//
//   - changed+added are applied as one sorted filename sequence, and each
//     patched tenant is rebuilt from EVERY declaring file in
//     sortFlatMergeOrder (reclaimTenantFrom), mirroring mergePartialConfigs,
//     so the last-writer is deterministic and a tenant file beats a platform
//     file's `tenants:` entry whatever either is called.
//   - a tenant "survives" only while `exists` (tenantExistenceFor, this scan)
//     holds it: removing the last tenant file that declares a tenant drops
//     it even when a platform file's `tenants:` block still names it — a
//     platform file cannot keep a tenant alive any more than it can create
//     one, which is what the full rebuild does too.
//   - a removed file's tenant is dropped only when this same reload did NOT
//     re-introduce it via an added/changed file. A tenant relocating from a
//     removed file into an added/changed file in the same reload must stay —
//     the full rebuild keeps it because it re-merges every surviving file.
//     Without this guard the overwrite below adds the moved tenant and the
//     removal loop then wrongly drops it again (issue #790).
//
// ⛔ THE PARAGRAPH THAT USED TO SIT HERE WAS FALSE, and both of its claims are
// what #1569 measured. It said the removal pass may "consult only the
// just-patched tenants" because "cross-file duplicate declarations are rejected
// upstream by the hierarchical scan (issue #127)". They are rejected on a FULL
// load; this fast path accepts them silently, so a live tree can hold one. Once
// it does, a tenant surviving the deletion of one declaration does NOT have to
// reappear in a changed file — the surviving file did not change. The removal
// pass therefore scans the surviving parses (`reclaimTenantFrom`), which costs
// O(|newConfigs|) per removed tenant rather than O(1), and removals are rare.
//
// ⚠️ `patchedTenants` USED TO BE BUILT HERE AND IS GONE. It recorded the
// tenants this reload reintroduced, so the removal pass could tell a deletion
// from a move. Once both removal loops consult the declaration index instead,
// nothing read it — a reviewer pointed out it had no readers left, and a
// bookkeeping set nobody reads is a claim that the code does something it does
// not. The move case is now answered by the index, which knows every file that
// declares the tenant, not just the ones reparsed this round.
func patchTenants(prev *ThresholdConfig, newConfigs, oldConfigs map[string]ThresholdConfig, changed, added, removed []string, exists map[string]struct{}) ThresholdConfig {
	merged := ThresholdConfig{
		Defaults: prev.Defaults, // shared (immutable between patches)
		// Shared for the same reason: the incremental path only re-parses the
		// tenant files that changed, so anything platform-scoped has to be
		// carried across explicitly. Omitting it would give a config that is
		// correct on a full rebuild and empty after the first incremental
		// reload — exactly the full-vs-incremental drift this function's
		// header warns about.
		OptionalOverrides: prev.OptionalOverrides,
		StateFilters:      prev.StateFilters, // shared
		Profiles:          prev.Profiles,     // shared
		Tenants:           make(map[string]map[string]ScheduledValue, len(prev.Tenants)),
	}
	// Shallow-copy tenants map (keys only, values are immutable per-tenant maps)
	//
	// ⛔ "IMMUTABLE" IS AN OBLIGATION ON EVERY LATER STAGE, NOT A FACT. An
	// untouched tenant's inner map is the SAME OBJECT as the one inside
	// `m.config`, which `GetConfig()` has already handed to scraping
	// goroutines — verified by pointer identity across reloads. Two stages
	// downstream write into tenant maps in place (`ApplyProfiles`,
	// `applySubtreeDefaults` via `refreshRefused`), and today both are
	// idempotent fill-ins that never touch a key already present, so a steady
	// state performs no write at all and `-race` with concurrent scrapes is
	// clean. Anything added here that OVERWRITES rather than fills in would
	// mutate config a scrape is reading, with no test to catch it. Either keep
	// new overlays idempotent or copy the map first. (#1569 blind review, C-1.)
	for k, v := range prev.Tenants {
		merged.Tenants[k] = v
	}
	// Overwrite tenants from re-parsed (changed + added) files, applied as a
	// single sorted filename sequence so precedence matches mergePartialConfigs.
	patchFiles := append(append([]string{}, changed...), added...)
	sort.Strings(patchFiles)
	declaredIn := indexTenantDeclarations(newConfigs)
	for _, name := range patchFiles {
		if partial, ok := newConfigs[name]; ok {
			for tenant := range partial.Tenants {
				// ⛔ REBUILT FROM EVERY SOURCE, NOT FROM THIS FILE ALONE. This
				// used to be `merged.Tenants[tenant] = overrides`, which drops
				// whatever OTHER files contribute to the same tenant —
				// `mergePartialInto` unions per key, so a full load keeps them.
				// Measured on the legitimate two-source shape (a platform
				// file's `tenants:` block supplying one key, the tenant's own
				// file another): editing the tenant's file silently reverted
				// the platform-supplied key to the global default AND dropped
				// its `_profile`, which in turn made ApplyProfiles a no-op and
				// loosened a profile-supplied threshold from 95 to 60. All of
				// it silent. Found by the randomised fast-path-vs-full-load
				// differential, not by reading. (#1569 blind review.)
				//
				// ⚠️ COST, RE-MEASURED AFTER TWO WRONG VERSIONS OF THIS NOTE.
				// The first said "~3 allocations and ~16 KB per reload" — true
				// for ONE patched tenant, which is the only case it measured,
				// and badly wrong as a per-reload claim: the implementation it
				// described rescanned every file per patched tenant, so a
				// reload that rewrites the whole tree was O(tenants x files).
				// Measured at 1000 tenant files all changed: `IncrementalLoad`
				// 182-195 ms against a full `Load` of 80-94 ms on the same tree
				// — the fast path became slower than the rebuild it exists to
				// avoid. The declaration index fixed that; current figures on
				// the same box:
				//
				//	1000 files, all changed : 23-25 ms (full Load 87 ms)
				//	1000 files, one changed : 16246-16247 allocs/op
				//	                          (16242 before this PR's round 10,
				//	                           17247 with a slice per tenant)
				//
				// ⚠️ BYTES, WHICH BOTH EARLIER VERSIONS OF THIS NOTE OMITTED.
				// CI's IncrementalLoad_1000_OneFileChanged rose 2.252Mi → 2.330Mi
				// when the index landed, so the cost was attributed rather than
				// assumed: same box, same commit, index built vs index removed from
				// this loop, 5 runs each —
				//
				//	no index : 2312856-2345359 B/op, 16239-16240 allocs/op
				//	index    : 2443019-2443879 B/op, 16246-16247 allocs/op
				//
				// ⇒ ~99 KB and ~7 allocs per reload: the `map[string]string` sized
				// to len(newConfigs). Transient, freed with the patch, and paid once
				// per debounced reload rather than once per tenant.
				//
				// ⚠️ NOT TAKEN, DELIBERATELY: the index could cover only the
				// tenants named by patchFiles+removed rather than every file — ~1
				// entry in the common single-file reload. It buys back the 99 KB and
				// costs a SECOND SELECTION PREDICATE over the same tree, which is
				// the defect shape (the #1911 family) this whole PR exists to
				// close. Not worth
				// it for a transient 99 KB; recorded here so the next reader does
				// not rediscover it as a finding.
				//
				// ⚠️ It also said "the only benchmark that reaches this loop
				// with a changed file". False: BenchmarkIncrementalLoad_100_
				// OneFileChanged does too. ⚠️ And bytes/op on these benchmarks
				// is bimodal (two clusters ~32 KB apart, map bucket growth) — a
				// comment-only commit moved the CI figure — so a one-shot bytes
				// delta is never the signal on its own; the two arms above are
				// repeated 5x each for exactly that reason, and their ranges do not
				// overlap.
				// No `ok` check: `tenant` came from `partial.Tenants` and
				// `partial` came from `newConfigs`, so the index necessarily
				// has it. The earlier `if …; ok` branch could never be false —
				// a guard that cannot fire reads as a handled case and is not
				// one.
				ov, _ := reclaimTenantFrom(newConfigs, declaredIn, tenant)
				merged.Tenants[tenant] = ov
			}
		}
	}
	// ⛔ A TENANT CAN LEAVE WITHOUT ITS FILE LEAVING. The removal pass below
	// keys on deleted FILES, so deleting a tenant from a file that stays on
	// disk — or renaming one — left it live in the merged config forever:
	// measured, `tenants: {}` in a root file kept emitting the tenant after an
	// incremental reload while a restart dropped it, and a rename emitted BOTH
	// names. Alerts for a tenant the operator deleted keep firing until
	// something unrelated forces a full reload. (#1569 sweep B-2.)
	//
	// ⚠️ SCOPED TO FILES THAT STILL PARSE. When a changed file fails to parse
	// it is deleted from `newConfigs` upstream, so `ok` is false and THIS
	// file's tenants are left alone — today's fail-safe "keep the last good
	// values". A full load drops them instead (the walker rejects the file,
	// #1957), so the two PATHS still disagree there — though on each path
	// /effective follows /metrics (refreshTenantSources keeps such a tenant
	// exactly while this merged config does). That difference is a
	// deliberate behaviour question (silently keep stale values vs. stop a
	// tenant's alerts on a typo), not something to settle inside a bug fix.
	//
	// ⚠️ THE FAIL-SAFE DOES NOT EXTEND TO OTHER FILES' TENANTS, measured: with
	// a cross-file duplicate live, editing one file to drop the tenant while
	// the OTHER file fails to parse in the same reload removes the tenant even
	// though the unparseable file still declares it on disk. Same outcome
	// before and after this change, and it needs the invalid duplicate state to
	// reach, so it is recorded rather than fixed here.
	for _, name := range changed {
		oldPartial, hadOld := oldConfigs[name]
		newPartial, parsed := newConfigs[name]
		if !hadOld || !parsed {
			continue
		}
		for tenant := range oldPartial.Tenants {
			if _, stillDeclared := newPartial.Tenants[tenant]; stillDeclared {
				continue
			}
			if ov, survives := reclaimTenantFrom(newConfigs, declaredIn, tenant); survives && tenantExists(exists, tenant) {
				merged.Tenants[tenant] = ov
				continue
			}
			delete(merged.Tenants, tenant)
		}
	}
	// Remove tenants from deleted files, unless this same reload re-introduced
	// the tenant via an added/changed file (a move — see the invariant above).
	for _, name := range removed {
		if partial, ok := oldConfigs[name]; ok {
			for tenant := range partial.Tenants {
				if ov, survives := reclaimTenantFrom(newConfigs, declaredIn, tenant); survives && tenantExists(exists, tenant) {
					merged.Tenants[tenant] = ov
				} else {
					delete(merged.Tenants, tenant)
				}
			}
		}
	}
	return merged
}

// tenantExists reports whether a tenant is in the scan's existence set.
// nil means no platform `tenants:` entry is in the merge (tenantExistenceFor),
// so every tenant a surviving file declares came from a tenant file: yes.
func tenantExists(exists map[string]struct{}, tenant string) bool {
	if exists == nil {
		return true
	}
	_, ok := exists[tenant]
	return ok
}

// fullDirLoad performs a full directory load: ONE walk of the tree, then
// fullDirLoadFrom. Used for the initial load and as IncrementalLoad's
// cold-start fallback. The prior is whatever the manager retained, so a
// full load on a warm manager still takes the mtime fast-path and reuses
// its parsed partials; on a cold manager every file is read and cached.
func (m *ConfigManager) fullDirLoad() error {
	m.mu.RLock()
	tree := m.flat.tree
	m.mu.RUnlock()

	scan, err := scanDirTree(m.path, tree, m.getMetrics(), m.getLogger())
	if err != nil {
		return err
	}
	return m.fullDirLoadFrom(scan)
}

// fullDirLoadFrom is the full load built from a scan the caller already
// has: reject a duplicate tenant (#127), install the hierarchy state the
// scan describes, then build and commit the flat config against it. The
// two planes come from the same walk, so the audit that runs at commit
// compares two projections of one enumeration.
//
// Order matters and is the reverse of what it looks like: the hierarchy
// state goes in FIRST because installConfig hands the audit the
// tenantSources standing in the commit's lock window. Committing the new
// config against the previous tenantSources would pair this commit's
// refused keys with the previous population (and, until #1957 removed the
// presence check, reported every tenant the operator just deleted as
// divergent) until the next commit.
func (m *ConfigManager) fullDirLoadFrom(scan *treeScan) error {
	if err := rejectDuplicateTenant(scan); err != nil {
		return err
	}
	if len(scan.Files) == 0 {
		return fmt.Errorf("no .yaml files found in %s", m.path)
	}
	m.populateHierarchyStateFrom(scan)
	return m.commitFlatFrom(scan)
}

// commitFlatFrom builds the merged ThresholdConfig from a scan and commits
// it together with the flat cache, applying the subtree defaults the
// CURRENT hierarchy state (already installed by the caller) declares.
//
// A file carries bytes only when its hash moved against the prior, so for
// a file without bytes the parsed partial from the previous commit is
// reused when its hash is unchanged — the walker's prior IS the tree of
// that previous commit (see flatScanState.tree), which is what makes the
// reuse sound. A `_`-prefixed file that is unchanged but has no cached
// partial (it failed to parse last time, or it is a nested platform file
// this plane never caches) is re-read and re-judged, so its ERROR/WARN and
// its parse-failure count fire again exactly as on a cold load. A tenant
// file is never re-judged here: the walker parses it (#1957) and has already
// logged and counted a failure on this scan (TreeFile.ParseFailed).
func (m *ConfigManager) commitFlatFrom(scan *treeScan) error {
	if len(scan.Files) == 0 {
		return fmt.Errorf("no .yaml files found in %s", m.path)
	}

	m.mu.RLock()
	priorHashes := m.flat.hashes
	priorConfigs := m.flat.configs
	m.mu.RUnlock()

	// The root carrier the chain selects (#1674). Computed before the loop:
	// an unselected root carrier is ignored on every plane, so it is not even
	// parsed here, let alone merged or cached.
	rootCarrier := rootCarrierKey(scan, m.getLogger())

	fileConfigs := make(map[string]ThresholdConfig, len(scan.Files))
	for _, name := range scan.Keys {
		if isUnselectedRootCarrier(name, rootCarrier) {
			continue
		}
		f := scan.Files[name]
		// ⛔ THE WALKER ALREADY JUDGED IT (#1957). A tenant file whose one
		// decode failed was logged and counted by the walker on THIS scan;
		// re-reading it here would count it twice per scan (the historical
		// double count for syntax errors) and log it twice.
		if f.ParseFailed {
			continue
		}
		// ⛔ THE WALKER ALREADY DECODED IT (#1957). A tenant file this scan
		// parsed comes with its ThresholdConfig in scan.Partials, decoded by
		// the same config.ParseConfigFile parsePartialConfig calls — so the
		// walker's tenant set and this plane's are one verdict, and the bytes
		// are not decoded twice.
		if partial, ok := scan.Partials[name]; ok {
			applyBoundaryRules(name, &partial, m.getLogger())
			fileConfigs[name] = partial
			continue
		}
		fullPath := filepath.Join(m.path, name)
		data := f.Data
		if data == nil {
			if priorHashes[name] == f.Hash {
				if partial, ok := priorConfigs[name]; ok {
					fileConfigs[name] = partial
					continue
				}
			}
			// Unchanged-but-uncached, or a prior the walker did not have:
			// read from disk and take the ordinary path below.
			var rerr error
			data, rerr = os.ReadFile(fullPath)
			if rerr != nil {
				m.getLogger().Printf("WARN: skip unreadable file %s: %v", fullPath, rerr)
				continue
			}
		}
		// ⛔ A nested `_` file is scanned (change detection must see it) but
		// contributes NOTHING to the merged config. `ThresholdConfig.Defaults`
		// is ONE global map with no subtree scope, and the merge is
		// last-writer-wins over sorted keys — so `nested/_defaults.yaml` sorts
		// after the root's and would re-price every tenant in the tree,
		// including tenants in unrelated subtrees. Measured; see
		// `TestASubtreeDefaultNeverLeaksIntoTheGlobalOnes`.
		//
		// ⛔ NOT `parsePartialConfig`, BUT NOT SILENT EITHER. Running the full
		// parse here logs `ERROR: skip unparseable defaults/profiles file …`
		// for a tree that is entirely valid: `Defaults` is
		// `map[string]float64`, so a subtree defaults file in the SCHEDULE
		// form (`{default: "90", overrides: […]}`) — which the hierarchical
		// plane accepts and `/effective` renders — cannot decode into it.
		// Skipping outright, though, dropped the parse-failure counter and the
		// ERROR for files that are GENUINELY broken (measured: a nested
		// `_defaults.yaml` containing `defaults: [this is not a map` scored 0
		// on the counter and produced no ERROR — a severity downgrade the
		// recursion introduced). The probe below separates the two: a syntax
		// error is still counted and still loud; content this plane simply
		// does not want is skipped in silence. (#1569 blind review.)
		if isNestedPlatformFile(name) {
			reportUnparseableNestedPlatformFile(fullPath, data, m.getMetrics(), m.getLogger())
			reportNestedPlatformTenants(fullPath, data, m.getLogger())
			continue
		}
		partial, ok := parsePartialConfig(name, fullPath, data, m.getMetrics(), m.getLogger())
		if !ok {
			continue
		}
		applyBoundaryRules(name, &partial, m.getLogger())
		fileConfigs[name] = partial
	}

	// Merge all partials. Tenant existence is THIS scan's verdict
	// (declaredTenantIDs): a root platform file's `tenants:` entry for a
	// tenant no tenant file declares is dropped and named.
	exists := tenantExistenceFor(fileConfigs, scan)
	reportPlatformOrphans(fileConfigs, exists, m.getLogger())
	merged := mergePartialConfigs(fileConfigs, exists)
	merged.ApplyProfiles()

	// #1521 second half: the caller just installed the inheritance graph this
	// scan produced, so each tenant's L1..Ln defaults can be materialised into
	// its own map before the commit. Read under RLock because the install
	// published them under its own Lock and released it.
	m.mu.RLock()
	var tenantDefaults map[string][]string
	if m.hierarchy.graph != nil {
		tenantDefaults = m.hierarchy.graph.TenantDefaults
	}
	parsedDefaults := m.hierarchy.parsedDefaults
	m.mu.RUnlock()
	n, unreachable := applySubtreeDefaults(&merged, m.path, tenantDefaults, parsedDefaults)
	if n > 0 {
		m.getLogger().Printf(
			"INFO: applied %d inherited subtree default(s) to the collector config "+
				"(conf.d subdirectories declare defaults; without this the series "+
				"would carry the root value while /effective reports the subtree's)", n)
	}
	// Published before the commit so `installConfig` can hand it to the audit
	// from the same lock window that yields tenantSources.
	m.mu.Lock()
	m.hierarchy.unreachableInherited = unreachable
	m.mu.Unlock()

	scan.ReleaseData()
	m.commitConfig(&merged, scan.Composite, &flatScanState{
		hashes:  scan.RelHashes(),
		configs: fileConfigs,
		mtimes:  scan.RelMtimes(),
		tree:    scan,
	}, fmt.Sprintf("Config loaded (%s)", m.Mode()))
	return nil
}

// populateHierarchyStateFrom installs the graph + per-tenant merged_hash
// the scan describes onto the ConfigManager. Called by fullDirLoadFrom
// before the flat commit; the debounced reload path installs its own,
// incrementally classified, hierarchy state instead
// (installNewHierarchyState).
//
// It is a no-op if the tree holds neither a _defaults.yaml nor a tenant
// (flat mode — hierarchicalMode stays false; nothing to populate). The
// scan cannot fail here: walk errors were returned by scanDirTree and a
// duplicate tenant was rejected by the caller. Merge failures and
// unreadable defaults files are logged and skipped per tenant / per file.
//
// Every merged_hash is recomputed from disk. This is the cold-start
// semantics; the incremental reuse of unchanged tenants lives in
// classifyTenant, on the debounced path, and is not duplicated here.
// ⚠️ recomputeMergedHash re-reads and re-parses each tenant file the walker
// has just decoded in full (#1957; TreeScan.Partials) — the remaining
// double parse of a hierarchical cold load, tracked in #1978.
//
// Memory: the hashes map may be large at 1000 tenants (roughly
// tenants × 64-char strings = ~100KB). We swap the pointer rather than
// merging in place so a partial install never leaves torn state visible
// to the /effective read path.
func (m *ConfigManager) populateHierarchyStateFrom(scan *treeScan) {
	tenants, defaults, graph := scan.Tenants, scan.Defaults, scan.InheritanceGraph()
	if len(defaults) == 0 && len(tenants) == 0 {
		// Empty tree or flat layout with no files we recognize. Don't
		// flip hierarchicalMode — a later add-a-_defaults-file event will
		// flip it via diffAndReload.
		return
	}

	newMergedHashes := make(map[string]string, len(tenants))
	for tid, srcPath := range tenants {
		chain := graph.TenantDefaults[tid]
		mh, mergeErr := m.recomputeMergedHash(tid, srcPath, chain)
		if mergeErr != nil {
			logMergeSkip(m.getLogger(), tid, "initial-hierarchy-scan", mergeErr)
			continue
		}
		newMergedHashes[tid] = mh
	}

	// v2.8.0 Issue #61: pre-parse every _defaults.yaml so the first
	// post-cold-start diffAndReload tick can already classify
	// shadowed-vs-cosmetic effects without a "warm-up" tick where every
	// noOp falls back to "unknown". Parse failures are logged-and-skipped
	// (not fatal — same policy as logMergeSkip above) so one broken
	// defaults file can't poison the rest of the cache.
	newParsedDefaults := make(map[string]map[string]any, len(defaults))
	for dp := range defaults {
		b, rerr := os.ReadFile(dp)
		if rerr != nil {
			m.getLogger().Printf("WARN: parsedDefaults cache: read %s: %v", dp, rerr)
			continue
		}
		parsed, perr := parseDefaultsBytes(b)
		if perr != nil {
			m.getLogger().Printf("WARN: parsedDefaults cache: parse %s: %v", dp, perr)
			continue
		}
		newParsedDefaults[dp] = parsed
	}

	m.mu.Lock()
	// Only flip hierarchicalMode on once we've seen a _defaults.yaml
	// somewhere. Pure-flat trees keep hierarchicalMode=false, letting
	// WatchLoop take the v2.6.0 IncrementalLoad path.
	if len(defaults) > 0 {
		m.hierarchy.enabled = true
	}
	m.hierarchy.tenantSources = tenants
	m.hierarchy.hashes = scan.AbsHashes()
	m.hierarchy.mergedHashes = newMergedHashes
	m.hierarchy.graph = graph
	m.hierarchy.parsedDefaults = newParsedDefaults
	m.mu.Unlock()
}

// logConfigStats logs config summary with cheap counts instead of calling
// the expensive Resolve()/ResolveStateFilters()/ResolveSilentModes().
// At 1000 tenants, this saves ~4ms per reload (Resolve alone costs ~2-5ms).
// The "resolved thresholds" count is estimated from tenant override counts
// rather than running the full resolution pipeline.
// logger may be nil → falls back to log.Default() (production safety).
func logConfigStats(logger *log.Logger, cfg *ThresholdConfig, prefix string) {
	if logger == nil {
		logger = log.Default()
	}
	// Cheap estimate: count total tenant overrides (each becomes ~1 resolved threshold)
	overrideCount := 0
	silentCount := 0
	stateCount := 0
	for _, overrides := range cfg.Tenants {
		for key := range overrides {
			switch {
			case key == "_silent_mode":
				silentCount++
			case strings.HasPrefix(key, "_state_"):
				stateCount++
			case !strings.HasPrefix(key, "_"):
				overrideCount++
			}
		}
	}

	logger.Printf("%s: %d defaults, %d profiles, %d state_filters, %d tenants, ~%d threshold overrides, %d state entries, %d silent modes",
		prefix, len(cfg.Defaults), len(cfg.Profiles), len(cfg.StateFilters), len(cfg.Tenants),
		overrideCount, stateCount, silentCount)

	// #1231 c2: Errors (blocking channel — same set the tenant-api write
	// gate rejects on) first, then Notices (advisory deprecation channel).
	kv := cfg.ValidateTenantKeys()
	for _, w := range kv.Errors {
		logger.Printf("%s", w)
	}
	for _, n := range kv.Notices {
		logger.Printf("%s", n)
	}
}

// WatchLoop periodically checks for config changes and reloads.
// Change detection compares content hashes, but in directory mode a file's
// hash is only recomputed when the walker's mtime fast-path lets it through
// (pkg/config ScanDirTree: a file whose ModTime+Size match the prior scan and
// that is older than TreeScanMtimeGuard keeps its prior hash unread). K8s
// ConfigMap volumes update by swapping the `..data` symlink and leave every
// `key -> ..data/key` link untouched, so for a symlink that stat is the
// link's own lstat AND its TARGET's stat, both of which must be unchanged
// (#1969) — before that, only the link's lstat was compared and a `..data`
// swap was invisible to every tick, i.e. the swap never reloaded. Nested
// ConfigMap item paths (`team-a/x.yaml`) mount as a DIRECTORY symlink the
// walker does not follow; files under it are not loaded at all (#1972).
// Single-file mode hashes the file on every tick (no fast-path).
// The stopCh parameter allows graceful shutdown — close it to stop the loop.
//
// In directory mode, uses incremental reload (v2.1.0): per-file hash tracking
// means only changed files are re-parsed, reducing reload latency for large
// multi-tenant deployments.
func (m *ConfigManager) WatchLoop(interval time.Duration, stopCh <-chan struct{}) {
	// Defensive: ConfigManager constructed via struct literal (test
	// shortcut) wouldn't have called NewConfigManagerWithDebounce, so the
	// clock field is nil. Fall back to a real clock; tests that want a
	// fake clock must call SetClock before WatchLoop.
	if m.clock == nil {
		m.clock = clockwork.NewRealClock()
	}
	ticker := m.clock.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-stopCh:
			m.getLogger().Println("WatchLoop stopped")
			return
		case <-ticker.Chan():
		}
		m.tickOnce()
	}
}

// tickOnce performs ONE iteration of the polling cycle (detect change →
// trigger debounced reload, OR full single-file reload). Extracted from
// WatchLoop so tests can drive the pipeline synchronously without the
// real ticker — when paired with NewConfigManagerWithDebounce(dir, 0)
// the entire detect→reload chain becomes deterministic, eliminating the
// real-time-dependent flake that motivated TRK-011.
//
// Production behavior is unchanged: WatchLoop still calls this on every
// tick and the debounce path still gates the actual reload.
func (m *ConfigManager) tickOnce() {
	if m.isDir {
		changed, reason, err := m.detectChange()
		if err != nil {
			m.getLogger().Printf("WARN: cannot check config %s: %v", m.path, err)
			return
		}
		if changed {
			m.getLogger().Printf("Config changed, scheduling debounced reload...")
			// v2.7.0: route through debounce even for flat mode so an
			// ops tool that rapidly rewrites multiple files coalesces
			// into a single reload.
			m.triggerDebouncedReload(reason)
		}
		return
	}

	// Single-file mode: full reload (no incremental benefit)
	_, hash, err := loadFile(m.path)
	if err != nil {
		m.getLogger().Printf("WARN: cannot check config %s: %v", m.path, err)
		return
	}

	m.mu.RLock()
	changed := hash != m.lastHash
	m.mu.RUnlock()

	if changed {
		m.getLogger().Printf("Config changed, reloading...")
		if err := m.Load(); err != nil {
			m.getLogger().Printf("ERROR: failed to reload config: %v", err)
		}
	}
}

// detectChange runs directory-mode change detection — the flat-scan
// (v2.1.0) or hierarchical-scan (v2.8.0 A-10 fix, Issue #52) path
// depending on whether hierarchical mode has been activated for this
// config root.
//
// Both paths are ONE walk (scanDirTree with the retained scan as the
// prior, so an unchanged file costs a stat, not a read); they differ in
// what they compare and in the reason they return:
//
//   - Flat: composite hash against the last commit's. Returns
//     reason=source so the debounce path emits
//     da_config_reload_trigger_total{reason="source"}.
//   - Hierarchical: per-file hash against the installed hierarchy plane's
//     (absolute keys); any file added/removed/changed constitutes a
//     change, and a duplicate tenant is a scan error as it always was.
//     Returns reason=forced; diffAndReload will categorize the actual
//     reason via its per-tenant hash compare.
//
// O(N) compare for hierarchical mode is acceptable: at WatchInterval
// cadence (30s default) with 1000 files it adds ~1k comparisons/30s —
// negligible.
//
// The scan is NOT retained here: the prior must stay the tree of the last
// commit (see flatScanState.tree), and a tick that detects a change
// commits nothing — the debounced reload walks again and commits that.
//
// v2.8.0 PR-3: extracted from WatchLoop so the dual-path lives in a
// named seam. Single-file mode stays inline in WatchLoop because its
// reload semantics differ (synchronous m.Load instead of debounced).
//
// Caller responsibilities: log the (warning-level) error, and on
// changed=true call triggerDebouncedReload(reason).
func (m *ConfigManager) detectChange() (bool, string, error) {
	m.mu.RLock()
	tree := m.flat.tree
	prevHash := m.lastHash
	hierarchical := m.hierarchy.enabled
	priorHierHashes := m.hierarchy.hashes
	m.mu.RUnlock()

	scan, err := scanDirTree(m.path, tree, m.getMetrics(), m.getLogger())
	if hierarchical {
		if err == nil && scan.Conflict != nil {
			err = scan.Conflict
		}
		if err != nil {
			return false, "", fmt.Errorf("hierarchical scan: %w", err)
		}
		changed := len(scan.Files) != len(priorHierHashes)
		if !changed {
			for _, f := range scan.Files {
				if priorHierHashes[f.AbsPath] != f.Hash {
					changed = true
					break
				}
			}
		}
		return changed, ReloadReasonForced, nil
	}

	if err != nil {
		return false, "", err
	}
	return scan.Composite != prevHash, ReloadReasonSource, nil
}

func (m *ConfigManager) GetConfig() *ThresholdConfig {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.config
}

// EffectiveConfig is the result of resolving one tenant's full config
// chain (L0→Ln defaults merged + tenant override applied) with both the
// raw-source and canonical-merged hashes. Surfaced via
// ConfigManager.Resolve and the /api/v1/tenants/{id}/effective endpoint
// (§8.11.3 Phase 6).
//
// Field naming matches describe_tenant.py JSON output + tenant-api Go
// shape to keep cross-language consumers drop-in compatible.
type EffectiveConfig struct {
	TenantID      string         // tenant identifier
	SourceFile    string         // absolute path to tenant YAML
	SourceHash    string         // SHA-256[:16] of raw tenant bytes
	MergedHash    string         // SHA-256[:16] of canonical merged JSON
	DefaultsChain []string       // L0→Ln defaults file paths (root first)
	Config        map[string]any // merged tenant config (full dict)
	Warnings      []string       // merge-time warnings (currently empty)
}

// Resolve returns the effective config for one tenant, computed on
// demand from the cached hierarchy state. Returns (nil, false) when the
// tenant is not currently known (404 signal for the /effective handler).
//
// The returned Config is a freshly-allocated map owned by the caller —
// safe to serialize concurrently with future reloads.
//
// Error semantics: merge failures (unreadable file, bad YAML) return
// (nil, true) with a single warning. This lets the API respond with a
// structured error body instead of 404/500.
func (m *ConfigManager) Resolve(tenantID string) (*EffectiveConfig, bool) {
	m.mu.RLock()
	srcPath, known := m.hierarchy.tenantSources[tenantID]
	var chain []string
	if m.hierarchy.graph != nil {
		chain = append(chain, m.hierarchy.graph.TenantDefaults[tenantID]...)
	}
	cachedHash := m.hierarchy.mergedHashes[tenantID]
	m.mu.RUnlock()

	if !known {
		return nil, false
	}

	tenantBytes, err := os.ReadFile(srcPath)
	if err != nil {
		return &EffectiveConfig{
			TenantID:      tenantID,
			SourceFile:    srcPath,
			DefaultsChain: chain,
			Warnings:      []string{fmt.Sprintf("read tenant file: %v", err)},
		}, true
	}

	// Re-read each defaults file. This is intentional: the cached
	// merged_hash is valid under the last scan, but we want the /effective
	// response to contain the live effective_config map, not just the
	// hash. Future optimization: cache the merged map alongside the hash.
	chainBytes := make([][]byte, 0, len(chain))
	var warnings []string
	for _, dp := range chain {
		b, rerr := os.ReadFile(dp)
		if rerr != nil {
			warnings = append(warnings, fmt.Sprintf("read defaults %s: %v", dp, rerr))
			continue
		}
		chainBytes = append(chainBytes, b)
	}

	merged, err := computeEffectiveConfig(tenantBytes, tenantID, chainBytes)
	if err != nil {
		return &EffectiveConfig{
			TenantID:      tenantID,
			SourceFile:    srcPath,
			DefaultsChain: chain,
			Warnings:      append(warnings, fmt.Sprintf("merge: %v", err)),
		}, true
	}

	sourceHash := computeSourceHash(tenantBytes)
	mergedHash := cachedHash
	if mergedHash == "" {
		// Cold path: cache miss (first /effective before any reload).
		// Compute on the fly.
		if mh, mErr := computeMergedHash(tenantBytes, tenantID, chainBytes); mErr == nil {
			mergedHash = mh
		}
	}

	return &EffectiveConfig{
		TenantID:      tenantID,
		SourceFile:    srcPath,
		SourceHash:    sourceHash,
		MergedHash:    mergedHash,
		DefaultsChain: chain,
		Config:        merged,
		Warnings:      warnings,
	}, true
}

func (m *ConfigManager) IsLoaded() bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.loaded
}

func (m *ConfigManager) LastReload() time.Time {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.lastReload
}

// GetConfigInfo returns config source metadata for the threshold_exporter_config_info metric (v2.3.0).
func (m *ConfigManager) GetConfigInfo() ConfigInfo {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return ConfigInfo{
		ConfigSource: m.configSource,
		GitCommit:    m.gitCommit,
	}
}

// detectConfigSource determines the config source mode and git commit.
//
// Detection logic:
//  1. If .git-revision file exists adjacent to config path → "git-sync" + read commit hash
//  2. If OPERATOR_CRD_SOURCE env is set → "operator"
//  3. Default → "configmap"
//
// Called on initial load and each reload to pick up git-sync rotations.
//
// Pure w.r.t. ConfigManager state: it reads only the immutable m.path /
// m.isDir (fixed at construction) plus the filesystem/env, and RETURNS
// the detected values instead of writing m.configSource / m.gitCommit
// directly. commitConfig writes them under m.mu so concurrent
// GetConfigInfo readers never race an unsynchronised field write.
func (m *ConfigManager) detectConfigSource() (configSource, gitCommit string) {
	gitCommit = ""
	configSource = "configmap"

	// Check for .git-revision file (written by git-sync sidecar)
	var searchDir string
	if m.isDir {
		searchDir = m.path
	} else {
		searchDir = filepath.Dir(m.path)
	}
	revFile := filepath.Join(searchDir, ".git-revision")
	if data, err := os.ReadFile(revFile); err == nil {
		commit := strings.TrimSpace(string(data))
		if commit != "" {
			gitCommit = commit
			configSource = "git-sync"
		}
	}

	// Operator CRD source override (set by operator-generate sidecar or init container)
	if configSource != "git-sync" {
		if v := os.Getenv("OPERATOR_CRD_SOURCE"); v != "" {
			configSource = "operator"
		}
	}

	return configSource, gitCommit
}
