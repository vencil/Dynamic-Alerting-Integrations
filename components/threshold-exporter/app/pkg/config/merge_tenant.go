package config

import (
	"fmt"
	"log"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"gopkg.in/yaml.v3"
)

// CheckTenantRootKeys enforces the tenant-config.schema.json root contract
// (required:[tenants] + additionalProperties:false): a tenant config body may
// carry ONLY a top-level `tenants` block. It returns one warning per offending
// root key — `defaults`, `state_filters`, `profiles`, or any typo such as
// `tenant` / `tennants`. An empty result means the body is compliant.
//
// This is the single source of truth for the root-key rule, shared by the
// tenant-api PUT write boundary (gitops.validate, blocking) and the POST
// /validate dry-run so the two never disagree. It exists because the directory
// scanner silently strips/ignores a tenant file's stray `defaults:` block
// (no cross-tenant pollution, but the file is then dirty + WYSIWYG-not for
// operators, and a GET→edit→PUT round-trip keeps echoing the dirt). Enforcing
// at the write boundary keeps conf.d/{id}.yaml honest to its documented shape.
//
// YAML that does not parse as a mapping returns nil here — YAML validity is the
// caller's gate (so the error isn't double-reported); a scalar/sequence document
// simply yields no root-key warnings.
func CheckTenantRootKeys(yamlContent []byte) []string {
	var root map[string]any
	if err := yaml.Unmarshal(yamlContent, &root); err != nil {
		return nil
	}
	var bad []string
	for k := range root {
		if k != "tenants" {
			bad = append(bad, k)
		}
	}
	if len(bad) == 0 {
		return nil
	}
	sort.Strings(bad)
	return []string{fmt.Sprintf(
		"invalid root key(s) %v — a tenant config may only contain a top-level "+
			"'tenants' block (see docs/schemas/tenant-config.schema.json: "+
			"additionalProperties:false)", bad)}
}

// TenantMerge is the tenant-api merge core's result (#2208): the config GET
// resolves, plus what it takes to validate only what the TENANT wrote.
//
// The embedded ThresholdConfig is the display merge — the root defaults
// carrier's Defaults / OptionalOverrides / StateFilters, then per tenant the
// root platform files' `tenants:` entries (the layer /metrics applies), then
// the tenant body, key by key. ResolveAt on it serves what /metrics serves
// for the tenant, profiles excepted (see mergeTenantConfig).
//
// ⛔ ValidateTenantKeys IS SHADOWED ON PURPOSE. TenantMerge.ValidateTenantKeys
// judges the tenant layer (root defaults + the body — exactly the merge the
// write gate always judged) for Errors, and reports problems in a platform
// file's entry as Notices naming that file. Calling the EMBEDDED method
// (m.ThresholdConfig.ValidateTenantKeys()) would judge the platform keys as
// if the tenant had written them — a write refused for a key the tenant
// cannot fix in its own file. Every caller goes through the TenantMerge one.
type TenantMerge struct {
	ThresholdConfig

	// own is the tenant layer per tenant: the body's entry (or the flat-KV
	// fallback), read-only. The display maps are separate copies.
	own map[string]map[string]ScheduledValue
	// platform is, per tenant, the root platform files that supplied keys
	// the display merge carries (merge order), each with those keys.
	platform map[string][]platformSupply
	// profileRefs holds the names of the profiles the root platform files
	// define (nil values): the platform-side `_profile` check reads them.
	// Deliberately NOT in ThresholdConfig.Profiles — this core does not
	// expand profiles (#1385).
	profileRefs map[string]map[string]ScheduledValue
}

// platformSupply is one root platform file's contribution to one tenant: the
// keys whose value in the display merge came from it (the tenant body does
// not write them and no later platform file overwrote them).
type platformSupply struct {
	file string // root-relative scan key, e.g. "_platform.yaml" — never a server path
	keys map[string]ScheduledValue
}

// ValidateTenantKeys validates the tenant layer and reports the platform
// layer as advice — see TenantMerge.
//
//   - Errors: the tenant layer's Errors, and nothing else. A root platform
//     file's entry never blocks a write to the tenant and is never the
//     tenant's error.
//   - A tenant LEGACY key whose canonical spelling a platform file supplies
//     (tenant `mysql_cpu`, platform `mysql_threads_running`): resolve's
//     canonical-wins dedup serves the platform's value, on /metrics and
//     here alike, so the tenant's notice says the key is not applied and
//     names the file, instead of "the old name still resolves".
//   - Notices: the tenant layer's Notices, then one notice per problem in a
//     platform file's entry — every message ValidateTenantKeys would give
//     had the tenant written those keys (unknown key, bad `expires:`,
//     dangling `_critical`, unknown `_profile`, bad version label, renamed
//     key), naming the file to fix.
func (m *TenantMerge) ValidateTenantKeys() KeyValidation {
	tenantLayer := ThresholdConfig{
		Defaults:          m.Defaults,
		StateFilters:      m.StateFilters,
		OptionalOverrides: m.OptionalOverrides,
		Profiles:          m.Profiles,
		Tenants:           m.own,
	}
	v := tenantLayer.validateTenantKeys(m.shadowingPlatformFile)
	if len(m.platform) == 0 {
		return v
	}
	tenants := make([]string, 0, len(m.platform))
	for tid := range m.platform {
		tenants = append(tenants, tid)
	}
	sort.Strings(tenants)
	for _, tid := range tenants {
		for _, sup := range m.platform[tid] {
			layer := ThresholdConfig{
				Defaults:          m.Defaults,
				OptionalOverrides: m.OptionalOverrides,
				Profiles:          m.profileRefs,
				Tenants:           map[string]map[string]ScheduledValue{tid: sup.keys},
			}
			pv := layer.validateOverrideKeys(nil)
			msgs := append(append([]string(nil), pv.Errors...), pv.Notices...)
			sort.Strings(msgs)
			for _, msg := range msgs {
				v.Notices = append(v.Notices, platformLayerNotice(sup.file, tid, msg))
			}
		}
	}
	return v
}

// shadowingPlatformFile is the tenant layer's aliasShadow: the platform file
// that supplies canonKey to tenant, if any. A supplied key is by
// construction one the tenant's own map does not write, and the display
// merge carries it beside the tenant's legacy spelling — so resolve serves
// the platform's value (as /metrics does) and ignores the tenant's.
func (m *TenantMerge) shadowingPlatformFile(tenant, _, canonKey string) (string, bool) {
	for _, sup := range m.platform[tenant] {
		if _, ok := sup.keys[canonKey]; ok {
			return "platform file " + sup.file, true
		}
	}
	return "", false
}

// platformLayerNotice rewrites one validateOverrideKeys message about a
// platform file's entry for tenantID into a notice attributed to the file.
// The file is its root-relative name; the server's path never reaches a
// client.
func platformLayerNotice(file, tenantID, msg string) string {
	body := strings.TrimPrefix(strings.TrimPrefix(msg, "WARN: "), "NOTICE: ")
	body = strings.TrimPrefix(body, "tenant="+tenantID+": ")
	return fmt.Sprintf("NOTICE: platform file %s, entry tenants.%s: %s — fix it in that file; "+
		"it is not in this tenant's file and does not block writing it", file, tenantID, body)
}

// MergeTenantWithRootDefaults merges a tenant YAML document over the ROOT
// platform surface of configDir: the root defaults carrier the exporter's
// chain selects (any casing, `.yaml` over `.yml`; #1674) and the per-tenant
// `tenants:` entries of the root platform files (#2208). It populates
// Defaults + StateFilters + OptionalOverrides from the carrier so callers can
// run ValidateTenantKeys against a *tenant-only* body (the real
// conf.d/{id}.yaml shape — see db-a.yaml "Only 'tenants' block") and have its
// metric keys resolve against the inherited platform defaults.
//
// This is the single source of truth for the lightweight, root-only merge used
// across the tenant-api boundary:
//   - GET  /api/v1/tenants/{id}            (handler.loadMergedConfig, raw bytes)
//   - POST /api/v1/tenants/{id}/validate   (dry-run validation, via the write
//     gate's validate)
//   - PUT  /api/v1/tenants/{id}            (gitops write-boundary validation,
//     via the MergeParsedTenantWithRootDefaults sibling — same merge core, a
//     pre-decoded body to avoid a redundant Unmarshal, #708)
//
// Consolidating these call sites on one merge core is deliberate: a previous
// copy in the write path did NOT merge defaults, so a tenant-only body validated
// clean on GET//validate but was rejected at write time — the asymmetry tracked
// by ADR-024 PR4 / #704.
//
// It is intentionally NOT the full L0..Ln cascade that ResolveEffective walks
// (that one is parity-pinned to describe_tenant.py for the /effective
// endpoint). For the flat conf.d layout the two coincide; nested-directory
// _defaults.yaml cascades are out of scope here, matching the historical
// loadMergedConfig behavior this consolidates.
//
// ⚠️ It reads every root `_` file, and a read can block (a FIFO, a hung
// mount): it takes no deadline. The tenant-api GET path bounds it
// (handler.loadMergedConfig); the write paths walk the whole tree, bounded,
// before they get here.
func MergeTenantWithRootDefaults(configDir, tenantID string, tenantData []byte) TenantMerge {
	// Decode the tenant body into the typed config. A decode error contributes
	// no overrides (the historical behavior: the merge loop was guarded by
	// `err == nil`); YAML validity is the caller's gate.
	var tenantCfg ThresholdConfig
	if err := yaml.Unmarshal(tenantData, &tenantCfg); err != nil {
		tenantCfg = ThresholdConfig{}
	}

	merged := mergeTenantConfig(configDir, tenantCfg)

	// Fallback: a flat key-value document (no `tenants:` wrapper) is wrapped
	// under tenantID. Preserves the historical loadMergedConfig behavior. This
	// raw-bytes re-decode lives only on the byte entry point — the parsed
	// variant's callers (the write boundary) have already asserted a
	// `tenants.<id>` block, so the fallback is unreachable for them.
	//
	// No platform layer applies to it: the exporter declares no tenant from
	// such a file, so /metrics has nothing to overlay either.
	if _, exists := merged.Tenants[tenantID]; !exists {
		var flatKV map[string]ScheduledValue
		if err := yaml.Unmarshal(tenantData, &flatKV); err == nil && len(flatKV) > 0 {
			merged.Tenants[tenantID] = flatKV
			merged.own[tenantID] = flatKV
		}
	}

	merged.ApplyProfiles()
	return merged
}

// MergeParsedTenantWithRootDefaults is the parse-once variant of
// MergeTenantWithRootDefaults for callers that have ALREADY decoded the tenant
// body into a ThresholdConfig. It overlays that parsed config on the root
// platform surface of configDir without re-Unmarshalling the same bytes.
//
// Motivation (#708): the tenant-api write-path validation (gitops.validate)
// decoded the incoming YAML three times — once for the structural tenant check,
// once for the root-key contract, and a third time inside this merge. validate
// now decodes the typed body once and threads it here, dropping that redundant
// third decode. The root-key contract (CheckTenantRootKeys) still decodes a
// separate map[string]any because a typed ThresholdConfig cannot surface stray
// top-level keys — that decode targets a genuinely different shape, not the same
// one twice.
//
// It deliberately omits the byte variant's flat-KV fallback (that path serves
// the GET read path's legacy flat on-disk files; a parsed caller has already
// asserted a `tenants.<id>` block is present). The root merge, tenant merge,
// and ApplyProfiles are otherwise identical, so for a tenants-block body this
// returns the same result as the byte entry point.
func MergeParsedTenantWithRootDefaults(configDir string, tenantCfg ThresholdConfig) TenantMerge {
	merged := mergeTenantConfig(configDir, tenantCfg)
	merged.ApplyProfiles()
	return merged
}

// mergeTenantConfig is the shared core behind both Merge*TenantWithRootDefaults
// entry points: it builds a fresh ThresholdConfig, overlays the root defaults
// carrier (Defaults + OptionalOverrides + StateFilters) from configDir, then
// per tenant the root platform files' `tenants:` entries, then the
// already-decoded tenantCfg's `tenants:` block. It does NOT run the flat-KV
// fallback or ApplyProfiles — the entry points layer those on so each
// preserves its exact step ordering.
//
// The platform layer is the flat plane's (/metrics), rule for rule, with
// ONE deliberate exception (`_metadata`, below):
//
//   - WHICH FILES: rootPlatformKeys over ONE root-only walk (scanRootPlatform)
//     — `_`-prefixed, at the root, not an unselected root carrier; hidden
//     files and non-YAML extensions are the walker's; a file the flat decode
//     (ParseConfigFile) rejects contributes nothing.
//   - WHICH TENANTS: only those the body declares. A platform file cannot
//     create a tenant here, as it cannot on /metrics.
//   - PRECEDENCE: per top-level key, a later platform file (sort order) over
//     an earlier one, the tenant body over all of them — mergePartialInto's
//     map overwrite. A key the body writes, even as null, is the body's.
//   - ⚠️ `_metadata` is NOT inherited — the walker plane's rule (/effective,
//     overlayTenant), deliberately NOT /metrics': the flat merge carries a
//     platform entry's `_metadata` into ResolveMetadata. GET serves no
//     metadata field, so no client sees the difference today; the pin is
//     TestMergeTenantPlatformLayerMatchesMetrics' `_metadata` tree.
//   - VALUES are the typed decode's ScheduledValue, the one /metrics resolves
//     — never a round trip through an untyped `any` (which would turn
//     `0x1F` into 31 while /metrics keeps the text).
//
// ⛔ PROFILES ARE NOT EXPANDED. The display merge carries no Profiles, so a
// `_profile` — the tenant's own or one a platform entry elects — changes
// nothing here, exactly as before this layer was read. /metrics does expand
// them; closing that is #1385's step.
func mergeTenantConfig(configDir string, tenantCfg ThresholdConfig) TenantMerge {
	merged := ThresholdConfig{
		Defaults:     make(map[string]float64),
		StateFilters: make(map[string]StateFilter),
		Tenants:      make(map[string]map[string]ScheduledValue),
		Profiles:     make(map[string]map[string]ScheduledValue),
	}
	out := TenantMerge{own: make(map[string]map[string]ScheduledValue, len(tenantCfg.Tenants))}

	// Load the root platform surface. A missing carrier is fine — the tenant
	// may legitimately rely on metric keys that simply have no default yet,
	// in which case ValidateTenantKeys still flags genuinely unknown keys.
	//
	// ⛔ #1674: the carrier is the one the exporter's chain reads at the root
	// (TreeScan.DefaultsCarriers), not a hard-coded `_defaults.yaml`. With that
	// join a root holding only `_defaults.yml` or `_DEFAULTS.YAML` — both served
	// by the exporter — read as NO platform surface, so the tenant-api write
	// gate refused valid keys as unknown and GET under-reported (blind review).
	root := loadRootPlatform(configDir)
	if c := root.carrier(); c != nil {
		if c.parsed.err != nil {
			// A file that EXISTS but cannot be decoded is not the benign case
			// the missing-file comment above describes. This function is the
			// whole of what the tenant-api read and write paths know about the
			// platform surface, so a decode failure here hands every caller an
			// EMPTY one: the effective-config GET under-reports, and
			// ValidateTenantKeys then judges legitimate tenant keys "unknown"
			// and refuses the write — through the only supported writer.
			//
			// Note the asymmetry this closes. The exporter's own loader has
			// been loud about exactly this since the cycle-6 RCA
			// (parsePartialConfig logs ERROR and increments parse_failure for
			// `_`-prefixed files); this path had no signal at all. The decode
			// result is still discarded — unchanged behaviour — it just no
			// longer happens without a word.
			log.Printf("ERROR: %s exists but failed to parse, ALL platform defaults "+
				"are being ignored: %v", c.absPath, c.parsed.err)
		} else {
			defaults := c.parsed.cfg
			for k, v := range defaults.Defaults {
				merged.Defaults[k] = v
			}
			// Carried for the same reason as Defaults: this function is the
			// whole of what the tenant-api write path knows about the platform
			// surface. A key recognised by the platform but missing here would
			// still be refused at write time, so the runtime slot would exist
			// while remaining unusable through the only supported writer.
			merged.OptionalOverrides = append(merged.OptionalOverrides, defaults.OptionalOverrides...)
			for k, v := range defaults.StateFilters {
				merged.StateFilters[k] = v
			}
		}
	}

	// Merge the tenant config's `tenants:` block on top.
	for tenant, overrides := range tenantCfg.Tenants {
		// Pre-size to the known override count: a tenant can carry many
		// metric thresholds, so sizing the destination lets the copy below
		// fill without incremental map growth/rehashing (#708 review nit).
		dst := make(map[string]ScheduledValue, len(overrides))
		for k, v := range overrides {
			dst[k] = v
		}
		merged.Tenants[tenant] = dst
		out.own[tenant] = overrides
	}

	// The platform layer, under the keys the body does not write.
	for tenant, overrides := range tenantCfg.Tenants {
		supplies := root.supplyFor(tenant, overrides)
		for _, sup := range supplies {
			for k, v := range sup.keys {
				merged.Tenants[tenant][k] = v
			}
		}
		if len(supplies) > 0 {
			if out.platform == nil {
				out.platform = make(map[string][]platformSupply)
			}
			out.platform[tenant] = supplies
		}
	}
	out.profileRefs = root.profileRefs()

	out.ThresholdConfig = merged
	return out
}

// rootPlatform is one read of configDir's root platform files: every file
// rootPlatformKeys selects, in merge order, with its (cached) decode.
type rootPlatform struct {
	files      []rootPlatformFile
	carrierIdx int // index into files of the selected root carrier; -1 = none
}

type rootPlatformFile struct {
	key     string // root-relative scan key
	absPath string // for the server log only
	parsed  *parsedPlatformFile
}

func (r rootPlatform) carrier() *rootPlatformFile {
	if r.carrierIdx < 0 {
		return nil
	}
	return &r.files[r.carrierIdx]
}

// loadRootPlatform walks the root once (scanRootPlatform) and decodes each
// selected file through rootPlatformParses. A root that cannot be walked
// yields no files — the historical "no platform surface" answer.
func loadRootPlatform(configDir string) rootPlatform {
	out := rootPlatform{carrierIdx: -1}
	scan, err := scanRootPlatform(configDir)
	if err != nil {
		return out
	}
	carrierKey := selectedRootCarrierKey(scan)
	for _, k := range rootPlatformKeys(scan) {
		f := scan.Files[k]
		if f == nil || f.Data == nil {
			// A cold walk (no prior) keeps the bytes of every file it
			// kept; an entry without them was not read.
			continue
		}
		if k == carrierKey {
			out.carrierIdx = len(out.files)
		}
		out.files = append(out.files, rootPlatformFile{
			key:     k,
			absPath: f.AbsPath,
			parsed:  rootPlatformParses.get(f.Hash, f.Data),
		})
	}
	return out
}

// supplyFor returns, in merge order, what each platform file supplies to
// tenant: its entry's keys that the body (own) does not write, that no
// later file overwrites, and that are not `_metadata`.
func (r rootPlatform) supplyFor(tenant string, own map[string]ScheduledValue) []platformSupply {
	var owner map[string]int
	for i := range r.files {
		pf := r.files[i].parsed
		if pf.err != nil {
			continue
		}
		for k := range pf.cfg.Tenants[tenant] {
			if k == "_metadata" {
				continue
			}
			if _, written := own[k]; written {
				continue
			}
			if owner == nil {
				owner = make(map[string]int)
			}
			owner[k] = i
		}
	}
	if len(owner) == 0 {
		return nil
	}
	byFile := make([]map[string]ScheduledValue, len(r.files))
	for k, i := range owner {
		if byFile[i] == nil {
			byFile[i] = make(map[string]ScheduledValue)
		}
		byFile[i][k] = r.files[i].parsed.cfg.Tenants[tenant][k]
	}
	var out []platformSupply
	for i, keys := range byFile {
		if len(keys) > 0 {
			out = append(out, platformSupply{file: r.files[i].key, keys: keys})
		}
	}
	return out
}

// profileRefs is the set of profile names the root platform files define —
// the set /metrics expands a `_profile` from (every platform file may carry
// profiles; applyBoundaryRules strips them only from tenant files).
func (r rootPlatform) profileRefs() map[string]map[string]ScheduledValue {
	var out map[string]map[string]ScheduledValue
	for i := range r.files {
		pf := r.files[i].parsed
		if pf.err != nil {
			continue
		}
		for name := range pf.cfg.Profiles {
			if out == nil {
				out = make(map[string]map[string]ScheduledValue)
			}
			out[name] = nil
		}
	}
	return out
}

// parsedPlatformFile is ParseConfigFile's verdict on one root platform
// file's bytes. ⛔ SHARED AND READ-ONLY: the cache hands the same value to
// concurrent GETs and writes; the merge copies what it takes into maps of
// its own.
type parsedPlatformFile struct {
	cfg ThresholdConfig
	err error
}

// rootPlatformParseCacheSize bounds the cache. A deployment has a handful of
// root platform files; the bound only keeps a long-lived process that sees
// many edits from keeping every past version decoded.
const rootPlatformParseCacheSize = 32

// rootPlatformParses caches decodes by content hash (TreeFile.Hash): a file
// whose bytes did not change is not decoded again — the reuse
// LoadRootPlatformTenants gets from its `prior`, without a prior to thread
// through the tenant-api's stateless calls. GET runs concurrently, and the
// gitops writer validates inside its single-writer token, so a large
// platform file (thousands of `tenants:` entries) decoded on every request
// would be paid on every read and inside the write lock.
var rootPlatformParses = &platformParseCache{max: rootPlatformParseCacheSize}

type platformParseCache struct {
	mu      sync.Mutex
	max     int
	entries map[string]*parsedPlatformFile
	order   []string // insertion order, for eviction
}

// get returns the decode of data, whose SHA-256 hex is hash. The decode runs
// outside the lock, so one large decode does not serialise every other
// caller; two callers racing on the same new hash may both decode it, and
// the first stored result wins (the two are equal — the decode is a pure
// function of the bytes).
func (c *platformParseCache) get(hash string, data []byte) *parsedPlatformFile {
	c.mu.Lock()
	if p, ok := c.entries[hash]; ok {
		c.mu.Unlock()
		return p
	}
	c.mu.Unlock()

	cfg, err := ParseConfigFile(data)
	p := &parsedPlatformFile{cfg: cfg, err: err}

	c.mu.Lock()
	defer c.mu.Unlock()
	if existing, ok := c.entries[hash]; ok {
		return existing
	}
	if c.entries == nil {
		c.entries = make(map[string]*parsedPlatformFile)
	}
	c.entries[hash] = p
	c.order = append(c.order, hash)
	for len(c.order) > c.max {
		delete(c.entries, c.order[0])
		c.order = c.order[1:]
	}
	return p
}

// reset empties the cache (benchmarks measuring the uncached cost).
func (c *platformParseCache) reset() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.entries = nil
	c.order = nil
}

// rootDefaultsCarrier returns the path and bytes of the ROOT defaults carrier
// the exporter's chain selects in configDir (TreeScan.DefaultsCarriers), and
// false when the root has none or the directory cannot be walked.
//
// It runs the walker's root-only, carriers-only mode (scanRootDefaults), not a
// full ScanDirTree: a full walk per call parsed every tenant file's
// declarations to answer a root-only question (~100x main's cost on a
// 1000-file tree). The mode shares the full walk's listing, readability and
// classification code, so the carrier chosen here is the one the exporter's
// chain reads. Its caller is RootMaxMetricsPerTenant; the tenant-api merge
// core reads the carrier through loadRootPlatform instead (#2208), in the
// same single walk as the other root platform files.
func rootDefaultsCarrier(configDir string) (path string, data []byte, ok bool) {
	scan, err := scanRootDefaults(configDir)
	if err != nil {
		return "", nil, false
	}
	p, found := scan.DefaultsCarriers().ByDir[scan.AbsRoot]
	if !found {
		return "", nil, false
	}
	rel, rerr := filepath.Rel(scan.AbsRoot, p)
	if rerr != nil {
		return "", nil, false
	}
	f := scan.Files[filepath.ToSlash(rel)]
	if f == nil || f.Data == nil {
		return "", nil, false
	}
	return p, f.Data, true
}
