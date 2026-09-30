package config

// Walker-plane half of profile expansion (#2117).
//
// A tenant's `_profile: <name>` elects a platform profile: every key the
// profile sets and the tenant's own layer does not is filled in. The flat
// plane (/metrics) has always done this (ThresholdConfig.ApplyProfiles, run
// by BuildFlatConfig after the multi-file merge). The walker plane —
// ResolveEffective (/effective), ScopeEffective (da-guard) — did not, so for
// a tenant on a profile /effective reported the defaults chain's value while
// /metrics served the profile's, and da-guard told the tenant an override
// was redundant when removing it changed what /metrics serves.
//
// Semantics (the oracle is /metrics; pinned by
// tests/shared/platform_tenant_overlay_matrix.json):
//
//   - WHICH FILES carry `profiles:`: the root platform files
//     (rootPlatformKeys — `_`-prefixed, at the root, not an unselected root
//     carrier) whose bytes the flat plane's decode (ParseConfigFile)
//     accepts. A nested `_profiles.yaml` / `_defaults.yaml`'s `profiles:`
//     is read by no plane; a tenant file's is stripped (applyBoundaryRules).
//     Across files a profile merges per name, per key, a later file (sort
//     order) over an earlier one — mergePartialInto's rule.
//   - WHICH NAME: the `_profile` the tenant's own layer ends up with — the
//     tenant file's, else the root platform files' `tenants:` entry's
//     (overlayTenant), exactly the merged `Tenants` map ApplyProfiles reads.
//     An unknown name, or an empty one, expands nothing.
//   - WHICH KEYS: profileFill, the one decision ApplyProfiles also makes —
//     canonical spelling (canonicalView), not a key the tenant's layer
//     already sets under any spelling (hasAliasEquivalent), not a key the
//     root carrier declares in `optional_overrides` (except `_critical`).
//   - PRECEDENCE: tenant file > root platform `tenants:` entry > profile >
//     defaults chain (root and subtree alike: applySubtreeDefaults does not
//     touch a key the profile filled). A filled key is merged over the chain
//     as if the tenant file had written it.

import (
	"path"
	"reflect"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/internal/confdname"
)

// ProfileOverlaySource is one entry of EffectiveConfig.ProfileOverlay: the
// profile the tenant is on, one file that profile's filled-in values came
// from, and the top-level keys whose value in the effective config that
// file's part of the profile supplied. Same shape as PlatformOverlaySource
// plus the profile name; field names match describe_tenant.py.
type ProfileOverlaySource struct {
	Profile string   `json:"profile"`
	File    string   `json:"file"`
	Keys    []string `json:"keys"`
}

// profileEntry is one profile key's value and the file that supplied it
// (the last one in merge order that set it).
type profileEntry struct {
	file  string
	value any
}

// PlatformProfiles is the `profiles:` set of a tree's root platform files,
// merged the flat plane's way, plus the root carrier's declared-without-
// value keys (which a profile may not fill in). Built once per resolver and
// shared read-only by every tenant; the zero value (or nil) has no profiles.
type PlatformProfiles struct {
	byName   map[string]map[string]profileEntry
	files    []string            // merge order, for the attribution order
	abs      map[string]string   // file (scan key) → TreeFile.AbsPath, the reload's scope attribution
	declared map[string]struct{} // canonicalizeOptionalOverrides of the root carrier
}

// profileSourceFile is one root platform file's bytes for newPlatformProfiles.
type profileSourceFile struct {
	key  string // scan key (root-relative slash path) — what profile_overlay reports
	abs  string // TreeFile.AbsPath ("" for /simulate's synthetic file)
	data []byte
}

// newPlatformProfiles decodes the `profiles:` block (and, from a defaults
// carrier, `optional_overrides:`) of each file, in the given merge order. A
// file the flat plane's decode rejects contributes nothing — it is dropped
// whole on /metrics too.
//
// ⚠️ The typed decode runs only for a file an untyped one found something
// in, so a tree without profiles pays one untyped decode per root platform
// file (as parsePlatformTenants does).
func newPlatformProfiles(files []profileSourceFile) *PlatformProfiles {
	pp := &PlatformProfiles{}
	for _, f := range files {
		var doc struct {
			Profiles          map[string]any `yaml:"profiles"`
			OptionalOverrides []any          `yaml:"optional_overrides"`
		}
		isCarrier := confdname.IsDefaults(path.Base(f.key))
		if yaml.Unmarshal(f.data, &doc) != nil {
			continue
		}
		if len(doc.Profiles) == 0 && (!isCarrier || len(doc.OptionalOverrides) == 0) {
			continue
		}
		typed, err := ParseConfigFile(f.data)
		if err != nil {
			continue
		}
		if isCarrier && len(typed.OptionalOverrides) > 0 {
			for k := range canonicalizeOptionalOverrides(typed.OptionalOverrides) {
				if pp.declared == nil {
					pp.declared = make(map[string]struct{})
				}
				pp.declared[k] = struct{}{}
			}
		}
		if len(doc.Profiles) == 0 {
			continue
		}
		pp.files = append(pp.files, f.key)
		if f.abs != "" {
			if pp.abs == nil {
				pp.abs = make(map[string]string)
			}
			pp.abs[f.key] = f.abs
		}
		for name, body := range doc.Profiles {
			if pp.byName == nil {
				pp.byName = make(map[string]map[string]profileEntry)
			}
			if pp.byName[name] == nil {
				pp.byName[name] = make(map[string]profileEntry)
			}
			// A null body is a known, empty profile (ParseConfigFile
			// accepted the file, so any other body is a mapping).
			m, _ := normalizeYAMLToJSON(body).(map[string]any)
			for k, v := range m {
				pp.byName[name][k] = profileEntry{file: f.key, value: v}
			}
		}
	}
	return pp
}

// LoadRootPlatformProfiles builds the PlatformProfiles of `scan` from its
// root platform files (rootPlatformKeys, merge order). `bytesOf` supplies a
// file's bytes; a file it cannot supply contributes nothing (the walker
// already logged an unreadable file).
func LoadRootPlatformProfiles(scan *TreeScan, bytesOf func(*TreeFile) ([]byte, error)) *PlatformProfiles {
	var files []profileSourceFile
	for _, k := range rootPlatformKeys(scan) {
		data, err := bytesOf(scan.Files[k])
		if err != nil {
			continue
		}
		files = append(files, profileSourceFile{key: k, abs: scan.Files[k].AbsPath, data: data})
	}
	return newPlatformProfiles(files)
}

// profileNameOf is the profile a tenant layer's `_profile` value elects:
// the string, trimmed (ApplyProfiles trims ScheduledValue.Default). ""
// (no profile) for anything else.
//
// A scalar `_profile` reaches here as its TEXT, not its decoded value
// (#2433): decodeTenantFile and parsePlatformTenants re-read it with
// withProfileText, as /metrics reads it (ScheduledValue keeps a scalar's
// text) and as describe_tenant.py does since #2408. So bare `010` elects
// profile "010" (the generic decode gave int 8, which elected nothing),
// and `!!binary MDEw` elects "MDEw" (the generic decode gave "010"). A
// mapping with `default:` reaches here as its default's text too; the
// merge-key mapping and the shapes left as values (a sequence, a mapping
// without `default:`) are covered in withProfileText.
func profileNameOf(v any) string {
	if s, ok := v.(string); ok {
		return strings.TrimSpace(s)
	}
	return ""
}

// profileTexts is each tenant's `_profile` in one document's `tenants:`
// block as the flat plane reads it — the typed decode /metrics runs
// (ScheduledValue keeps a scalar's text; merge keys and aliases resolve as
// they do there) — keyed by tenant id text. decode is that document's
// yaml decode. nil when it does not decode: the flat plane rejects such a
// file too, and the generic values are left as they were.
func profileTexts(decode func(any) error) map[string]string {
	var doc struct {
		Tenants map[string]struct {
			Profile *ScheduledValue `yaml:"_profile"`
		} `yaml:"tenants"`
	}
	if decode(&doc) != nil {
		return nil
	}
	var out map[string]string
	for tid, b := range doc.Tenants {
		if b.Profile == nil {
			continue
		}
		if out == nil {
			out = make(map[string]string)
		}
		out[tid] = b.Profile.Default
	}
	return out
}

// tenantsWriteProfile reports whether any tenant block in a generically
// decoded `tenants:` mapping writes `_profile` — the only case profileTexts'
// second decode can change anything, so a file without one skips it.
func tenantsWriteProfile(block map[string]any) bool {
	for _, body := range block {
		if b, ok := body.(map[string]any); ok {
			if _, has := b["_profile"]; has {
				return true
			}
		}
	}
	return false
}

// withProfileText replaces `_profile` in body — one tenant's generically
// decoded block — with the name /metrics elects from it, its flat-plane
// text from profileTexts (#2433):
//   - a scalar: its text (bare `010` is "010", not int 8);
//   - a mapping with a `default:` key (the scheduled-value form): its
//     default's text, which is all ApplyProfiles reads — a profile is not
//     time-windowed, so the mapping's other keys elect nothing and
//     /effective carries the elected name alone.
//
// A null, a sequence and a mapping without `default:` are left as the
// generic decode gave them. A null keeps its meaning in the overlay (see
// overlayTenant). For the other two the planes differ in what they read:
// /metrics serialises the value to YAML text and elects that as a name —
// normally an unknown profile, with ApplyProfiles' WARN — while the walker
// elects no profile (profileNameOf). They serve the same values UNLESS a
// profile happens to be named by exactly that YAML text; then /metrics
// applies it and the walker does not. Not handled: no such name is
// expected, and the schema allows only a string here.
//
// ⚠️ The merge-key shape `_profile: {<<: {default: x}}` is the exception to
// "a mapping with `default:`": ScheduledValue checks the written keys, sees
// `<<`, and takes the arbitrary-mapping branch, so /metrics elects the YAML
// text `default: x` (unknown profile). The generic decode resolves the
// merge and sees `default`, so the walker takes that same text from
// profileTexts and shows it as `_profile` — the planes agree.
func withProfileText(body map[string]any, texts map[string]string, tenantID string) {
	text, ok := texts[tenantID]
	if !ok {
		return
	}
	switch v := body["_profile"].(type) {
	case nil, []any:
		return
	case map[string]any:
		if _, scheduled := v["default"]; !scheduled {
			return
		}
	}
	body["_profile"] = text
}

// profileFill is ApplyProfiles' per-key decision, shared by both planes
// (#2117): which entries of canonProfile (already canonicalView'd) fill in
// for a tenant whose own layer is `own`. onDeclared is called for a key
// skipped only because it is declared without a platform value — never for
// one the tenant sets itself (that would report a non-event).
func profileFill[V, O any](canonProfile map[string]V, own map[string]O, canonDeclared map[string]struct{}, onDeclared func(key string)) map[string]V {
	var fill map[string]V
	for key, profileValue := range canonProfile {
		// ⛔ The declared check must look at the BASE, not the whole key.
		// resolveDimensionalRows emits unconditionally — it never consults
		// defaults — so a profile entry spelled `oracle_wait_time_rate{db="x"}`
		// fans out to every tenant on the profile exactly like the flat
		// spelling would, one label segment away from the blocked shape.
		// (Measured: two tenants on such a profile got two rows.)
		//
		// ⛔ …but NOT the `_critical` shape, and the asymmetry is the whole
		// test: block only where blocking buys something. resolveCriticalRows
		// admits on defaults[base], so a profile supplying
		// `jvm_memory_critical` (base `jvm_memory` valued) produces a real
		// critical-tier row — while resolveDeclaredRows refuses that shape
		// outright. Blocking it here would therefore prevent nothing and
		// delete a working row, with a WARN as the only trace. Registry tier
		// membership groups the two together; runtime behaviour does not.
		declaredKey := key
		if i := strings.IndexByte(key, '{'); i > 0 {
			declaredKey = key[:i]
		}
		_, declared := canonDeclared[declaredKey]
		if declared && strings.HasSuffix(key, criticalSuffix) {
			declared = false
		}
		if hasAliasEquivalent(own, key) {
			continue
		}
		if declared {
			if onDeclared != nil {
				onDeclared(key)
			}
			continue
		}
		if fill == nil {
			fill = make(map[string]V)
		}
		fill[key] = profileValue
	}
	return fill
}

// profileFor returns the canonical view of the profile `own` elects, or nil
// when it elects none or an unknown one.
func (pp *PlatformProfiles) profileFor(own map[string]any) (string, map[string]profileEntry) {
	if pp == nil || len(pp.byName) == 0 {
		return "", nil
	}
	name := profileNameOf(own["_profile"])
	if name == "" {
		return "", nil
	}
	profile, ok := pp.byName[name]
	if !ok {
		return "", nil
	}
	return name, canonicalView(profile)
}

// expand returns `own` (the tenant block with the platform overlay applied)
// with the elected profile's values filled in, and the attribution of the
// keys it filled. With nothing to fill `own` is returned as is (no copy).
//
// Attribution follows overlayTenant's: never `_metadata` (not inherited),
// never a null on a threshold key (deepMerge ignores it), a null on a
// reserved key only when `chain` has that key to delete.
func (pp *PlatformProfiles) expand(own, chain map[string]any) (map[string]any, []ProfileOverlaySource) {
	name, profile := pp.profileFor(own)
	if profile == nil {
		return own, nil
	}
	fill := profileFill(profile, own, pp.declared, nil)
	if len(fill) == 0 {
		return own, nil
	}
	out := make(map[string]any, len(own)+len(fill))
	for k, v := range own {
		out[k] = v
	}
	byFile := make(map[string][]string)
	for k, e := range fill {
		out[k] = e.value
		if k == "_metadata" {
			continue
		}
		if e.value == nil {
			if _, inherited := chain[k]; !strings.HasPrefix(k, "_") || !inherited {
				continue
			}
		}
		byFile[e.file] = append(byFile[e.file], k)
	}
	var sources []ProfileOverlaySource
	for _, f := range pp.files {
		keys := byFile[f]
		if len(keys) == 0 {
			continue
		}
		sort.Strings(keys)
		sources = append(sources, ProfileOverlaySource{Profile: name, File: f, Keys: keys})
	}
	return out, sources
}

// inherited is the part of the profile a tenant key falls back to when it
// is deleted from the tenant file — what EffectiveConfig.MergedDefaults (the
// guard's "inherited value") merges over the chain, below
// platformInherited. nil when nothing qualifies.
//
// Only a non-null profile value, and only for a key the tenant does not
// write as a mapping (a leaf removed from the tenant's mapping leaves the
// key set, so the profile does not fill it and the leaf falls back to the
// chain). A MAPPING profile value (the schedule form, `{default: …}`) over
// a scalar the tenant writes is kept: deleting the scalar falls back to the
// profile's mapping merged into the chain — which is what MergedDefaults
// then holds, so the guard compares the tenant's scalar with its leaves and
// never calls it redundant. (platformInherited follows the same rule for
// the platform overlay's values, #2191.) A key the platform overlay sets
// under any spelling is left out: deleting the tenant's value falls back to the overlay's, not the profile's. The profile is the one
// `own` (tenant + overlay) elects.
func (pp *PlatformProfiles) inherited(own, tenantRaw map[string]any, overlay []PlatformBlock) map[string]any {
	_, profile := pp.profileFor(own)
	if profile == nil {
		return nil
	}
	platformOnly := make(map[string]any)
	for _, pb := range overlay {
		for k, v := range pb.Block {
			platformOnly[k] = v
		}
	}
	candidates := profileFill(profile, platformOnly, pp.declared, nil)
	var out map[string]any
	for k, e := range candidates {
		if k == "_metadata" || k == "_profile" || e.value == nil {
			continue
		}
		if _, tenantMap := tenantRaw[k].(map[string]any); tenantMap {
			continue
		}
		if out == nil {
			out = make(map[string]any)
		}
		out[k] = e.value
	}
	return out
}

// ============================================================
// Reload attribution (#2117) — package main's classifyTenant asks these
// which tenants a change to the root platform files' `profiles:` feeds,
// the way platform_overlay's helpers answer it for `tenants:`.
// ============================================================

// ChangedProfiles returns the profile names whose merged entry (a key, its
// value, or the file that supplies it) differs between two sets; nil when
// none. A name present on one side only is changed. A nil set is empty.
func ChangedProfiles(prior, next *PlatformProfiles) map[string]struct{} {
	var out map[string]struct{}
	mark := func(name string) {
		if out == nil {
			out = make(map[string]struct{})
		}
		out[name] = struct{}{}
	}
	pb, nb := prior.names(), next.names()
	for name, p := range pb {
		if n, ok := nb[name]; !ok || !reflect.DeepEqual(p, n) {
			mark(name)
		}
	}
	for name := range nb {
		if _, ok := pb[name]; !ok {
			mark(name)
		}
	}
	return out
}

func (pp *PlatformProfiles) names() map[string]map[string]profileEntry {
	if pp == nil {
		return nil
	}
	return pp.byName
}

// ProfileDelta compares profile `name` between two sets: absPaths are the
// files whose part of it changed (the scope attribution), keys the
// canonical keys whose value changed (the shadow test). Both nil when the
// profile did not change.
func ProfileDelta(prior, next *PlatformProfiles, name string) (absPaths, keys []string) {
	p, n := prior.names()[name], next.names()[name]
	files := map[string]struct{}{}
	for k, pe := range p {
		if ne, ok := n[k]; !ok || !reflect.DeepEqual(pe, ne) {
			files[prior.absOf(pe.file)] = struct{}{}
			if ok {
				files[next.absOf(ne.file)] = struct{}{}
			}
		}
	}
	for k, ne := range n {
		if _, ok := p[k]; !ok {
			files[next.absOf(ne.file)] = struct{}{}
		}
	}
	for f := range files {
		absPaths = append(absPaths, f)
	}
	sort.Strings(absPaths)
	pc, nc := canonicalView(p), canonicalView(n)
	for k, pe := range pc {
		if ne, ok := nc[k]; !ok || !reflect.DeepEqual(pe.value, ne.value) {
			keys = append(keys, k)
		}
	}
	for k := range nc {
		if _, ok := pc[k]; !ok {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	return absPaths, keys
}

func (pp *PlatformProfiles) absOf(file string) string {
	if pp != nil {
		if a, ok := pp.abs[file]; ok {
			return a
		}
	}
	return file
}

// Expand is expand's layer without the attribution: `own` (a tenant layer,
// ApplyPlatformOverlay's result) with its elected profile's keys filled in —
// every top-level key the tenant's config takes from above the defaults
// chain. For package main's reload classifier: a CHAIN change to a key the
// profile fills is shadowed, exactly as one the tenant file writes. nil pp =
// no profiles (own returned as is).
func (pp *PlatformProfiles) Expand(own map[string]any) map[string]any {
	out, _ := pp.expand(own, nil)
	return out
}

// ElectedProfile is the profile a tenant layer (the tenant block with the
// platform overlay applied — ApplyPlatformOverlay) elects; "" for none.
func ElectedProfile(own map[string]any) string { return profileNameOf(own["_profile"]) }

// ProfileKeysSetBy reports whether `own` (a tenant layer) sets every key in
// keys under some spelling — so a change to those profile keys cannot
// reach the tenant (the fill-in skips them): "shadowed".
func ProfileKeysSetBy(own map[string]any, keys []string) bool {
	for _, k := range keys {
		if !hasAliasEquivalent(own, k) {
			return false
		}
	}
	return true
}
