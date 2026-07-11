package main

// Flat-mode directory scanner + per-file YAML cache + multi-file merge.
//
// v2.8.0 PR-7 split out of config.go to live next to flatScanState
// (PR-5). The flat-mode pipeline is what `IncrementalLoad` and
// `fullDirLoad` execute; `loadFile` is the single-file fallback used
// by `Load` when m.path points at a file rather than a directory.
//
// Functions:
//
//   loadFile(path)           — single YAML file → ThresholdConfig + hash.
//                              Directory mode has no separate eager loader:
//                              Load delegates to fullDirLoad (config.go) so
//                              the initial load and the watch loop share one
//                              composite-hash construction + per-file cache.
//   scanDirFileHashes(...)   — per-file SHA-256 + mtime-fast-path stat.
//                              Caches file bytes for the parse phase to
//                              avoid double disk read. Used by
//                              IncrementalLoad + fullDirLoad.
//   applyBoundaryRules(...)  — enforce "state_filters / defaults only
//                              in _defaults.yaml; profiles only in
//                              _profiles.yaml" convention.
//   mergePartialConfigs(...) — deep-merge per-file partials into a
//                              single ThresholdConfig (used by
//                              fullDirLoad + IncrementalLoad
//                              full-rebuild branch).

import (
	"crypto/sha256"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

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

// parsePartialConfig unmarshals one config file's bytes into a ThresholdConfig.
// On parse failure it records the parse_failure metric and logs — ERROR for
// underscore-prefixed files (a broken _defaults/_profiles silently nullifies an
// entire block → every dependent tenant override breaks; cycle-6 RCA, planning
// archive §S#37d, cost 5+ hours at WARN) or WARN for tenant files — then
// returns ok=false so the caller can skip the file. `name` is the base filename
// (drives the underscore severity choice); `path` is the display path used for
// logs and the metric basename. Shared by IncrementalLoad and fullDirLoad so
// the flat-mode parse paths report failures identically.
func parsePartialConfig(name, path string, data []byte, metrics *configMetrics, logger *log.Logger) (ThresholdConfig, bool) {
	var partial ThresholdConfig
	if err := yaml.Unmarshal(data, &partial); err != nil {
		metrics.IncParseFailure(filepath.Base(path))
		if strings.HasPrefix(name, "_") {
			logger.Printf("ERROR: skip unparseable defaults/profiles file %s: %v (entire block dropped — fix file or remove)", path, err)
		} else {
			logger.Printf("WARN: skip unparseable file %s: %v", path, err)
		}
		return partial, false
	}
	return partial, true
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
func scanDirFileHashes(dir string, oldHashes map[string]string, oldMtimes map[string]fileStat, logger *log.Logger) (map[string]string, string, map[string]fileStat, map[string][]byte, error) {
	if logger == nil {
		logger = log.Default()
	}
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
				logger.Printf("WARN: skip unreadable entry %s: %v", name, ierr)
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
			logger.Printf("WARN: skip unreadable file %s: %v", f.name, rerr)
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
// applyBoundaryRules enforces the boundary convention: state_filters and
// defaults only in _defaults.yaml, profiles only in _profiles.yaml.
// logger may be nil → falls back to log.Default() (production safety).
func applyBoundaryRules(name string, partial *ThresholdConfig, logger *log.Logger) {
	if logger == nil {
		logger = log.Default()
	}
	isDefaultsFile := strings.HasPrefix(name, "_")
	isProfilesFile := name == "_profiles.yaml" || name == "_profiles.yml"

	if !isDefaultsFile {
		if len(partial.StateFilters) > 0 {
			logger.Printf("WARN: state_filters found in %s — should only be in _defaults.yaml, ignoring", name)
			partial.StateFilters = nil
		}
		if len(partial.Defaults) > 0 {
			logger.Printf("WARN: defaults found in %s — should only be in _defaults.yaml, ignoring", name)
			partial.Defaults = nil
		}
	}
	if !isProfilesFile && !isDefaultsFile {
		if len(partial.Profiles) > 0 {
			logger.Printf("WARN: profiles found in %s — should only be in _profiles.yaml, ignoring", name)
			partial.Profiles = nil
		}
	}
}

// mergePartialConfigs merges all cached partial configs in sorted filename order
// via mergePartialInto: defaults/state_filters overwrite, tenants/profiles deep merge.
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
		mergePartialInto(&merged, configs[name])
	}

	return merged
}

// mergePartialInto deep-merges one partial config into merged using the
// flat-mode merge semantics shared by mergePartialConfigs (full rebuild) and
// the IncrementalLoad diff path: defaults and state_filters overwrite by key;
// profiles and tenants deep-merge per name (later values win). Keeping this in
// one place guarantees the full-rebuild and incremental paths can never drift
// in merge precedence.
func mergePartialInto(merged *ThresholdConfig, partial ThresholdConfig) {
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

// WatchLoop periodically checks for config changes and reloads.
// Uses content hash comparison for reliable change detection.
// K8s ConfigMap volumes update via symlink rotation (..data), so hash-based
// detection is more reliable than ModTime for both modes.
// The stopCh parameter allows graceful shutdown — close it to stop the loop.
//
// In directory mode, uses incremental reload (v2.1.0): per-file hash tracking
// means only changed files are re-parsed, reducing reload latency for large
// multi-tenant deployments.
