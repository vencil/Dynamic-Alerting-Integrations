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
//   applyBoundaryRules(...)  — enforce "state_filters / defaults /
//                              optional_overrides only in a defaults
//                              carrier; profiles only in `_` files"
//                              convention (#1676).
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

	"github.com/vencil/threshold-exporter/internal/confdname"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// loadFile reads a single YAML config file and returns the parsed config + content hash.
func loadFile(path string) (ThresholdConfig, string, error) {
	var cfg ThresholdConfig

	data, err := os.ReadFile(path)
	if err != nil {
		return cfg, "", fmt.Errorf("read config %s: %w", path, err)
	}

	hash := fmt.Sprintf("%x", sha256.Sum256(data))

	cfg, err = config.ParseConfigFile(data)
	if err != nil {
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

// parsePartialConfig decodes one config file's bytes with the ONE decode,
// config.ParseConfigFile (#1957) — the same function the conf.d walker judges
// tenant files with, so a file this rejects declares no tenant on any plane.
// On parse failure it records the parse_failure metric and logs — ERROR for
// underscore-prefixed files (a broken _defaults/_profiles silently nullifies an
// entire block → every dependent tenant override breaks; cycle-6 RCA, planning
// archive §S#37d, cost 5+ hours at WARN) or WARN for tenant files — then
// returns ok=false so the caller can skip the file. `name` is the base filename
// (drives the underscore severity choice); `path` is the display path used for
// logs and the metric basename. Shared by IncrementalLoad and fullDirLoad so
// the flat-mode parse paths report failures identically.
func parsePartialConfig(name, path string, data []byte, metrics *configMetrics, logger *log.Logger) (ThresholdConfig, bool) {
	partial, err := config.ParseConfigFile(data)
	if err != nil {
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

// applyBoundaryRules enforces the boundary convention: state_filters,
// optional_overrides and defaults only in a defaults CARRIER, profiles only
// in a platform (`_`-prefixed) file. logger may be nil → falls back to
// log.Default() (production safety).
//
// ⛔ TWO QUESTIONS, NOT ONE (#1676). This used to ask only "is the basename
// `_`-prefixed" and let every platform file through, so a root
// `_defaults-multidb.yaml` or `_profiles.yaml` carrying a `defaults:` block
// had it merged into the ONE global Defaults map served on /metrics — while
// the inheritance chain (/effective, describe_tenant) reads only the carrier
// and never saw it. "Is this a platform file" and "is this the defaults
// carrier" are separate predicates; the carrier one is the SSOT
// confdname.IsDefaults, the same one the walker classifies with.
//
// A carrier is necessary, not sufficient: when a directory has more than one
// (`_defaults.yaml` + `_defaults.yml`) only the one the chain selects is
// read — and the others are not read at all. That is a TREE property, so it
// is decided by the callers (commitFlatFrom skips an unselected root carrier
// before parsing; see isUnselectedRootCarrier), not per file here.
func applyBoundaryRules(name string, partial *ThresholdConfig, logger *log.Logger) {
	if logger == nil {
		logger = log.Default()
	}
	// ⛔ BASENAME, not the whole key. Keys are root-relative since #1521, so
	// `HasPrefix(name, "_")` would read `nested/_defaults.yaml` as a TENANT file
	// and strip its platform sections with a WARN — quietly, and for a file the
	// convention plainly marks as platform-scoped.
	base := scanKeyBase(name)
	isPlatformFile := confdname.IsReserved(base)
	isCarrier := confdname.IsDefaults(base)

	if isPlatformFile && !isCarrier {
		// ⛔ Loud, and the same shape as the tenant-file strip below: the
		// operator put these keys here on purpose and must learn they are not
		// served. Profiles stay allowed from any platform file (their own
		// convention, unchanged by #1676).
		if len(partial.Defaults) > 0 {
			logger.Printf("WARN: defaults found in %s — not a defaults carrier (only _defaults.yaml / _defaults.yml is), ignoring", name)
			partial.Defaults = nil
		}
		if len(partial.StateFilters) > 0 {
			logger.Printf("WARN: state_filters found in %s — not a defaults carrier (only _defaults.yaml / _defaults.yml is), ignoring", name)
			partial.StateFilters = nil
		}
		if len(partial.OptionalOverrides) > 0 {
			logger.Printf("WARN: optional_overrides found in %s — platform-scoped, not a defaults carrier (only _defaults.yaml / _defaults.yml is), ignoring", name)
			partial.OptionalOverrides = nil
		}
	}

	if !isPlatformFile {
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
	if !isPlatformFile {
		if len(partial.Profiles) > 0 {
			logger.Printf("WARN: profiles found in %s — should only be in _profiles.yaml, ignoring", name)
			partial.Profiles = nil
		}
	}
}

// mergePartialConfigs merges all cached partial configs in sorted filename order
// via mergePartialInto: defaults/state_filters overwrite, tenants/profiles deep merge.
// An unselected root defaults carrier never reaches `configs` (#1674; see
// isUnselectedRootCarrier), so every file merged here is one the chain reads
// or a non-carrier the boundary rules have already judged.
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

// isRootCarrierKey reports whether a scan key is a defaults carrier at the
// conf.d ROOT (nested carriers never reach the flat merge at all).
func isRootCarrierKey(key string) bool {
	return !strings.Contains(key, "/") && confdname.IsDefaults(key)
}

// isUnselectedRootCarrier reports whether a scan key is a ROOT defaults
// carrier other than the one the chain selected (rootCarrierKey).
//
// ⛔ Such a file contributes NOTHING to the flat plane — not defaults, not
// state_filters, not optional_overrides, and not profiles or tenants either
// (#1674). The multi-carrier WARN says it "is ignored on every plane", and a
// version that dropped only the three carrier sections left an unselected
// `_defaults.yml` still adding a `profiles:` entry and a `tenants:` entry to
// /metrics (blind review). It is therefore never parsed or cached here
// (commitFlatFrom skips it), and any change to a root carrier sends the
// incremental path to the full load (anyRootCarrierKey), so a cached partial
// cannot outlive a selection that moved.
func isUnselectedRootCarrier(key, rootCarrier string) bool {
	return isRootCarrierKey(key) && key != rootCarrier
}

// anyRootCarrierKey reports whether a reload touched a root defaults carrier,
// which can move the root selection.
func anyRootCarrierKey(groups ...[]string) bool {
	for _, g := range groups {
		for _, k := range g {
			if isRootCarrierKey(k) {
				return true
			}
		}
	}
	return false
}

// rootCarrierKey returns the scan key of the root directory's selected
// defaults carrier ("" when the root has none), and WARNs once per call for
// every directory in the tree holding more than one carrier.
//
// ⛔ CALLED ONLY WHERE A FULL LOAD BUILDS THE FLAT CONFIG (commitFlatFrom),
// never on the quiet watch tick — detectChange does not merge — so a
// misconfigured tree logs once per load/reload, not every WatchInterval.
// The selection is the scan's own (TreeScan.DefaultsCarriers), the one that
// scan's inheritance graph is built from, so within one load /metrics' root
// Defaults and the chain's L0 are the same file. ⚠️ That is a statement
// about a single scan, not about merged_hash across reloads: a reload that
// deletes a chain file can leave the exporter's cached merged_hash stale
// (#1964, pre-existing, not addressed here).
func rootCarrierKey(scan *treeScan, logger *log.Logger) string {
	sel := scan.DefaultsCarriers()
	for _, w := range sel.AmbiguityWarnings() {
		logger.Print(w)
	}
	chosen, ok := sel.ByDir[scan.AbsRoot]
	if !ok {
		return ""
	}
	rel, err := filepath.Rel(scan.AbsRoot, chosen)
	if err != nil {
		return ""
	}
	return filepath.ToSlash(rel)
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
