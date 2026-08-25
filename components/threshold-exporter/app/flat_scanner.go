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
	"io/fs"
	"log"
	"os"
	"path"
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
// scanKeyBase is the underscore convention's unit of judgement: the FILE NAME,
// not the whole scan key. Keys are root-relative slash paths since #1521
// (`nested/_defaults.yaml`), and every `_`-prefix test in this package means
// "is this file platform-scoped" — a question the directory part cannot answer.
// One helper so the three call sites cannot drift apart, and so
// `parsePartialConfig`, whose own parameter is named `path`, can ask it without
// shadowing the package.
func scanKeyBase(key string) string { return path.Base(key) }

// resolveScanRoot is the ONE derivation of "which directory is the conf.d
// root" that every enumerator over that tree must use.
//
// ⛔ IT EXISTS BECAUSE HAVING TWO OF THEM IS THIS TICKET'S ENTIRE DEFECT
// CLASS. `filepath.WalkDir` lstats its root and never follows a symlink, so
// each scanner that starts from an unresolved `-config-dir` silently sees an
// EMPTY tree when that path is a link. Fixing only the flat scanner produced
// exactly the split this PR closes, one layer down: measured on a symlinked
// root, `GetConfig()` had the tenant while `hierarchy.enabled` was false and
// `tenantSources` was empty, so the tenant's series carried the ROOT default
// (50) instead of the subtree's (90) — and the divergence audit reports only
// the opposite direction, so the gauge stayed at 0. (#1569 blind review.)
//
// ⚠️ Falls back to the given path when resolution fails (dangling link,
// permission), so the caller's own error handling still decides — this
// function never turns a broken path into a different one.
// isNestedPlatformFile reports whether a scan key names an underscore-prefixed
// platform file BELOW the conf.d root.
//
// ⛔ ONE PREDICATE, TWO CALLERS, ON PURPOSE. `fullDirLoad` and
// `IncrementalLoad` each decide which files reach the merged config, and a
// predicate copied into both is precisely the shape of the defect this whole
// change set exists to close: two enumerations over one tree that can drift
// apart silently. (CodeRabbit, #1569.)
func isNestedPlatformFile(key string) bool {
	return strings.Contains(key, "/") && strings.HasPrefix(scanKeyBase(key), "_")
}

// absScanRoot is resolveScanRoot preceded by the absolutisation every caller
// needs and one of them once forgot.
//
// ⛔ `m.path` is whatever `-config-dir` was given, frequently relative, while
// the hierarchical scanner stores absolute paths. Comparing the two without
// this made EVERY defaults file look like a subtree file — measured on the
// repo's own flat golden fixtures, whose ROOT defaults keys were then copied
// into tenant maps. That was fixed in place; this hoists the three-line
// derivation out of the one caller that had it so a second caller cannot get
// it subtly different. (#1569 sweep B-2.)
func absScanRoot(dir string) string {
	clean := filepath.Clean(dir)
	if abs, err := filepath.Abs(dir); err == nil {
		clean = filepath.Clean(abs)
	}
	return resolveScanRoot(clean)
}

func resolveScanRoot(dir string) string {
	if resolved, err := filepath.EvalSymlinks(dir); err == nil {
		return resolved
	}
	return dir
}

// reportUnparseableNestedPlatformFile keeps a genuinely broken nested
// `_defaults.yaml` / `_profiles.yaml` loud, even though its content is
// deliberately excluded from the merged config.
//
// ⛔ THE DISTINCTION IS BETWEEN "BROKEN" AND "NOT FOR THIS PLANE", and losing
// it was a severity downgrade. `Defaults` is `map[string]float64`, so a
// perfectly valid subtree defaults file written in the schedule form fails to
// decode into `ThresholdConfig` — running the full parse on files this plane
// discards therefore logged an ERROR for healthy trees. Skipping them outright
// then went too far the other way: a file with real syntax damage stopped
// incrementing `parse_failure` and stopped logging at all. A syntax-only probe
// answers the right question — the same one `ERROR:` has always meant here.
func reportUnparseableNestedPlatformFile(fullPath string, data []byte, metrics *configMetrics, logger *log.Logger) {
	var probe any
	err := yaml.Unmarshal(data, &probe)
	if err == nil {
		return // syntactically fine; its content simply is not for this plane
	}
	metrics.IncParseFailure(filepath.Base(fullPath))
	logger.Printf("ERROR: skip unparseable defaults/profiles file %s: %v (entire block dropped — fix file or remove)", fullPath, err)
}

func parsePartialConfig(name, path string, data []byte, metrics *configMetrics, logger *log.Logger) (ThresholdConfig, bool) {
	var partial ThresholdConfig
	if err := yaml.Unmarshal(data, &partial); err != nil {
		metrics.IncParseFailure(filepath.Base(path))
		if strings.HasPrefix(scanKeyBase(name), "_") {
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
	// ⛔ RECURSIVE since #1521. It was `os.ReadDir` + `if IsDir() { continue }`,
	// which gave this scanner a SMALLER population than the hierarchical one
	// reading the same tree: a tenant one directory down resolved through
	// `/effective` and emitted no `user_threshold` at all, with no error and no
	// metric to notice it by. See `config_nested_tenant_test.go`.
	//
	// ⛔ THE MAP KEY IS NOW A ROOT-RELATIVE SLASH PATH, not a bare filename, and
	// that is load-bearing rather than cosmetic. With bare names `a/x.yaml` and
	// `x.yaml` collide in `perFile`/`mtimes`/`dataCache` and one of the two
	// tenants disappears silently — the same failure this change exists to fix.
	// Callers already rebuild the path as `filepath.Join(m.path, name)`, which
	// stays correct for a relative path, so the key change is invisible to them.
	//
	// ⚠️ WalkDir's error is NOT swallowed: a missing root has to stay a hard
	// error (the caller treats it as "config dir unreadable"), so it is checked
	// up front rather than left to the callback.
	if _, serr := os.Stat(dir); serr != nil {
		return nil, "", nil, nil, fmt.Errorf("read config dir %s: %w", dir, serr)
	}
	// ⛔ THE WALK ROOT IS THE RESOLVED PATH, and that is a fix rather than a
	// tidy-up. `os.Stat` above follows symlinks, but `filepath.WalkDir` LSTATS
	// its root and never follows one — so a `-config-dir` that is a symlink to
	// the real directory was visited once as a non-directory, matched no
	// `.yaml` suffix, and the walk ended with zero files and a nil error.
	// `fullDirLoad`'s empty guard then failed the whole load with
	// "no .yaml files found". Measured: `Load(real dir)` nil vs
	// `Load(symlink)` "no .yaml files found", at uid 0 and uid 65534 alike.
	// The pre-#1521 `os.ReadDir` followed the link, so this was a regression
	// introduced by the recursion, not a pre-existing gap.
	//
	// ⚠️ Only the ROOT is resolved. Symlinked entries INSIDE the tree are
	// still not followed — that is WalkDir's documented behaviour, it matches
	// the hierarchical scanner walking the same tree, and following them would
	// open a cycle risk that neither scanner is written to survive.
	walkRoot := resolveScanRoot(dir)

	type dirFile struct {
		name string      // root-relative, slash-separated
		info os.FileInfo // from DirEntry.Info(), avoids separate os.Stat
	}
	var files []dirFile
	walkErr := filepath.WalkDir(walkRoot, func(full string, entry fs.DirEntry, werr error) error {
		if werr != nil {
			// One unreadable subtree must not blank the whole config; the
			// hierarchical scanner reading the same tree logs and continues too.
			logger.Printf("WARN: skip unreadable path %s: %v", full, werr)
			if entry != nil && entry.IsDir() {
				return fs.SkipDir
			}
			return nil
		}
		name := entry.Name()
		if entry.IsDir() {
			// Dot-prefixed directories are pruned whole — which also covers the
			// K8s ConfigMap symlink shims (`..data`, `..2026_04_25_…`), since a
			// `..` name is a `.` name. Measured on a real mount layout: both
			// scanners see the two root-level entries and nothing doubled.
			if full != walkRoot && strings.HasPrefix(name, ".") {
				return fs.SkipDir
			}
			return nil
		}
		if strings.HasPrefix(name, ".") {
			return nil
		}
		// ⛔ CASE-INSENSITIVE since #1521, matching `config_hierarchy.go` and the
		// loader itself. It used to be exact, so `UPPER.YAML` AT THE ROOT — no
		// nesting involved — produced the identical symptom: found by `Resolve()`,
		// absent from `/metrics`. Two enumerators over one tree with two
		// different skip rules is the defect class; this closes the second half.
		lower := strings.ToLower(name)
		if !strings.HasSuffix(lower, ".yaml") && !strings.HasSuffix(lower, ".yml") {
			return nil
		}
		info, ierr := entry.Info()
		if ierr != nil {
			logger.Printf("WARN: skip unreadable entry %s: %v", full, ierr)
			return nil
		}
		rel, rerr := filepath.Rel(walkRoot, full)
		if rerr != nil {
			logger.Printf("WARN: skip path outside root %s: %v", full, rerr)
			return nil
		}
		files = append(files, dirFile{name: filepath.ToSlash(rel), info: info})
		return nil
	})
	if walkErr != nil {
		return nil, "", nil, nil, fmt.Errorf("read config dir %s: %w", dir, walkErr)
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
	// ⛔ BASENAME, not the whole key. Keys are root-relative since #1521, so
	// `HasPrefix(name, "_")` would read `nested/_defaults.yaml` as a TENANT file
	// and strip its platform sections with a WARN — quietly, and for a file the
	// convention plainly marks as platform-scoped.
	base := scanKeyBase(name)
	isDefaultsFile := strings.HasPrefix(base, "_")
	isProfilesFile := base == "_profiles.yaml" || base == "_profiles.yml"

	if !isDefaultsFile {
		if len(partial.StateFilters) > 0 {
			logger.Printf("WARN: state_filters found in %s — should only be in _defaults.yaml, ignoring", name)
			partial.StateFilters = nil
		}
		// ⛔ SECURITY: same boundary as Defaults, and for a sharper reason. A
		// tenant file naming its own keys here would be self-authorising: the
		// write gate refuses keys outside the platform surface, so a tenant
		// that could extend that surface from its own file would walk straight
		// past the refusal via a direct GitOps push (tenant-api is not the only
		// writer). Strip and say so.
		if len(partial.OptionalOverrides) > 0 {
			logger.Printf("WARN: optional_overrides found in %s — platform-scoped, should only be in _defaults.yaml, ignoring", name)
			partial.OptionalOverrides = nil
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
	// Union, not replace: like Defaults above, several `_defaults.yaml` files
	// across the directory tree each contribute part of the platform surface.
	// De-duplicated because the same key legitimately appears at more than one
	// level of the hierarchy.
	if len(partial.OptionalOverrides) > 0 {
		seen := make(map[string]struct{}, len(merged.OptionalOverrides))
		for _, k := range merged.OptionalOverrides {
			seen[k] = struct{}{}
		}
		for _, k := range partial.OptionalOverrides {
			if _, dup := seen[k]; dup {
				continue
			}
			seen[k] = struct{}{}
			merged.OptionalOverrides = append(merged.OptionalOverrides, k)
		}
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
