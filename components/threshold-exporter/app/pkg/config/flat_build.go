package config

// Flat-plane parse helpers + multi-file merge, and BuildFlatConfig — the
// build of the ThresholdConfig the exporter's collector serves. Moved from
// package main (flat_scanner.go, and the body of config.go's commitFlatFrom)
// in #1988 so the build can be shared; package main keeps same-named
// forwarders. The directory walk itself is ScanDirTree (tree_scan.go).
//
// Functions:
//
//   BuildFlatConfig(...)     — scan → merged ThresholdConfig (parse, boundary
//                              rules, merge, ApplyProfiles, subtree defaults).
//   applyBoundaryRules(...)  — enforce "state_filters / defaults /
//                              optional_overrides only in a defaults
//                              carrier; profiles only in `_` files"
//                              convention (#1676).
//   mergePartialConfigs(...) — deep-merge per-file partials into a
//                              single ThresholdConfig (used by
//                              BuildFlatConfig + package main's
//                              IncrementalLoad full-rebuild branch).

import (
	"fmt"
	"log"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/internal/confdname"
)

// FlatBuildInput is what BuildFlatConfig reads besides the scan: the previous
// commit's flat cache (for the partial reuse described below) and the subtree
// defaults chain the hierarchy plane derived from the same scan.
type FlatBuildInput struct {
	Root           string                     // conf.d root as given (display paths, subtree root test)
	PriorHashes    map[string]string          // previous commit's per-file hashes (nil on a cold load)
	PriorConfigs   map[string]ThresholdConfig // previous commit's parsed partials (nil on a cold load)
	TenantDefaults map[string][]string        // tenantID → defaults chain (InheritanceGraph.TenantDefaults)
	ParsedDefaults map[string]map[string]any  // defaults file → parsed dict (ParseDefaultsFiles)
	Obs            ScanObserver               // parse-failure counter; may be nil
	Logger         *log.Logger                // must be non-nil
}

// FlatBuild is BuildFlatConfig's result: the merged config the collector
// serves, the per-file partials it was merged from (the next commit's
// PriorConfigs), and applySubtreeDefaults' two returns.
type FlatBuild struct {
	Config        ThresholdConfig
	FileConfigs   map[string]ThresholdConfig
	SubtreeFilled int
	Unreachable   map[string][]string
}

// BuildFlatConfig builds the merged ThresholdConfig from a scan: parse each
// file (or reuse its prior partial), apply the boundary rules, merge, expand
// profiles, then overlay each tenant's subtree defaults. It is package main's
// commitFlatFrom without the commit (#1988) — no manager state is read or
// written here, so the exporter and any other reader of a conf.d tree build
// the same config from the same scan.
//
// A file carries bytes only when its hash moved against the prior, so for
// a file without bytes the parsed partial from the previous commit is
// reused when its hash is unchanged — the walker's prior IS the tree of
// that previous commit (see package main's flatScanState.tree), which makes the
// reuse sound. A `_`-prefixed file that is unchanged but has no cached
// partial (it failed to parse last time, or it is a nested platform file
// this plane never caches) is re-read and re-judged, so its ERROR/WARN and
// its parse-failure count fire again exactly as on a cold load. A tenant
// file is never re-judged here: the walker parses it (#1957) and has already
// logged and counted a failure on this scan (TreeFile.ParseFailed).
func BuildFlatConfig(scan *TreeScan, in FlatBuildInput) (FlatBuild, error) {
	if len(scan.Files) == 0 {
		return FlatBuild{}, fmt.Errorf("no .yaml files found in %s", in.Root)
	}
	logger := in.Logger

	// The root carrier the chain selects (#1674). Computed before the loop:
	// an unselected root carrier is ignored on every plane, so it is not even
	// parsed here, let alone merged or cached.
	rootCarrier := rootCarrierKey(scan, logger)

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
			applyBoundaryRules(name, &partial, logger)
			fileConfigs[name] = partial
			continue
		}
		fullPath := filepath.Join(in.Root, name)
		data := f.Data
		if data == nil {
			if in.PriorHashes[name] == f.Hash {
				if partial, ok := in.PriorConfigs[name]; ok {
					fileConfigs[name] = partial
					continue
				}
			}
			// Unchanged-but-uncached, or a prior the walker did not have:
			// read from disk and take the ordinary path below.
			var rerr error
			data, rerr = os.ReadFile(fullPath)
			if rerr != nil {
				logger.Printf("WARN: skip unreadable file %s: %v", fullPath, rerr)
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
			if probe, ok := reportUnparseableNestedPlatformFile(fullPath, data, in.Obs, logger); ok {
				reportNestedPlatformTenants(fullPath, probe, logger)
			}
			continue
		}
		partial, ok := parsePartialConfig(name, fullPath, data, in.Obs, logger)
		if !ok {
			continue
		}
		applyBoundaryRules(name, &partial, logger)
		fileConfigs[name] = partial
	}

	// Merge all partials. Tenant existence is THIS scan's verdict
	// (declaredTenantIDs): a root platform file's `tenants:` entry for a
	// tenant no tenant file declares is dropped and named.
	exists := tenantExistenceFor(fileConfigs, scan)
	reportPlatformOrphans(fileConfigs, exists, logger)
	merged := mergePartialConfigs(fileConfigs, exists)
	merged.ApplyProfiles()

	n, unreachable := applySubtreeDefaults(&merged, in.Root, in.TenantDefaults, in.ParsedDefaults)
	return FlatBuild{Config: merged, FileConfigs: fileConfigs, SubtreeFilled: n, Unreachable: unreachable}, nil
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
//
// Returns the probe's decode and ok=true when the file is syntactically
// fine, so reportNestedPlatformTenants can inspect it WITHOUT a second
// parse (a separate decode there measured +10k allocs / +1.4 MiB per
// DiffAndReload_Hierarchical_1000 reload — every nested carrier parsed
// twice on every full load, #1982 bench gate).
func reportUnparseableNestedPlatformFile(fullPath string, data []byte, metrics ScanObserver, logger *log.Logger) (any, bool) {
	var probe any
	err := yaml.Unmarshal(data, &probe)
	if err == nil {
		// Syntactically fine; its content simply is not for this plane —
		// except a key that exists ONLY on this plane (#2028). Subtree
		// inheritance carries `defaults:` down the tree, but the per-tenant
		// cap is one global value read from the root carrier alone, so a
		// nested one would otherwise vanish without a word.
		if top, ok := probe.(map[string]any); ok {
			if _, set := top["max_metrics_per_tenant"]; set {
				logger.Printf("WARN: max_metrics_per_tenant found in %s — only the ROOT _defaults.yaml may set it (one global cap, not inherited per subtree), ignoring", fullPath)
			}
		}
		return probe, true
	}
	if metrics != nil {
		metrics.IncParseFailure(filepath.Base(fullPath))
	}
	logger.Printf("ERROR: skip unparseable defaults/profiles file %s: %v (entire block dropped — fix file or remove)", fullPath, err)
	return nil, false
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
func parsePartialConfig(name, path string, data []byte, metrics ScanObserver, logger *log.Logger) (ThresholdConfig, bool) {
	partial, err := ParseConfigFile(data)
	if err != nil {
		if metrics != nil {
			metrics.IncParseFailure(filepath.Base(path))
		}
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
// is decided by the callers (BuildFlatConfig skips an unselected root carrier
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
		if partial.MaxMetricsPerTenant != 0 {
			logger.Printf("WARN: max_metrics_per_tenant found in %s — not a defaults carrier (only the ROOT _defaults.yaml / _defaults.yml is), ignoring", name)
			partial.MaxMetricsPerTenant = 0
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
		// ⛔ SECURITY (#2028): the cap exists to bound what ONE tenant can make
		// the exporter emit. A tenant file that could set it would raise its
		// own ceiling — or disable it with a negative value — via a direct
		// GitOps push. Same boundary as Defaults above.
		if partial.MaxMetricsPerTenant != 0 {
			logger.Printf("WARN: max_metrics_per_tenant found in %s — platform-scoped, only the ROOT _defaults.yaml may set it, ignoring", name)
			partial.MaxMetricsPerTenant = 0
		}
	}
	if !isPlatformFile {
		if len(partial.Profiles) > 0 {
			logger.Printf("WARN: profiles found in %s — should only be in _profiles.yaml, ignoring", name)
			partial.Profiles = nil
		}
	}
}

// isPlatformKey reports whether a flat-cache key names a platform
// (`_`-prefixed) file. Only ROOT platform files ever reach the flat merge —
// nested ones are dropped before it (isNestedPlatformFile) — so inside
// `configs` this is "a root platform file".
func isPlatformKey(key string) bool { return strings.HasPrefix(scanKeyBase(key), "_") }

// sortFlatMergeOrder sorts flat-cache keys into the ONE merge order every
// flat path uses: platform files first, then tenant files, each group by
// filename.
//
// ⛔ THE GROUPING IS THE SEMANTICS, NOT A TIE-BREAK. A platform file's
// `tenants:` block is the platform's per-tenant DEFAULT; the tenant's own
// file wins key by key, whatever either file is called. A plain filename
// sort made that depend on ASCII: `tx.yaml` sorts after `_defaults.yaml` and
// won, while `TX.yaml` / `0tx.yaml` sort before it and LOST — the platform's
// 60 served over the tenant's 70 on /metrics, while /effective reported 70.
//
// Only Tenants are order-sensitive across the two groups: the boundary rules
// strip defaults / state_filters / optional_overrides / profiles from tenant
// files, so every other section is merged from platform files alone, in the
// same relative order as before.
//
// ⚠️ Classified ONCE, then each group sorted with sort.Strings. A comparator
// that called isPlatformKey per comparison measured 15-40% slower on
// BenchmarkMergePartialConfigs_1000 than main's plain sort (blind review);
// the stable partition keeps the cost at one pass plus the same sort.
func sortFlatMergeOrder(names []string) {
	n := 0
	for i, name := range names {
		if isPlatformKey(name) {
			names[n], names[i] = names[i], names[n]
			n++
		}
	}
	sort.Strings(names[:n])
	sort.Strings(names[n:])
}

// declaredTenantIDs is the set of tenants that EXIST: those some tenant
// (non-`_`) file declares, on this scan's verdict.
//
// ⛔ THE WALKER'S ANSWER, NOT A SECOND ONE. TreeFile.TenantIDs is filled by
// the walker's own decode (config.ParseConfigFile) for tenant files only —
// `_` files are never parsed for tenants and a file whose decode failed
// declares none — and carried across the mtime fast-path with the hash. So
// "does tx exist" here is the question /effective's Locate answers from the
// same walk. A platform file can supply defaults for a tenant in this set; it
// cannot add one to it.
func declaredTenantIDs(scan *TreeScan) map[string]struct{} {
	out := make(map[string]struct{}, len(scan.Files))
	for key, f := range scan.Files {
		if isPlatformKey(key) {
			continue // never parsed for tenants; stated, not assumed
		}
		for _, tid := range f.TenantIDs {
			out[tid] = struct{}{}
		}
	}
	return out
}

// tenantExistenceFor returns declaredTenantIDs(scan) when some root platform
// file in `configs` carries a `tenants:` block, and nil otherwise.
//
// ⛔ nil MEANS "NOTHING TO FILTER", and every consumer reads it that way
// (mergePartialConfigs, patchTenants, reportPlatformOrphans). With no
// platform `tenants:` entry in the merge, every tenant in it came from a
// tenant file and therefore exists — so the set would answer "yes" to every
// question asked of it. Not building it keeps the common tree (no platform
// per-tenant block at all) off a 1000-entry map per reload, which the bench
// gate charges to IncrementalLoad_1000_OneFileChanged.
func tenantExistenceFor(configs map[string]ThresholdConfig, scan *TreeScan) map[string]struct{} {
	for name, partial := range configs {
		if isPlatformKey(name) && len(partial.Tenants) > 0 {
			return declaredTenantIDs(scan)
		}
	}
	return nil
}

// reportPlatformOrphans WARNs once per (platform file, tenant) for every
// tenant a root platform file's `tenants:` block names that no tenant file
// declares. The merge drops those entries (mergePartialConfigs,
// patchTenants); this is the sentence that says so. Called on every commit
// that builds the flat config, never on a quiet tick, so it repeats once per
// load/reload like the other boundary WARNs.
func reportPlatformOrphans(configs map[string]ThresholdConfig, exists map[string]struct{}, logger *log.Logger) {
	if exists == nil {
		return // no platform `tenants:` entry at all (tenantExistenceFor)
	}
	if logger == nil {
		logger = log.Default()
	}
	names := make([]string, 0, len(configs))
	for name := range configs {
		if isPlatformKey(name) && len(configs[name].Tenants) > 0 {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	for _, name := range names {
		ids := make([]string, 0, len(configs[name].Tenants))
		for tid := range configs[name].Tenants {
			if _, ok := exists[tid]; !ok {
				ids = append(ids, tid)
			}
		}
		sort.Strings(ids)
		for _, tid := range ids {
			logger.Printf("WARN: tenants.%s in platform file %s ignored — no tenant file declares tenant %q; "+
				"a platform file can only provide defaults for a tenant that already exists", tid, name, tid)
		}
	}
}

// reportNestedPlatformTenants WARNs when a NESTED platform file carries a
// non-empty `tenants:` block. Its content never reaches any plane — the
// flat merge drops nested `_` files whole (see isNestedPlatformFile) and the
// inheritance chain reads only a carrier's `defaults:` — so without this the
// tenant values in it vanish with no log at all. Behaviour is unchanged: the
// block is still dropped; it is just no longer silent. A syntactically
// broken file is reportUnparseableNestedPlatformFile's to report; this
// takes THAT function's decode (`probe`) and never parses on its own.
func reportNestedPlatformTenants(name string, probe any, logger *log.Logger) {
	doc, ok := probe.(map[string]any)
	if !ok {
		return
	}
	var ids []string
	switch tenants := doc["tenants"].(type) {
	case map[string]any:
		for tid := range tenants {
			ids = append(ids, tid)
		}
	case map[any]any: // a non-string key somewhere in the mapping
		for tid := range tenants {
			ids = append(ids, fmt.Sprint(tid))
		}
	}
	if len(ids) == 0 {
		return
	}
	sort.Strings(ids)
	logger.Printf("WARN: tenants: block in nested platform file %s ignored (tenants %s) — "+
		"the tenants: block of a nested platform file is not read by any plane; "+
		"only a root platform file can provide per-tenant defaults", name, strings.Join(ids, ", "))
}

// mergePartialConfigs merges all cached partial configs via mergePartialInto,
// in sortFlatMergeOrder: platform files first, then tenant files.
// defaults/state_filters overwrite, tenants/profiles deep merge — so a
// tenant file's key beats the same key in a platform file's `tenants:`
// block regardless of either filename. A platform file's entry for a tenant
// not in `exists` (tenantExistenceFor; nil = nothing to filter) is dropped: a
// platform file cannot create a tenant (reportPlatformOrphans says so).
// An unselected root defaults carrier never reaches `configs` (#1674; see
// isUnselectedRootCarrier), so every file merged here is one the chain reads
// or a non-carrier the boundary rules have already judged.
func mergePartialConfigs(configs map[string]ThresholdConfig, exists map[string]struct{}) ThresholdConfig {
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

	names := make([]string, 0, len(configs))
	for name := range configs {
		names = append(names, name)
	}
	sortFlatMergeOrder(names)

	for _, name := range names {
		partial := configs[name]
		if exists != nil && isPlatformKey(name) && len(partial.Tenants) > 0 {
			// ⛔ On a COPY of the struct with a filtered map: the cached
			// partial in flat.configs keeps every entry, so a tenant file
			// added later picks the platform's values up without the
			// platform file being re-read.
			kept := make(map[string]map[string]ScheduledValue, len(partial.Tenants))
			for tid, ov := range partial.Tenants {
				if _, ok := exists[tid]; ok {
					kept[tid] = ov
				}
			}
			partial.Tenants = kept
		}
		mergePartialInto(&merged, partial)
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
// (BuildFlatConfig skips it), and any change to a root carrier sends the
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
// ⛔ CALLED ONLY WHERE A FULL LOAD BUILDS THE FLAT CONFIG (BuildFlatConfig),
// never on the quiet watch tick — detectChange does not merge — so a
// misconfigured tree logs once per load/reload, not every WatchInterval.
// The selection is the scan's own (TreeScan.DefaultsCarriers), the one that
// scan's inheritance graph is built from, so within one load /metrics' root
// Defaults and the chain's L0 are the same file. ⚠️ That is a statement
// about a single scan, not about merged_hash across reloads: a reload that
// deletes a chain file can leave the exporter's cached merged_hash stale
// (#1964, pre-existing, not addressed here).
func rootCarrierKey(scan *TreeScan, logger *log.Logger) string {
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
	// #2028: this field was never copied, so in directory mode (the mode the
	// Helm chart runs) the cap was always the built-in DefaultMaxMetricsPerTenant
	// whatever `_defaults.yaml` said. Order-independent by construction:
	// applyBoundaryRules zeroes it in every file except a defaults carrier, and
	// a nested carrier never reaches this merge — so only the root carrier
	// arrives here non-zero.
	if partial.MaxMetricsPerTenant != 0 {
		merged.MaxMetricsPerTenant = partial.MaxMetricsPerTenant
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

// Exported forms of the helpers above, for package main's forwarders
// (flat_scanner.go). Behavior pin: every one is `return x(args...)`.

func ScanKeyBase(key string) string        { return scanKeyBase(key) }
func IsNestedPlatformFile(key string) bool { return isNestedPlatformFile(key) }
func ReportUnparseableNestedPlatformFile(fullPath string, data []byte, metrics ScanObserver, logger *log.Logger) (any, bool) {
	return reportUnparseableNestedPlatformFile(fullPath, data, metrics, logger)
}
func ReportNestedPlatformTenants(name string, probe any, logger *log.Logger) {
	reportNestedPlatformTenants(name, probe, logger)
}
func IsPlatformKey(key string) bool     { return isPlatformKey(key) }
func SortFlatMergeOrder(names []string) { sortFlatMergeOrder(names) }
func TenantExistenceFor(configs map[string]ThresholdConfig, scan *TreeScan) map[string]struct{} {
	return tenantExistenceFor(configs, scan)
}
func ReportPlatformOrphans(configs map[string]ThresholdConfig, exists map[string]struct{}, logger *log.Logger) {
	reportPlatformOrphans(configs, exists, logger)
}
func ParsePartialConfig(name, path string, data []byte, metrics ScanObserver, logger *log.Logger) (ThresholdConfig, bool) {
	return parsePartialConfig(name, path, data, metrics, logger)
}
func ApplyBoundaryRules(name string, partial *ThresholdConfig, logger *log.Logger) {
	applyBoundaryRules(name, partial, logger)
}
func MergePartialConfigs(configs map[string]ThresholdConfig, exists map[string]struct{}) ThresholdConfig {
	return mergePartialConfigs(configs, exists)
}
func MergePartialInto(merged *ThresholdConfig, partial ThresholdConfig) {
	mergePartialInto(merged, partial)
}
func AnyRootCarrierKey(groups ...[]string) bool { return anyRootCarrierKey(groups...) }
