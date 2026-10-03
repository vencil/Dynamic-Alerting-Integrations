package config

import (
	"fmt"
	"log"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

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
// the tenant body, key by key, and finally the profile the tenant elects,
// filling in only the keys neither of those sets (#1385). ResolveAt on it
// serves what /metrics serves for the tenant (see mergeTenantConfig).
//
// ⛔ ValidateTenantKeys IS SHADOWED ON PURPOSE. TenantMerge.ValidateTenantKeys
// judges the tenant layer (root defaults + the body — exactly the merge the
// write gate always judged) for Errors, and reports problems in a platform
// file's entry, or in the part of an elected profile that reaches the
// tenant, as Notices naming the file. Calling the EMBEDDED method
// (m.ThresholdConfig.ValidateTenantKeys()) would judge the platform and
// profile keys as if the tenant had written them — a write refused for a
// key the tenant cannot fix in its own file. Every caller goes through the
// TenantMerge one.
//
// ⛔ SO ARE THE THRESHOLD RESOLVE ENTRY POINTS (#2397). TenantMerge.Resolve /
// ResolveAt / ResolveAtWithStats / ResolveAtWithKeys return exactly the embedded
// methods' rows and stats, but write none of the resolver's ERROR/WARN lines
// (cardinality truncation, unknown / invalid values, dangling `_critical`,
// bad dimensional keys, bad time windows, rejected custom alerts) to the
// process log: the merge runs per request, and a GET must not log the
// tenant's resolver findings on every call. The lines are dropped, not
// handed on: some of the same facts reach the caller as ValidateTenantKeys
// Errors / notices (a dangling `_critical`, for one), but not all (a
// non-numeric base value is not flagged there). /metrics resolves a
// ThresholdConfig, not a TenantMerge, and keeps logging every line.
//
// ⚠️ ONLY those four. The other promoted resolvers — ResolveStateFilters(At),
// ResolveSilentModes(At), OperationalStatesAt, ResolveMaintenanceExpiries(At),
// ResolveThresholdExpiries(At), ResolveSeverityDedup, ResolveMetadata,
// ResolveRouting, ApplyProfiles — are NOT shadowed and still write the
// exporter's WARNs when called on a TenantMerge. No tenant-api caller calls
// them on one today (GET uses ResolveAt and ValidateTenantKeys); a caller
// that starts to, per request, needs the same treatment first. (The list /
// search paths read a plain *ThresholdConfig, not a TenantMerge, through
// OperationalStatesAtLogf with a nil sink — #2467.)
type TenantMerge struct {
	ThresholdConfig

	// own is the tenant layer per tenant: the body's entry (or the flat-KV
	// fallback), read-only. The display maps are separate copies.
	own map[string]map[string]ScheduledValue
	// platform is, per tenant, the root platform files that supplied keys
	// the display merge carries (merge order), each with those keys.
	platform map[string][]platformSupply
	// profileFiles is, per profile name, per key as the file spells it, the
	// root platform file whose value ThresholdConfig.Profiles holds (the
	// last one in merge order that sets it) — the attribution of a profile
	// notice. profileFileOrder is those files in merge order.
	profileFiles     map[string]map[string]string
	profileFileOrder []string
}

// Resolve is ThresholdConfig.Resolve without the resolver's log lines — see
// TenantMerge. Shadowed too: the promoted one calls ResolveAt on the embedded
// *ThresholdConfig, which would bypass the shadow below.
func (m *TenantMerge) Resolve() []ResolvedThreshold {
	return m.ResolveAt(time.Now())
}

// ResolveAt is ThresholdConfig.ResolveAt without the resolver's log lines —
// see TenantMerge.
func (m *TenantMerge) ResolveAt(now time.Time) []ResolvedThreshold {
	rows, _ := m.ResolveAtWithStats(now)
	return rows
}

// ResolveAtWithStats is ThresholdConfig.ResolveAtWithStats without the
// resolver's log lines — see TenantMerge.
func (m *TenantMerge) ResolveAtWithStats(now time.Time) ([]ResolvedThreshold, ResolveStats) {
	return m.resolveAtWithStats(now, nil, nil)
}

// ResolveAtWithKeys is ThresholdConfig.ResolveAtWithKeys without the
// resolver's log lines — see TenantMerge.
func (m *TenantMerge) ResolveAtWithKeys(now time.Time) ([]KeyedThreshold, ResolveStats, error) {
	var keyed []KeyedThreshold
	rows, stats := m.resolveAtWithStats(now, &keyed, nil)
	if err := checkKeyed(rows, keyed); err != nil {
		return nil, stats, err
	}
	return keyed, stats, nil
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
//   - A tenant LEGACY key whose canonical spelling a platform file also
//     writes (tenant `mysql_cpu`, platform `mysql_threads_running`) is the
//     tenant's: supplyFor never supplies a threshold the body writes under
//     any spelling (#2368), so the usual rename notice ("the old name still
//     resolves") is true, and it is the one given.
//   - Notices: the tenant layer's Notices, then one notice per problem in a
//     platform file's entry — every message ValidateTenantKeys would give
//     had the tenant written those keys (unknown key, bad `expires:`,
//     dangling `_critical`, unknown `_profile`, bad version label, renamed
//     key), naming the file to fix — then the same for the keys the
//     elected profile fills in for the tenant (profileLayerNotices),
//     naming the profile and the file that defines each key. That part
//     never carries a renamed-key notice: ApplyProfiles fills from
//     canonicalView(profile), so a legacy spelling in a profile reaches
//     the tenant under its canonical key (ApplyProfiles' #1231 F4 rule —
//     the profile author's deprecation signal is deliberately not
//     surfaced to the tenant).
//   - The tenant's own `_profile` is judged against the profiles the root
//     platform files define (ThresholdConfig.Profiles, the set /metrics
//     expands from): a name no root platform file defines is still an
//     Error, as it always was; a defined one is not (#1385 — before
//     profiles were read here, every `_profile` was reported unknown).
func (m *TenantMerge) ValidateTenantKeys() KeyValidation {
	tenantLayer := ThresholdConfig{
		Defaults:          m.Defaults,
		StateFilters:      m.StateFilters,
		OptionalOverrides: m.OptionalOverrides,
		Profiles:          m.Profiles,
		Tenants:           m.own,
	}
	v := tenantLayer.validateTenantKeys()
	if len(m.platform) > 0 {
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
					Profiles:          m.Profiles,
					Tenants:           map[string]map[string]ScheduledValue{tid: sup.keys},
				}
				pv := layer.validateOverrideKeys()
				msgs := append(append([]string(nil), pv.Errors...), pv.Notices...)
				sort.Strings(msgs)
				for _, msg := range msgs {
					v.Notices = append(v.Notices, platformLayerNotice(sup.file, tid, msg))
				}
			}
		}
	}
	v.Notices = append(v.Notices, m.profileLayerNotices()...)
	return v
}

// profileLayerNotices is one notice per problem in the part of an elected
// profile that reaches a tenant — the keys ApplyProfiles fills in for it
// (profileFill over the tenant layer plus the platform layer, the map
// ApplyProfiles reads) — judged as validateOverrideKeys would judge them
// had the tenant written them, and attributed to the root platform file
// that defines each key. A key the profile supplies but may not fill in
// (declared without a platform value) gets a notice too: /metrics drops it
// with a WARN nobody authoring the tenant reads.
//
// Never an Error: the profile is platform-owned, the tenant cannot fix it
// in its own file, and a key the tenant writes itself is never filled (so
// never judged here). Keys the tenant or a platform entry sets are not the
// profile's and are not reported.
//
// ⚠️ It judges what validateOverrideKeys judges — key names, `expires:`,
// `_critical` bases, version labels — and nothing else. A value resolve
// cannot parse (`mysql_connections: abc`) is not reported, for the profile
// as for the tenant's own file: resolve falls back to the default with a
// log line on both.
func (m *TenantMerge) profileLayerNotices() []string {
	if len(m.Profiles) == 0 || len(m.own) == 0 {
		return nil
	}
	tenants := make([]string, 0, len(m.own))
	for tid := range m.own {
		tenants = append(tenants, tid)
	}
	sort.Strings(tenants)
	canonDeclared := canonicalizeOptionalOverrides(m.OptionalOverrides)
	var out []string
	for _, tid := range tenants {
		// The layer ApplyProfiles read: the tenant's own keys and what the
		// platform files supply (never a key the tenant writes).
		layer := make(map[string]ScheduledValue, len(m.own[tid]))
		for k, v := range m.own[tid] {
			layer[k] = v
		}
		for _, sup := range m.platform[tid] {
			for k, v := range sup.keys {
				layer[k] = v
			}
		}
		sv, ok := layer["_profile"]
		if !ok {
			continue
		}
		name := strings.TrimSpace(sv.Default)
		profile, found := m.Profiles[name]
		if name == "" || !found {
			continue // unknown: the tenant layer's Error or a platform notice says so
		}
		byFile := make(map[string]map[string]ScheduledValue)
		declaredBy := make(map[string][]string)
		fileOf := m.profileKeyFiles(name)
		fill := profileFill(canonicalView(profile), layer, canonDeclared, func(key string) {
			f := fileOf[key]
			declaredBy[f] = append(declaredBy[f], key)
		})
		for k, v := range fill {
			f := fileOf[k]
			if byFile[f] == nil {
				byFile[f] = make(map[string]ScheduledValue)
			}
			byFile[f][k] = v
		}
		for _, f := range m.profileFileOrder {
			var msgs []string
			if keys := byFile[f]; len(keys) > 0 {
				pl := ThresholdConfig{
					Defaults:          m.Defaults,
					OptionalOverrides: m.OptionalOverrides,
					Profiles:          m.Profiles,
					Tenants:           map[string]map[string]ScheduledValue{tid: keys},
				}
				pv := pl.validateOverrideKeys()
				msgs = append(append(msgs, pv.Errors...), pv.Notices...)
			}
			for _, k := range declaredBy[f] {
				msgs = append(msgs, fmt.Sprintf(
					"key %q is declared without a platform value (optional_overrides), which a profile cannot fill in — it is not applied", k))
			}
			sort.Strings(msgs)
			for _, msg := range msgs {
				out = append(out, profileLayerNotice(f, name, tid, msg))
			}
		}
	}
	return out
}

// profileKeyFiles maps each key of canonicalView(Profiles[name]) to the file
// its value came from: the canonical spelling's file when the profile
// writes it (canonicalView lets it win), else the legacy spelling's.
func (m *TenantMerge) profileKeyFiles(name string) map[string]string {
	raw := m.profileFiles[name]
	out := make(map[string]string, len(raw))
	for k, f := range raw {
		if canon, alias := canonicalKeyFor(k); alias {
			if _, set := out[canon]; !set {
				out[canon] = f
			}
			continue
		}
		out[k] = f
	}
	return out
}

// profileLayerNotice rewrites one validateOverrideKeys message about the
// part of profile `name` that file supplies to tenantID into a notice
// attributed to the file and the profile.
func profileLayerNotice(file, name, tenantID, msg string) string {
	body := strings.TrimPrefix(strings.TrimPrefix(msg, "WARN: "), "NOTICE: ")
	body = strings.TrimPrefix(body, "tenant="+tenantID+": ")
	return fmt.Sprintf("NOTICE: platform file %s, profile %q (elected by tenant %s via _profile): %s — fix it in that file; "+
		"it is not in this tenant's file and does not block writing it", file, name, tenantID, body)
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
//   - GET  /api/v1/tenants/{id}            (handler, via the split halves)
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
// It is LoadRootPlatform + MergeTenantOverRootPlatform. ⚠️ The read can
// block (see LoadRootPlatform); the tenant-api GET path calls the two halves
// itself so it can bound and share the read.
func MergeTenantWithRootDefaults(configDir, tenantID string, tenantData []byte) TenantMerge {
	return MergeTenantOverRootPlatform(LoadRootPlatform(configDir), tenantID, tenantData)
}

// RootPlatform is ONE read of a conf.d root's platform surface — the
// selected defaults carrier and every root platform file, each decoded (from
// the content-hash cache when its bytes are unchanged). It is independent of
// any tenant, which is why it is split out of the per-tenant merge: the
// tenant-api GET shares one in-flight read among concurrent requests
// (#2208, PR #2214 review) and then merges each tenant over it.
//
// ⛔ A SNAPSHOT, NOT A CACHE. It holds what the files said when
// LoadRootPlatform ran. Callers merge over it and drop it; keeping one
// across requests would serve stale platform values. Read-only and safe
// for concurrent MergeTenantOverRootPlatform calls. The zero value is an
// empty root: no carrier, no platform files.
type RootPlatform struct {
	r rootPlatform
}

// LoadRootPlatform reads configDir's root platform surface (one root-only
// walk, scanRootPlatform). A root that cannot be walked yields an empty
// surface — the historical "no platform defaults" answer.
//
// ⚠️ It reads files and takes no deadline: a read can block (a FIFO, a hung
// mount). The tenant-api GET path bounds it; the write paths walk the whole
// tree, bounded, before they get here.
func LoadRootPlatform(configDir string) RootPlatform {
	return RootPlatform{r: loadRootPlatform(configDir)}
}

// MergeTenantOverRootPlatform is MergeTenantWithRootDefaults over a root
// surface already read by LoadRootPlatform. It reads no file.
func MergeTenantOverRootPlatform(root RootPlatform, tenantID string, tenantData []byte) TenantMerge {
	// Decode the tenant body into the typed config. A decode error contributes
	// no overrides (the historical behavior: the merge loop was guarded by
	// `err == nil`); YAML validity is the caller's gate.
	//
	// ParseConfigFile, not a bare yaml.Unmarshal (#2518): a threshold the
	// body writes as null is no write, so the platform layer (supplyFor) and
	// the profile fill supply it — as on /metrics, which decodes the same way.
	tenantCfg, err := ParseConfigFile(tenantData)
	if err != nil {
		tenantCfg = ThresholdConfig{}
	}

	merged := mergeTenantConfig(root.r, tenantCfg)

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
			// ⛔ Two maps: ApplyProfiles below writes the profile's keys
			// into the display one, and own must stay what the tenant wrote.
			display := make(map[string]ScheduledValue, len(flatKV))
			for k, v := range flatKV {
				display[k] = v
			}
			merged.Tenants[tenantID] = display
			merged.own[tenantID] = flatKV
		}
	}

	// Silent: ApplyProfiles' WARNs would be written on every request; the
	// same facts reach the caller through ValidateTenantKeys.
	merged.applyProfiles(nil)
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
// and ApplyProfiles are otherwise identical, with ONE difference that comes
// from the caller's decode, not from this function: the byte entry point
// decodes through ParseConfigFile, which drops a threshold key the body writes
// as null (#2518), while the write gate (gitops.validate) hands in a plain
// yaml.Unmarshal of the body, where such a key is present with an empty value.
// So for a tenants-block body the two results are equal except that a body
// key written as null (not `_`-prefixed) is in this merge's tenant map and
// not in the byte entry point's — it then also keeps the platform entry's and
// the profile's value for that key out of this merge. The gate reads only
// ValidateTenantKeys from this result, which judges key names, so the
// difference only means a null key is still validated there (an unknown key
// written as null is still refused) — stricter than GET, never looser.
func MergeParsedTenantWithRootDefaults(configDir string, tenantCfg ThresholdConfig) TenantMerge {
	merged := mergeTenantConfig(loadRootPlatform(configDir), tenantCfg)
	merged.applyProfiles(nil) // silent, as in MergeTenantOverRootPlatform
	return merged
}

// mergeTenantConfig is the shared core behind both Merge*TenantWithRootDefaults
// entry points: it builds a fresh ThresholdConfig, overlays the root defaults
// carrier (Defaults + OptionalOverrides + StateFilters) from root, then
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
//     map overwrite. A reserved key the body writes, even as null, is the
//     body's; a threshold key written as null is not written at all (#2518,
//     MergeTenantOverRootPlatform decodes through ParseConfigFile).
//   - ⚠️ `_metadata` is NOT inherited — the walker plane's rule (/effective,
//     overlayTenant), deliberately NOT /metrics': the flat merge carries a
//     platform entry's `_metadata` into ResolveMetadata. GET serves no
//     metadata field, so no client sees the difference today; the pin is
//     TestMergeTenantPlatformLayerMatchesMetrics' `_metadata` tree.
//   - VALUES are the typed decode's ScheduledValue, the one /metrics resolves
//     — never a round trip through an untyped `any` (which would turn
//     `0x1F` into 31 while /metrics keeps the text).
//
// PROFILES (#1385) are /metrics', rule for rule — the entry points run
// the flat plane's own expansion (ApplyProfiles' core, applyProfiles,
// without its log lines: a GET must not write the exporter's WARNs to the
// process log per request; the caller gets them as an Error / notices from
// ValidateTenantKeys) on this merge:
//
//   - WHERE DEFINED: the `profiles:` block of every root platform file
//     above (so `_profiles.yaml`, the root carrier, any other root `_`
//     file); not a nested `_` file, not an unselected root carrier, not a
//     file the flat decode rejects, not the tenant body (applyBoundaryRules
//     strips a tenant file's). They come from the same read as the rest of
//     the root surface — no file is read for them.
//   - SEVERAL FILES: per profile name, per key, a later file (sort order)
//     over an earlier one — mergePartialInto's rule.
//   - WHICH NAME: the `_profile` in the tenant's layer after the platform
//     layer — the body's, else a platform entry's. An unknown or empty name
//     expands nothing.
//   - WHICH KEYS: profileFill — never a key the body or a platform entry
//     sets under any spelling, never one the root carrier declares in
//     `optional_overrides` (except `_critical`).
func mergeTenantConfig(root rootPlatform, tenantCfg ThresholdConfig) TenantMerge {
	merged := ThresholdConfig{
		Defaults:     make(map[string]float64),
		StateFilters: make(map[string]StateFilter),
		Tenants:      make(map[string]map[string]ScheduledValue),
		Profiles:     make(map[string]map[string]ScheduledValue),
	}
	out := TenantMerge{own: make(map[string]map[string]ScheduledValue, len(tenantCfg.Tenants))}

	// The root platform surface (read by the caller). A missing carrier is fine — the tenant
	// may legitimately rely on metric keys that simply have no default yet,
	// in which case ValidateTenantKeys still flags genuinely unknown keys.
	//
	// ⛔ #1674: the carrier is the one the exporter's chain reads at the root
	// (TreeScan.DefaultsCarriers), not a hard-coded `_defaults.yaml`. With that
	// join a root holding only `_defaults.yml` or `_DEFAULTS.YAML` — both served
	// by the exporter — read as NO platform surface, so the tenant-api write
	// gate refused valid keys as unknown and GET under-reported (blind review).
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
			// #2369: the per-tenant cap /metrics truncates at, so GET's
			// ResolveAt cuts where the exporter cuts. Only the ROOT carrier
			// (this one) may set it — the exporter's rule (#2028) and
			// RootMaxMetricsPerTenant's; the tenant body's value is never
			// read. READ-side only: nothing in ValidateTenantKeys consults
			// it, so the write gate is unchanged (pinned by
			// TestMergeTenantMaxMetricsDoesNotReachTheWriteGate).
			merged.MaxMetricsPerTenant = defaults.MaxMetricsPerTenant
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
	out.profileFiles, out.profileFileOrder = root.mergeProfilesInto(merged.Profiles)

	out.ThresholdConfig = merged
	return out
}

// rootPlatform is one read of configDir's root platform files: every file
// rootPlatformKeys selects, in merge order, with its (cached) decode.
//
// ⛔ Shared read-only by concurrent merges (RootPlatform): nothing may write
// to it, its files or their decodes after loadRootPlatform returns.
type rootPlatform struct {
	files []rootPlatformFile
	// carrierPos is 1 + the index into files of the selected root carrier;
	// 0 = none. Offset by one so the ZERO rootPlatform (and so the zero
	// RootPlatform) means "no carrier" instead of indexing an empty files.
	carrierPos int
}

type rootPlatformFile struct {
	key     string // root-relative scan key
	absPath string // for the server log only
	parsed  *parsedPlatformFile
}

func (r rootPlatform) carrier() *rootPlatformFile {
	if r.carrierPos <= 0 || r.carrierPos > len(r.files) {
		return nil
	}
	return &r.files[r.carrierPos-1]
}

// loadRootPlatform walks the root once (scanRootPlatform) and decodes each
// selected file through rootPlatformParses. A root that cannot be walked
// yields no files — the historical "no platform surface" answer.
func loadRootPlatform(configDir string) rootPlatform {
	var out rootPlatform
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
			out.carrierPos = len(out.files) + 1
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
		entry := pf.cfg.Tenants[tenant]
		for k := range entry {
			if k == "_metadata" {
				continue
			}
			// Per THRESHOLD, not per spelling (#2368): the body writing
			// `mysql_cpu` owns `mysql_threads_running` too, and a later
			// file's spelling drops an earlier file's other one — the
			// flat plane's overlayAcrossSpellings, applied to ownership.
			if hasAliasEquivalent(own, k) {
				continue
			}
			if owner == nil {
				owner = make(map[string]int)
			}
			var buf [2]string
			for _, s := range otherSpellings(k, &buf) {
				if _, same := entry[s]; !same {
					delete(owner, s)
				}
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

// mergeProfilesInto merges the root platform files' `profiles:` blocks into
// dst the way mergePartialInto does on /metrics — in merge order, per name,
// per key, the later file over the earlier; a null body is a known, empty
// profile (every platform file may carry profiles; applyBoundaryRules
// strips them only from tenant files). It returns, per name and key, the
// file whose value dst holds, and the files that define any profile.
//
// dst's inner maps are new: the decoded files are shared (the cache), and
// nothing may write to them.
func (r rootPlatform) mergeProfilesInto(dst map[string]map[string]ScheduledValue) (map[string]map[string]string, []string) {
	var files map[string]map[string]string
	var order []string
	for i := range r.files {
		pf := r.files[i].parsed
		if pf.err != nil || len(pf.cfg.Profiles) == 0 {
			continue
		}
		key := r.files[i].key
		order = append(order, key)
		if files == nil {
			files = make(map[string]map[string]string)
		}
		for name, values := range pf.cfg.Profiles {
			if dst[name] == nil {
				dst[name] = make(map[string]ScheduledValue, len(values))
			}
			if files[name] == nil {
				files[name] = make(map[string]string, len(values))
			}
			for k, v := range values {
				dst[name][k] = v
				files[name][k] = key
			}
		}
	}
	return files, order
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
