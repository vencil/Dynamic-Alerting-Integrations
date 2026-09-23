package main

// Flat-mode parse helpers + multi-file merge. The directory walk itself
// lives in pkg/config/tree_scan.go (config.ScanDirTree, one walk for both
// planes since #1568, moved out of this package in #1941; reached through
// scanDirTree in config_tree_scan.go); the flat scanner that used to live
// here survives only as a test projection in scan_wrappers_test.go.
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
//   (absScanRoot, the ONE derivation of the conf.d root, moved with the
//   walker to pkg/config in #1941; config_tree_scan.go forwards to it.)
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
	"path"
	"path/filepath"
	"sort"
	"strings"

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

// scanKeyBase is the underscore convention's unit of judgement: the FILE NAME,
// not the whole scan key. Keys are root-relative slash paths since #1521
// (`nested/_defaults.yaml`), and every `_`-prefix test in this package means
// "is this file platform-scoped" — a question the directory part cannot answer.
// One helper so the three call sites cannot drift apart, and so
// `parsePartialConfig`, whose own parameter is named `path`, can ask it without
// shadowing the package.
func scanKeyBase(key string) string { return path.Base(key) }

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
		if strings.HasPrefix(scanKeyBase(name), "_") {
			logger.Printf("ERROR: skip unparseable defaults/profiles file %s: %v (entire block dropped — fix file or remove)", path, err)
		} else {
			logger.Printf("WARN: skip unparseable file %s: %v", path, err)
		}
		return partial, false
	}
	return partial, true
}

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
