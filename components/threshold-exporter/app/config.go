package main

import (
	"crypto/sha256"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

// ============================================================
// ConfigManager — supports both single-file and directory mode
// ============================================================

// ConfigManager handles loading and hot-reloading the config.
// Supports two modes:
//   - Single-file mode (legacy): reads one YAML file
//   - Directory mode: scans all *.yaml files in a directory and deep-merges
//
// In directory mode, ConfigManager supports incremental hot-reload (v2.1.0):
// per-file SHA-256 tracking + parsed config cache → only changed files are
// re-parsed on each reload cycle, then all cached partials are merged.
type ConfigManager struct {
	path     string // file path or directory path
	isDir    bool   // true = directory mode
	mu       sync.RWMutex
	config   *ThresholdConfig
	loaded   bool
	lastReload time.Time
	lastHash   string // SHA-256 composite hash for change detection

	// Incremental reload state (directory mode only, v2.1.0)
	fileHashes  map[string]string          // filename → SHA-256
	fileConfigs map[string]ThresholdConfig // filename → parsed partial config
	fileMtimes  map[string]fileStat        // filename → mtime+size for quick skip (v2.1.0)

	// Config info metric state (v2.3.0)
	configSource string // "configmap", "operator", or "git-sync"
	gitCommit    string // git commit hash from .git-revision file, or ""

	// Hierarchical scan state (v2.7.0, ADR-017/018 — Phase 3+5)
	//
	// hierarchical mode is auto-detected on first load: if scanDirHierarchical
	// finds at least one _defaults.yaml at any depth AND the top-level scan
	// path is a directory, we keep hierarchical state populated alongside the
	// flat fileHashes above. A reload always produces both views so a legacy
	// flat caller (fullDirLoad) stays correct.
	//
	// When hierarchicalMode is false, all three maps/graph are nil and
	// diffAndReload falls back to IncrementalLoad (flat path).
	hierarchicalMode bool
	// tenantSources maps tenantID → absolute tenant file path. Updated on
	// every hierarchical scan; used by Resolve(tenantID) for /effective.
	tenantSources map[string]string
	// hierarchyHashes is keyed by absolute Clean path and stores the full
	// 64-char SHA-256 hex of each scanned YAML (tenant + defaults). Used to
	// diff which files changed between scans in Phase 3.
	hierarchyHashes map[string]string
	// hierarchyMtimes parallels hierarchyHashes for mtime-based fast path
	// (forward-compat with scanDirHierarchical's priorMtimes arg).
	hierarchyMtimes map[string]fileStat
	// mergedHashes is the user-facing 16-char merged_hash per tenant, keyed
	// by tenantID. Computed by diffAndReload on every dirty tenant. The
	// /effective handler reads this to avoid recomputing on each request.
	mergedHashes map[string]string
	// inheritanceGraph records defaults↔tenants dependencies. Swapped
	// atomically on reload (pointer swap; the graph itself is immutable
	// once built). nil when hierarchicalMode is false.
	inheritanceGraph *InheritanceGraph
	// parsedDefaults caches the *parsed-and-normalized* dict of every
	// _defaults.yaml file in the tree (key = absolute Clean path, same
	// keying as hierarchyHashes). Required by Issue #61 to distinguish
	// effect=shadowed from effect=cosmetic in the blast-radius
	// histogram: the noOp branch needs to know which keys actually
	// moved between prior and current scan, which file-hash alone can't
	// answer (comment-only edits move the file hash but not any key).
	//
	// MUST atomic-swap together with hierarchyHashes — a desync between
	// "we know file X has hash H_new" and "parsed cache for X is H_old"
	// would cause changedDefaultsKeys to compute against the wrong
	// baseline. Both are installed under m.mu.Lock() in
	// populateHierarchyState (cold start) and diffAndReload (incremental).
	//
	// Memory: ~432 _defaults files × ~2KB parsed dict ≈ 1MB at the
	// 1000-tenant baseline; scales linearly with tree size.
	parsedDefaults map[string]map[string]any

	// Debounce state (v2.7.0 Phase 3)
	//
	// debounceWindow bundles multiple fsnotify-ish bursts (tick-initiated
	// diffs, manual SIGHUP, etc.) into a single reload. 0 disables debouncing
	// and restores the v2.6.0 behavior (immediate reload on detected diff).
	debounceWindow time.Duration
	debounceTimer  *time.Timer // current pending reload; nil when idle
	debounceMu     sync.Mutex  // guards debounceTimer + pendingReasons
	// pendingReasons accumulates reload triggers during a debounce window so
	// the terminal diffAndReload can emit counter increments per-reason (see
	// collector.go da_config_reload_trigger_total). Cleared on fire + Close.
	pendingReasons []string
	// debounceFired is bumped by the timer goroutine each time a debounce
	// fires. Tests read this via DebounceFiredCount() to assert batching.
	debounceFired uint64
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
		path:           path,
		isDir:          isDir,
		debounceWindow: debounceWindow,
	}
}

// Mode returns "directory" or "single-file" for diagnostics.
func (m *ConfigManager) Mode() string {
	if m.isDir {
		return "directory"
	}
	return "single-file"
}

// Load loads config from either a single file or a directory.
func (m *ConfigManager) Load() error {
	var cfg ThresholdConfig
	var hash string
	var err error

	if m.isDir {
		cfg, hash, err = loadDir(m.path)
	} else {
		cfg, hash, err = loadFile(m.path)
	}
	if err != nil {
		return err
	}

	// Ensure maps are initialized
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

	// v2.8.x issue #127: hierarchical scan runs BEFORE the flat-mode commit
	// so a `*DuplicateTenantError` (mixed-mode misconfig: same tenant ID in
	// both `<root>/<id>.yaml` and `<root>/<dir>/<id>.yaml`) rejects Load
	// at the boundary instead of silently last-wins-merging via the flat
	// path. Other scan errors (permissions, malformed file, missing path)
	// keep the prior log-and-continue policy because hierarchical mode is
	// opt-in — a malformed branch shouldn't tear down a flat-only deploy.
	//
	// On hard reject: m.config / m.loaded stay at their pre-Load values
	// (nil / false on cold start) — caller observes "Load returned error"
	// without any partial state being committed.
	if m.isDir {
		if hierErr := m.populateHierarchyState(); hierErr != nil {
			var dupErr *DuplicateTenantError
			if errors.As(hierErr, &dupErr) {
				return fmt.Errorf("config rejected (mixed-mode duplicate tenant): %w", hierErr)
			}
			log.Printf("WARN: hierarchical scan during Load failed: %v", hierErr)
		}
	}

	m.mu.Lock()
	m.config = &cfg
	m.loaded = true
	m.lastReload = time.Now()
	m.lastHash = hash
	m.mu.Unlock()

	// Detect config source mode and git commit (v2.3.0)
	m.detectConfigSource()

	logConfigStats(&cfg, fmt.Sprintf("Config loaded (%s)", m.Mode()))

	return nil
}

// loadFile reads a single YAML config file and returns the parsed config + content hash.
func loadFile(path string) (ThresholdConfig, string, error) {
	var cfg ThresholdConfig

	data, err := os.ReadFile(path)
	if err != nil {
		return cfg, "", fmt.Errorf("read config %s: %w", path, err)
	}

	hash := fmt.Sprintf("%x", sha256.Sum256(data))

	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return cfg, "", fmt.Errorf("parse config %s: %w", path, err)
	}

	return cfg, hash, nil
}

// loadDir scans a directory for *.yaml files, parses and deep-merges them.
//
// File naming convention:
//   - _defaults.yaml: contains 'defaults' and 'state_filters' (loaded first due to underscore prefix)
//   - <tenant-name>.yaml: contains tenant-specific overrides under 'tenants' key
//
// Merge rules:
//   - Files are processed in sorted order (underscore prefix sorts first)
//   - defaults: later values overwrite earlier ones for the same key
//   - state_filters: later values overwrite earlier ones for the same filter name
//   - tenants: deep merge per tenant (later key-values overwrite)
//
// Boundary rule: state_filters should only be defined in _defaults.yaml.
// Tenant files should only contain a 'tenants' block. This is enforced with warnings.
func loadDir(dir string) (ThresholdConfig, string, error) {
	merged := ThresholdConfig{
		Defaults:     make(map[string]float64),
		StateFilters: make(map[string]StateFilter),
		Tenants:      make(map[string]map[string]ScheduledValue),
		Profiles:     make(map[string]map[string]ScheduledValue),
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return merged, "", fmt.Errorf("read config dir %s: %w", dir, err)
	}

	// Collect *.yaml files, sorted (underscore prefix sorts first)
	var files []string
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || strings.HasPrefix(name, ".") {
			continue
		}
		if strings.HasSuffix(name, ".yaml") || strings.HasSuffix(name, ".yml") {
			files = append(files, name)
		}
	}
	sort.Strings(files)

	if len(files) == 0 {
		return merged, "", fmt.Errorf("no .yaml files found in %s", dir)
	}

	// Hash all file contents for change detection
	hasher := sha256.New()

	for _, name := range files {
		path := filepath.Join(dir, name)
		data, err := os.ReadFile(path)
		if err != nil {
			log.Printf("WARN: skip unreadable file %s: %v", path, err)
			continue
		}
		hasher.Write(data)

		var partial ThresholdConfig
		if err := yaml.Unmarshal(data, &partial); err != nil {
			// _defaults.yaml parse failure silently nullifies the entire
			// defaults block → every dependent tenant override breaks
			// (`unknown key not in defaults`). Cycle-6 RCA (planning archive
			// §S#37d) cost 5+ hours wall-clock because this signal lived at
			// WARN. Promote to ERROR for `_*` files; emit parse_failure_total
			// (v2.8.0 A-8d metric) so ops can alert.
			IncParseFailure(filepath.Base(path))
			if strings.HasPrefix(name, "_") {
				log.Printf("ERROR: skip unparseable defaults/profiles file %s: %v (entire block dropped — fix file or remove)", path, err)
			} else {
				log.Printf("WARN: skip unparseable file %s: %v", path, err)
			}
			continue
		}

		isDefaultsFile := strings.HasPrefix(name, "_")
		isProfilesFile := name == "_profiles.yaml" || name == "_profiles.yml"

		// Boundary enforcement: warn if tenant file contains state_filters, defaults, or profiles
		if !isDefaultsFile {
			if len(partial.StateFilters) > 0 {
				log.Printf("WARN: state_filters found in %s — should only be in _defaults.yaml, ignoring", name)
				partial.StateFilters = nil
			}
			if len(partial.Defaults) > 0 {
				log.Printf("WARN: defaults found in %s — should only be in _defaults.yaml, ignoring", name)
				partial.Defaults = nil
			}
		}
		if !isProfilesFile && !isDefaultsFile {
			if len(partial.Profiles) > 0 {
				log.Printf("WARN: profiles found in %s — should only be in _profiles.yaml, ignoring", name)
				partial.Profiles = nil
			}
		}

		// Merge defaults
		for k, v := range partial.Defaults {
			merged.Defaults[k] = v
		}

		// Merge state_filters
		for k, v := range partial.StateFilters {
			merged.StateFilters[k] = v
		}

		// Merge profiles (v1.12.0)
		for profileName, profileValues := range partial.Profiles {
			if merged.Profiles[profileName] == nil {
				merged.Profiles[profileName] = make(map[string]ScheduledValue)
			}
			for k, v := range profileValues {
				merged.Profiles[profileName][k] = v
			}
		}

		// Merge tenants (deep merge per tenant)
		for tenant, overrides := range partial.Tenants {
			if merged.Tenants[tenant] == nil {
				merged.Tenants[tenant] = make(map[string]ScheduledValue)
			}
			for k, v := range overrides {
				merged.Tenants[tenant][k] = v
			}
		}
	}

	hash := fmt.Sprintf("%x", hasher.Sum(nil))
	return merged, hash, nil
}

// scanDirFileHashes scans a directory and returns per-file SHA-256 hashes,
// the composite hash, per-file mtime+size stats, and a byte cache of files
// that were actually read (for reuse by callers that need file contents,
// avoiding double disk reads in fullDirLoad/IncrementalLoad).
//
// Uses DirEntry.Info() to get mtime+size from the directory listing itself,
// avoiding separate os.Stat calls per file.
//
// When oldHashes and oldMtimes are provided (non-nil), the mtime guard kicks in:
// files whose ModTime and Size match the previous scan reuse the cached SHA-256
// without re-reading file contents. This reduces NoChange cost from O(N×read)
// to O(N×stat) — typically 4-5× faster at 1000 tenants.
func scanDirFileHashes(dir string, oldHashes map[string]string, oldMtimes map[string]fileStat) (map[string]string, string, map[string]fileStat, map[string][]byte, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, "", nil, nil, fmt.Errorf("read config dir %s: %w", dir, err)
	}

	type dirFile struct {
		name string
		info os.FileInfo // from DirEntry.Info(), avoids separate os.Stat
	}
	var files []dirFile
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || strings.HasPrefix(name, ".") {
			continue
		}
		if strings.HasSuffix(name, ".yaml") || strings.HasSuffix(name, ".yml") {
			info, ierr := entry.Info()
			if ierr != nil {
				log.Printf("WARN: skip unreadable entry %s: %v", name, ierr)
				continue
			}
			files = append(files, dirFile{name: name, info: info})
		}
	}
	sort.Slice(files, func(i, j int) bool { return files[i].name < files[j].name })

	perFile := make(map[string]string, len(files))
	mtimes := make(map[string]fileStat, len(files))
	dataCache := make(map[string][]byte)
	compositeHasher := sha256.New()

	for _, f := range files {
		cur := fileStat{ModTime: f.info.ModTime().UnixNano(), Size: f.info.Size()}
		fullPath := filepath.Join(dir, f.name)

		// Mtime guard: reuse cached hash if mtime+size unchanged and file
		// is older than 2 seconds (safety window for coarse-mtime filesystems).
		if oldHashes != nil && oldMtimes != nil {
			age := time.Since(f.info.ModTime())
			if prev, ok := oldMtimes[f.name]; ok && age > 2*time.Second {
				if oldHash, hok := oldHashes[f.name]; hok && cur == prev {
					perFile[f.name] = oldHash
					mtimes[f.name] = cur
					compositeHasher.Write([]byte(oldHash))
					continue
				}
			}
		}

		data, rerr := os.ReadFile(fullPath)
		if rerr != nil {
			log.Printf("WARN: skip unreadable file %s: %v", f.name, rerr)
			continue
		}
		h := fmt.Sprintf("%x", sha256.Sum256(data))
		perFile[f.name] = h
		mtimes[f.name] = cur
		compositeHasher.Write([]byte(h))
		// Only cache bytes for files whose hash changed or is new (saves memory
		// in incremental path where 999/1000 files are unchanged).
		if oldHashes == nil {
			// First load: cache everything (fullDirLoad needs all bytes)
			dataCache[f.name] = data
		} else if oldH, ok := oldHashes[f.name]; !ok || oldH != h {
			// Changed or added file: cache for Phase 3 re-parse
			dataCache[f.name] = data
		}
	}

	return perFile, fmt.Sprintf("%x", compositeHasher.Sum(nil)), mtimes, dataCache, nil
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
	hasCache := m.fileHashes != nil && len(m.fileHashes) > 0
	m.mu.RUnlock()

	if !hasCache {
		return m.fullDirLoad()
	}

	// Phase 1: scan per-file hashes with mtime guard (cheap — stat + skip unchanged)
	m.mu.RLock()
	oldH := m.fileHashes
	oldM := m.fileMtimes
	prevHash := m.lastHash
	m.mu.RUnlock()

	newHashes, compositeHash, newMtimes, dataCache, err := scanDirFileHashes(m.path, oldH, oldM)
	if err != nil {
		return err
	}

	// Quick check: composite hash unchanged → no work needed
	unchanged := compositeHash == prevHash
	if unchanged {
		return nil
	}

	// Phase 2: diff per-file hashes → identify changed/added/removed
	m.mu.RLock()
	oldHashes := m.fileHashes
	oldConfigs := m.fileConfigs
	m.mu.RUnlock()

	var changed, added, removed []string

	// Detect changed and added files
	for name, newHash := range newHashes {
		oldHash, exists := oldHashes[name]
		if !exists {
			added = append(added, name)
		} else if newHash != oldHash {
			changed = append(changed, name)
		}
	}

	// Detect removed files
	for name := range oldHashes {
		if _, exists := newHashes[name]; !exists {
			removed = append(removed, name)
		}
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
	reparse := append(changed, added...)
	sort.Strings(reparse)
	for _, name := range reparse {
		fullPath := filepath.Join(m.path, name)
		data, ok := dataCache[name]
		if !ok {
			// Fallback: file not in cache (shouldn't happen, but be safe)
			var rerr error
			data, rerr = os.ReadFile(fullPath)
			if rerr != nil {
				log.Printf("WARN: skip unreadable file %s: %v", fullPath, rerr)
				delete(newConfigs, name)
				continue
			}
		}
		var partial ThresholdConfig
		if err := yaml.Unmarshal(data, &partial); err != nil {
			// See loadDir — defaults parse failure silently nullifies the
			// block; promote to ERROR + emit metric (cycle-6 RCA, archive §S#37d).
			IncParseFailure(filepath.Base(fullPath))
			if strings.HasPrefix(name, "_") {
				log.Printf("ERROR: skip unparseable defaults/profiles file %s: %v (entire block dropped — fix file or remove)", fullPath, err)
			} else {
				log.Printf("WARN: skip unparseable file %s: %v", fullPath, err)
			}
			delete(newConfigs, name)
			continue
		}
		// Apply boundary enforcement (same rules as loadDir)
		applyBoundaryRules(name, &partial)
		newConfigs[name] = partial
	}

	// Remove deleted files from cache
	for _, name := range removed {
		delete(newConfigs, name)
	}

	// Phase 4: merge — use incremental patch when only tenant files changed,
	// full rebuild when _defaults.yaml, _profiles.yaml, or _state_filters changed.
	tenantOnly := true
	for _, name := range append(changed, added...) {
		if name == "_defaults.yaml" || name == "_profiles.yaml" || strings.HasPrefix(name, "_") {
			tenantOnly = false
			break
		}
	}
	for _, name := range removed {
		if strings.HasPrefix(name, "_") {
			tenantOnly = false
			break
		}
	}
	var merged ThresholdConfig
	if tenantOnly && m.config != nil {
		// Incremental patch: copy existing merged config, patch only affected tenants.
		// This avoids O(N) merge for the common "1 tenant file changed" case.
		m.mu.RLock()
		prev := m.config
		m.mu.RUnlock()

		merged = ThresholdConfig{
			Defaults:     prev.Defaults,     // shared (immutable between patches)
			StateFilters: prev.StateFilters,  // shared
			Profiles:     prev.Profiles,      // shared
			Tenants:      make(map[string]map[string]ScheduledValue, len(prev.Tenants)),
		}
		// Shallow-copy tenants map (keys only, values are immutable per-tenant maps)
		for k, v := range prev.Tenants {
			merged.Tenants[k] = v
		}
		// Apply changes: overwrite tenants from re-parsed files
		for _, name := range append(changed, added...) {
			if partial, ok := newConfigs[name]; ok {
				for tenant, overrides := range partial.Tenants {
					merged.Tenants[tenant] = overrides
				}
			}
		}
		// Remove tenants from deleted files
		for _, name := range removed {
			if partial, ok := oldConfigs[name]; ok {
				for tenant := range partial.Tenants {
					delete(merged.Tenants, tenant)
				}
			}
		}
		// Profiles unchanged → no need to re-apply
	} else {
		// Full rebuild: _defaults or _profiles changed, must re-merge everything
		merged = mergePartialConfigs(newConfigs)
		merged.ApplyProfiles()
	}

	// Atomic swap
	m.mu.Lock()
	m.config = &merged
	m.loaded = true
	m.lastReload = time.Now()
	m.lastHash = compositeHash
	m.fileHashes = newHashes
	m.fileConfigs = newConfigs
	m.fileMtimes = newMtimes
	m.mu.Unlock()

	// Refresh config source detection (v2.3.0) — git-sync may rotate .git-revision
	m.detectConfigSource()

	logConfigStats(&merged, fmt.Sprintf("Config reloaded (incremental, %d changed, %d added, %d removed)", len(changed), len(added), len(removed)))

	return nil
}

// fullDirLoad performs a full directory load and initializes the per-file cache.
// Used for the initial load and as fallback for IncrementalLoad.
func (m *ConfigManager) fullDirLoad() error {
	// Compute per-file hashes (no mtime guard on first load)
	perFileHashes, compositeHash, perFileMtimes, dataCache, err := scanDirFileHashes(m.path, nil, nil)
	if err != nil {
		return err
	}

	if len(perFileHashes) == 0 {
		return fmt.Errorf("no .yaml files found in %s", m.path)
	}

	// Parse all files using cached bytes from scan (avoids double disk read).
	fileConfigs := make(map[string]ThresholdConfig, len(perFileHashes))
	var fileNames []string
	for name := range perFileHashes {
		fileNames = append(fileNames, name)
	}
	sort.Strings(fileNames)

	for _, name := range fileNames {
		fullPath := filepath.Join(m.path, name)
		data, ok := dataCache[name]
		if !ok {
			// Fallback: read from disk (shouldn't happen on first load)
			var rerr error
			data, rerr = os.ReadFile(fullPath)
			if rerr != nil {
				log.Printf("WARN: skip unreadable file %s: %v", fullPath, rerr)
				continue
			}
		}
		var partial ThresholdConfig
		if err := yaml.Unmarshal(data, &partial); err != nil {
			// See loadDir — defaults parse failure silently nullifies the
			// block; promote to ERROR + emit metric (cycle-6 RCA, archive §S#37d).
			IncParseFailure(filepath.Base(fullPath))
			if strings.HasPrefix(name, "_") {
				log.Printf("ERROR: skip unparseable defaults/profiles file %s: %v (entire block dropped — fix file or remove)", fullPath, err)
			} else {
				log.Printf("WARN: skip unparseable file %s: %v", fullPath, err)
			}
			continue
		}
		applyBoundaryRules(name, &partial)
		fileConfigs[name] = partial
	}

	// Merge all partials
	merged := mergePartialConfigs(fileConfigs)
	merged.ApplyProfiles()

	// v2.8.x issue #127: hierarchical scan runs BEFORE the flat-mode commit
	// for the same reason as Load() — a `*DuplicateTenantError` rejects the
	// reload at the boundary, leaving the prior known-good state intact and
	// serving. Generic scan errors keep the log-and-continue policy
	// (hierarchical mode is opt-in).
	if err := m.populateHierarchyState(); err != nil {
		var dupErr *DuplicateTenantError
		if errors.As(err, &dupErr) {
			return fmt.Errorf("config rejected (mixed-mode duplicate tenant): %w", err)
		}
		log.Printf("WARN: hierarchical scan during fullDirLoad failed: %v", err)
	}

	m.mu.Lock()
	m.config = &merged
	m.loaded = true
	m.lastReload = time.Now()
	m.lastHash = compositeHash
	m.fileHashes = perFileHashes
	m.fileConfigs = fileConfigs
	m.fileMtimes = perFileMtimes
	m.mu.Unlock()

	// Detect config source mode and git commit (v2.3.0)
	m.detectConfigSource()

	logConfigStats(&merged, fmt.Sprintf("Config loaded (%s)", m.Mode()))

	return nil
}

// populateHierarchyState runs scanDirHierarchical against m.path and
// installs the resulting graph + per-tenant merged_hash onto the
// ConfigManager. Safe to call after any fullDirLoad or IncrementalLoad.
//
// The function returns nil if no _defaults.yaml is anywhere in the tree
// (flat mode — hierarchicalMode stays false; nothing to populate). A
// non-nil error means the scan or merge pipeline hit a real failure; the
// caller logs and leaves prior state untouched.
//
// Memory: the hashes map may be large at 1000 tenants (roughly
// tenants × 64-char strings = ~100KB). We swap the pointer rather than
// merging in place so a failed scan doesn't leave torn state visible to
// the /effective read path.
func (m *ConfigManager) populateHierarchyState() error {
	tenants, defaults, hashes, mtimes, graph, err := scanDirHierarchical(m.path, nil)
	if err != nil {
		return err
	}
	if len(defaults) == 0 && len(tenants) == 0 {
		// Empty tree or flat layout with no files we recognize. Don't
		// flip hierarchicalMode — a later add-a-_defaults-file event will
		// flip it via diffAndReload.
		return nil
	}

	newMergedHashes := make(map[string]string, len(tenants))
	for tid, srcPath := range tenants {
		chain := graph.TenantDefaults[tid]
		mh, mergeErr := m.recomputeMergedHash(tid, srcPath, chain)
		if mergeErr != nil {
			logMergeSkip(tid, "initial-hierarchy-scan", mergeErr)
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
			log.Printf("WARN: parsedDefaults cache: read %s: %v", dp, rerr)
			continue
		}
		parsed, perr := parseDefaultsBytes(b)
		if perr != nil {
			log.Printf("WARN: parsedDefaults cache: parse %s: %v", dp, perr)
			continue
		}
		newParsedDefaults[dp] = parsed
	}

	m.mu.Lock()
	// Only flip hierarchicalMode on once we've seen a _defaults.yaml
	// somewhere. Pure-flat trees keep hierarchicalMode=false, letting
	// WatchLoop take the v2.6.0 IncrementalLoad path.
	if len(defaults) > 0 {
		m.hierarchicalMode = true
	}
	m.tenantSources = tenants
	m.hierarchyHashes = hashes
	m.hierarchyMtimes = mtimes
	m.mergedHashes = newMergedHashes
	m.inheritanceGraph = graph
	m.parsedDefaults = newParsedDefaults
	m.mu.Unlock()
	return nil
}

// logConfigStats logs config summary with cheap counts instead of calling
// the expensive Resolve()/ResolveStateFilters()/ResolveSilentModes().
// At 1000 tenants, this saves ~4ms per reload (Resolve alone costs ~2-5ms).
// The "resolved thresholds" count is estimated from tenant override counts
// rather than running the full resolution pipeline.
func logConfigStats(cfg *ThresholdConfig, prefix string) {
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

	log.Printf("%s: %d defaults, %d profiles, %d state_filters, %d tenants, ~%d threshold overrides, %d state entries, %d silent modes",
		prefix, len(cfg.Defaults), len(cfg.Profiles), len(cfg.StateFilters), len(cfg.Tenants),
		overrideCount, stateCount, silentCount)

	if warnings := cfg.ValidateTenantKeys(); len(warnings) > 0 {
		for _, w := range warnings {
			log.Printf("%s", w)
		}
	}
}

// applyBoundaryRules enforces the boundary convention: state_filters and
// defaults only in _defaults.yaml, profiles only in _profiles.yaml.
func applyBoundaryRules(name string, partial *ThresholdConfig) {
	isDefaultsFile := strings.HasPrefix(name, "_")
	isProfilesFile := name == "_profiles.yaml" || name == "_profiles.yml"

	if !isDefaultsFile {
		if len(partial.StateFilters) > 0 {
			log.Printf("WARN: state_filters found in %s — should only be in _defaults.yaml, ignoring", name)
			partial.StateFilters = nil
		}
		if len(partial.Defaults) > 0 {
			log.Printf("WARN: defaults found in %s — should only be in _defaults.yaml, ignoring", name)
			partial.Defaults = nil
		}
	}
	if !isProfilesFile && !isDefaultsFile {
		if len(partial.Profiles) > 0 {
			log.Printf("WARN: profiles found in %s — should only be in _profiles.yaml, ignoring", name)
			partial.Profiles = nil
		}
	}
}

// mergePartialConfigs merges all cached partial configs in sorted filename order.
// Same merge semantics as loadDir: defaults/state_filters overwrite, tenants/profiles deep merge.
func mergePartialConfigs(configs map[string]ThresholdConfig) ThresholdConfig {
	// Pre-scan to estimate map capacities, avoiding rehash during merge.
	// In directory mode each tenant file has exactly 1 tenant, so
	// len(configs) is a reasonable upper bound for the Tenants map.
	tenantCap := 0
	defaultCap := 0
	for _, partial := range configs {
		tenantCap += len(partial.Tenants)
		if len(partial.Defaults) > defaultCap {
			defaultCap = len(partial.Defaults)
		}
	}

	merged := ThresholdConfig{
		Defaults:     make(map[string]float64, defaultCap),
		StateFilters: make(map[string]StateFilter),
		Tenants:      make(map[string]map[string]ScheduledValue, tenantCap),
		Profiles:     make(map[string]map[string]ScheduledValue),
	}

	// Sort filenames for deterministic merge order
	names := make([]string, 0, len(configs))
	for name := range configs {
		names = append(names, name)
	}
	sort.Strings(names)

	for _, name := range names {
		partial := configs[name]

		for k, v := range partial.Defaults {
			merged.Defaults[k] = v
		}
		for k, v := range partial.StateFilters {
			merged.StateFilters[k] = v
		}
		for profileName, profileValues := range partial.Profiles {
			if merged.Profiles[profileName] == nil {
				merged.Profiles[profileName] = make(map[string]ScheduledValue)
			}
			for k, v := range profileValues {
				merged.Profiles[profileName][k] = v
			}
		}
		for tenant, overrides := range partial.Tenants {
			if merged.Tenants[tenant] == nil {
				merged.Tenants[tenant] = make(map[string]ScheduledValue, len(overrides))
			}
			for k, v := range overrides {
				merged.Tenants[tenant][k] = v
			}
		}
	}

	return merged
}

// WatchLoop periodically checks for config changes and reloads.
// Uses content hash comparison for reliable change detection.
// K8s ConfigMap volumes update via symlink rotation (..data), so hash-based
// detection is more reliable than ModTime for both modes.
// The stopCh parameter allows graceful shutdown — close it to stop the loop.
//
// In directory mode, uses incremental reload (v2.1.0): per-file hash tracking
// means only changed files are re-parsed, reducing reload latency for large
// multi-tenant deployments.
func (m *ConfigManager) WatchLoop(interval time.Duration, stopCh <-chan struct{}) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-stopCh:
			log.Println("WatchLoop stopped")
			return
		case <-ticker.C:
		}

		if m.isDir {
			changed, reason, err := m.detectChange()
			if err != nil {
				log.Printf("WARN: cannot check config %s: %v", m.path, err)
				continue
			}
			if changed {
				log.Printf("Config changed, scheduling debounced reload...")
				// v2.7.0: route through debounce even for flat mode so an
				// ops tool that rapidly rewrites multiple files coalesces
				// into a single reload.
				m.triggerDebouncedReload(reason)
			}
			continue
		}

		// Single-file mode: full reload (no incremental benefit)
		_, hash, err := loadFile(m.path)
		if err != nil {
			log.Printf("WARN: cannot check config %s: %v", m.path, err)
			continue
		}

		m.mu.RLock()
		changed := hash != m.lastHash
		m.mu.RUnlock()

		if changed {
			log.Printf("Config changed, reloading...")
			if err := m.Load(); err != nil {
				log.Printf("ERROR: failed to reload config: %v", err)
			}
		}
	}
}

// detectChange runs directory-mode change detection — the flat-scan
// (v2.1.0) or hierarchical-scan (v2.8.0 A-10 fix, Issue #52) path
// depending on whether hierarchical mode has been activated for this
// config root.
//
//   - Flat: scanDirFileHashes of top-level files, mtime-guard cheap
//     stat, composite hash compare. Returns reason=source so the
//     debounce path emits da_config_reload_trigger_total{reason="source"}.
//   - Hierarchical: scanDirHierarchical (recursive, sees nested tenant
//     files under <domain>/<region>/). Any file added/removed/changed
//     constitutes a change. Returns reason=forced; diffAndReload will
//     categorize the actual reason via its per-tenant hash compare.
//
// O(N) compare for hierarchical mode is acceptable: at WatchInterval
// cadence (30s default) with 1000 files it adds ~1k comparisons/30s —
// negligible. Disk-read cost is in scanDirHierarchical itself; mtime-
// guard optimization is reserved for Phase 3 (see config_hierarchy.go).
//
// v2.8.0 PR-3: extracted from WatchLoop so the dual-path lives in a
// named seam. Single-file mode stays inline in WatchLoop because its
// reload semantics differ (synchronous m.Load instead of debounced).
//
// Caller responsibilities: log the (warning-level) error, and on
// changed=true call triggerDebouncedReload(reason).
func (m *ConfigManager) detectChange() (bool, string, error) {
	m.mu.RLock()
	oldH := m.fileHashes
	oldM := m.fileMtimes
	prevHash := m.lastHash
	hierarchical := m.hierarchicalMode
	priorHierHashes := m.hierarchyHashes
	priorHierMtimes := m.hierarchyMtimes
	m.mu.RUnlock()

	if hierarchical {
		_, _, newHashes, _, _, hErr := scanDirHierarchical(m.path, priorHierMtimes)
		if hErr != nil {
			return false, "", fmt.Errorf("hierarchical scan: %w", hErr)
		}
		changed := false
		if len(newHashes) != len(priorHierHashes) {
			changed = true
		} else {
			for k, v := range newHashes {
				if priorHierHashes[k] != v {
					changed = true
					break
				}
			}
		}
		return changed, ReloadReasonForced, nil
	}

	_, compositeHash, _, _, err := scanDirFileHashes(m.path, oldH, oldM)
	if err != nil {
		return false, "", err
	}
	return compositeHash != prevHash, ReloadReasonSource, nil
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
	TenantID      string            // tenant identifier
	SourceFile    string            // absolute path to tenant YAML
	SourceHash    string            // SHA-256[:16] of raw tenant bytes
	MergedHash    string            // SHA-256[:16] of canonical merged JSON
	DefaultsChain []string          // L0→Ln defaults file paths (root first)
	Config        map[string]any    // merged tenant config (full dict)
	Warnings      []string          // merge-time warnings (currently empty)
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
	srcPath, known := m.tenantSources[tenantID]
	var chain []string
	if m.inheritanceGraph != nil {
		chain = append(chain, m.inheritanceGraph.TenantDefaults[tenantID]...)
	}
	cachedHash := m.mergedHashes[tenantID]
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
func (m *ConfigManager) detectConfigSource() {
	gitCommit := ""
	configSource := "configmap"

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

	m.configSource = configSource
	m.gitCommit = gitCommit
}
