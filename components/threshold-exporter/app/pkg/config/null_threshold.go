package config

// A threshold written as YAML null writes nothing (#2518).
//
// ⛔ ONE PREDICATE, EVERY LAYER, BOTH PLANES. The owner's ruling (option a):
// a threshold override whose value is null means "this layer has no value
// for it — use the next layer down", the walker's deepMerge rule and the
// Helm values convention. Before, the planes answered three different ways:
//
//   - the walker's fold (deepMerge, noteSpellingWriters) skipped the null —
//     the next layer down showed through;
//   - /metrics kept the null as a PRESENT empty ScheduledValue in the
//     tenant's map, so the profile fill, the subtree overlay
//     (tenantAuthoredThreshold, `overrides[key]`) and the tenant-api's
//     platform supply (supplyFor) all counted the key as written, and
//     resolve then fell back past every layer to the ROOT default;
//   - the walker's own ownership checks (overlayTenant's `tenantRaw[k]`,
//     profileFill's hasAliasEquivalent over `own`) counted it as written
//     too, so a platform or profile value below a null was skipped there.
//
// And at the ROOT `defaults:` (map[string]float64) a null decoded to a
// present 0 — a threshold of 0 for every tenant, while the walker had no key.
//
// Rather than teach each of those predicates about null, the null is taken
// out at the one place each plane reads a layer: ParseConfigFile for
// /metrics (and the tenant-api merge core, which decodes through it), and
// the walker's tenant block / platform entry / profile entry readers. Every
// downstream "does this layer write the key" question then sees the key as
// absent, which is what the ruling says it is.
//
// ⚠️ RESERVED (`_`-prefixed) KEYS ARE NOT DROPPED. For them ADR-017's null
// is a real instruction — "delete the inherited value", so the tenant gets
// the platform default — honoured by deepMerge on the walker and, on
// /metrics, by reservedNullMarker: the null stays in the body as a marker,
// which displaces a platform `tenants:` entry's and a profile's value like
// any written value, and which the readers take as no key at all
// (tenantReserved, #3341). Before, the null decoded to a present `""`
// indistinguishable from a written `""`, and `_state_<f>: null` turned the
// filter ON even under `default_state: disable`.
// Threshold keys are exactly the non-`_` keys (deepMerge's comment has the
// schema argument), dimensional `k{…}` and `<k>_critical` keys included.
//
// ⚠️ ONLY A NULL. `k: ""` is a written empty string on the walker (it shows
// in /effective) and an "unknown value" on /metrics; that divergence is a
// different shape and is left as is.

import (
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// nullThreshold reports whether (key, raw) is a threshold written as null —
// a write of nothing. raw is the generic YAML decode of the value: a null, or
// a schedule that writes nothing (nullSchedule, #2708).
func nullThreshold(key string, raw any) bool {
	return (raw == nil || nullSchedule(raw)) && !strings.HasPrefix(key, "_")
}

// nullSchedule reports whether raw (a generic YAML decode) is a schedule that
// writes nothing: a mapping whose `default:` is null, with no override window
// (`overrides:` absent, `[]` or null) and no key other than ScheduledValue's
// own `expires:` / `reason:` (#2708). By the owner's ruling it is plain null.
// Before, /metrics kept it as a present empty value (resolve fell back to the
// ROOT default) while the walker showed it as that layer's own value.
//
// Any other extra key is left as it was. A schedule WITH a window and a null
// in it is refused instead (scheduleNullProblems, schedule_null.go).
func nullSchedule(raw any) bool {
	m, ok := raw.(map[string]any)
	if !ok {
		return false
	}
	d, has := m["default"]
	if !has || d != nil {
		return false
	}
	for k, v := range m {
		switch k {
		case "default", "expires", "reason":
		case "overrides":
			if l, isList := v.([]any); v != nil && (!isList || len(l) > 0) {
				return false
			}
		default:
			return false
		}
	}
	return true
}

// reservedNull reports whether (key, raw) is a reserved (`_`) key written as
// null: a null, or a schedule that writes nothing (nullSchedule) — except
// for a key whose mapping is kept whole (mappingReservedKeys), where
// `default:` is a field of the mapping, not a schedule's. raw is the generic
// YAML decode of the value. A schedule whose `default:` is null beside a
// window is not one: the write gate refuses that form (#3269).
func reservedNull(key string, raw any) bool {
	if !strings.HasPrefix(key, "_") {
		return false
	}
	return raw == nil || (!mappingReservedKeys[key] && nullSchedule(raw))
}

// reservedNullMarker is the value a reserved key written as null holds in a
// decoded `tenants:` / `profiles:` body (#3341): present, so the flat merge
// and the profile fill treat it as this layer's write and the layers below
// do not show through; and read as no key by tenantReserved, so each reader
// takes its no-key branch — the platform default.
//
// ⛔ A MARKER IN THE MAP, NOT A KEY DELETED AFTER THE MERGE. The exporter's
// incremental reload (package main's patchTenants) reuses the published
// maps of the tenants it does not re-read and runs ApplyProfiles over them
// again; a key deleted after the first build would then be filled from the
// profile on the next reload of an unrelated file — and written into a map
// a scrape is reading. A marker is still there, so the fill skips it.
var reservedNullMarker = ScheduledValue{deleted: true}

// tenantReserved is overrides[key] as the readers of a reserved key read it:
// ok=false both when the key is absent and when the tenant side wrote it as
// null (reservedNullMarker).
func tenantReserved(overrides map[string]ScheduledValue, key string) (ScheduledValue, bool) {
	sv, ok := overrides[key]
	if !ok || sv.deleted {
		return ScheduledValue{}, false
	}
	return sv, true
}

// withoutNullThresholds returns m without its nullThreshold entries — m
// itself (no copy) when it has none, so the steady state allocates nothing.
func withoutNullThresholds(m map[string]any) map[string]any {
	n := 0
	for k, v := range m {
		if nullThreshold(k, v) {
			n++
		}
	}
	if n == 0 {
		return m
	}
	out := make(map[string]any, len(m)-n)
	for k, v := range m {
		if !nullThreshold(k, v) {
			out[k] = v
		}
	}
	return out
}

// dropNullThresholds removes from a typed decode every entry the file wrote
// as null: a `defaults:` key (any key — at the root nothing sits below it,
// and the walker's root level has no such key either), and a threshold key
// (nullThreshold) of a `tenants:` or `profiles:` body; a reserved key of
// such a body written as null (reservedNull) becomes reservedNullMarker.
//
// The typed decode cannot tell a null from what it decodes to — 0 in
// `defaults:`, an empty ScheduledValue in a body (yaml.v3 never calls
// UnmarshalYAML for a null) — so the bytes are re-read generically, but only
// when the typed decode holds such a zero value: a file without one pays a
// map walk and nothing more.
func dropNullThresholds(cfg *ThresholdConfig, data []byte) {
	if !hasNullCandidate(cfg) {
		return
	}
	var raw struct {
		Defaults map[string]any            `yaml:"defaults"`
		Tenants  map[string]map[string]any `yaml:"tenants"`
		Profiles map[string]map[string]any `yaml:"profiles"`
	}
	if yaml.Unmarshal(data, &raw) != nil {
		// Cannot happen for a tenant file: ParseTenantFile's generic decode
		// of the same node succeeded first (#3063). A file read through
		// ParseConfigFile alone can get here — a scalar in a body whose
		// explicit tag its text does not satisfy (the typed decode never
		// resolves it) — and keeps its nulls undropped, as before.
		return
	}
	for _, k := range nullDefaultKeys(raw.Defaults) {
		delete(cfg.Defaults, k)
	}
	dropNullBodies(cfg.Tenants, raw.Tenants)
	dropNullBodies(cfg.Profiles, raw.Profiles)
}

// nullDefaultKeys is the keys of a generic `defaults:` decode written as
// null, sorted — the keys dropNullThresholds drops from `defaults:`.
func nullDefaultKeys(defaults map[string]any) []string {
	var out []string
	for k, v := range defaults {
		if v == nil {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}

// rootNullDefaults is nullDefaultKeys over a file's bytes: the `defaults:`
// keys ParseConfigFile dropped from it because they are written as null. For
// the conf.d root carrier those keys are not declared at all (#2518), so a
// tenant that sets one gets no series (rootNullUndeclared). Call it only on
// bytes ParseConfigFile accepted.
func rootNullDefaults(data []byte) []string {
	var raw struct {
		Defaults map[string]any `yaml:"defaults"`
	}
	if yaml.Unmarshal(data, &raw) != nil {
		return nil
	}
	return nullDefaultKeys(raw.Defaults)
}

// RootNullKey is one key a tenant's config sets that is not served because
// the conf.d root `_defaults.yaml` writes the key it depends on as null
// (#2518): FlatBuild.RootNullUndeclared, ScopedTenants.RootNullUndeclared.
type RootNullKey struct {
	// Key is the tenant-side key as the tenant's config spells it.
	Key string
	// NullKey is the threshold whose missing root value leaves Key
	// unserved, as the fix names it: Key itself for a base threshold; the
	// (canonical) base for a critical row.
	NullKey string
	// RootSpellings is how the root `_defaults.yaml` spells what it writes
	// as null — NullKey's spelling(s) in the file, sorted — so the report
	// names text the root actually holds even when the tenant uses the
	// other #1231 spelling.
	RootSpellings []string
	// CriticalRow: Key is a `<base>_critical` key, which resolveCriticalRows
	// serves only when the root defaults hold <base> — `optional_overrides:`
	// does not stand in for it.
	CriticalRow bool
}

// rootNullUndeclared is, per tenant of cfg (the built config: tenant files,
// platform entries, profiles and the subtree overlay applied), sorted by Key,
// each key of the tenant's map that no row will serve because of a root
// null (rootNull, rootNullDefaults). Two ways, each judged by the predicate
// of the code that would serve the key:
//
//   - a base threshold that is a spelling of a root-null key and that
//     nothing on the output plane iterates (keyCanReachTheOutputPlane, the
//     subtree overlay's own test), filtered by undeliverableThresholds'
//     rules (reserved keys, keys resolveBaseRows never serves, switched-off
//     keys) — the filter #1976's report uses;
//   - a critical-row key (criticalRowBase, resolveCriticalRows' own entry
//     test) whose base, under either spelling, is root-null and absent from
//     the canonical root defaults — the lookup resolveCriticalRows drops
//     the row on. keyCanReachTheOutputPlane passes every `_critical` key
//     (the resolver WARNs on its own), so it cannot answer this one. A
//     `_critical` whose base no root ever declared is not root-null-caused
//     and is left out. A switched-off key is left out, as the resolver
//     skips it before the lookup.
//
// nil when there is none.
//
// ⛔ ONE CAUSE, ONE REPORT. A key a subtree `_defaults.yaml` hands down is
// never in a tenant's map when it cannot be delivered — the overlay records
// it in Unreachable instead (subtree_default_undeliverable) — so the two
// sets cannot share a (tenant, key).
func rootNullUndeclared(cfg *ThresholdConfig, rootNull []string) map[string][]RootNullKey {
	if len(rootNull) == 0 {
		return nil
	}
	isNull := make(map[string]struct{}, len(rootNull))
	for _, k := range rootNull {
		isNull[k] = struct{}{}
	}
	// spelled is the root's null spellings of k's threshold, sorted; empty
	// when the root writes none of them as null.
	spelled := func(k string) []string {
		var out []string
		if _, ok := isNull[k]; ok {
			out = append(out, k)
		}
		var buf [2]string
		for _, s := range otherSpellings(k, &buf) {
			if _, ok := isNull[s]; ok {
				out = append(out, s)
			}
		}
		sort.Strings(out)
		return out
	}
	canonDefaults := canonicalizeDefaults(cfg.Defaults)
	var out map[string][]RootNullKey
	add := func(tenantID string, rk RootNullKey) {
		if out == nil {
			out = map[string][]RootNullKey{}
		}
		out[tenantID] = append(out[tenantID], rk)
	}
	for tenantID, overrides := range cfg.Tenants {
		for k, v := range overrides {
			canon, _ := canonicalKeyFor(k)
			if base, critical := criticalRowBase(canon); critical {
				if _, declared := canonDefaults[base]; !declared && !disabledEverywhere(v) {
					if rs := spelled(base); len(rs) > 0 {
						add(tenantID, RootNullKey{Key: k, NullKey: base, RootSpellings: rs, CriticalRow: true})
					}
				}
				continue
			}
			rs := spelled(k)
			if len(rs) == 0 || keyCanReachTheOutputPlane(cfg, k) {
				continue
			}
			if IsReservedKey(k) || baseRowsSkipKey(k) || disabledEverywhere(v) {
				continue // undeliverableThresholds' rules
			}
			add(tenantID, RootNullKey{Key: k, NullKey: k, RootSpellings: rs})
		}
	}
	for _, keys := range out {
		sort.Slice(keys, func(i, j int) bool { return keys[i].Key < keys[j].Key })
	}
	return out
}

func dropNullBodies(typed map[string]map[string]ScheduledValue, raw map[string]map[string]any) {
	for id, body := range raw {
		for k, v := range body {
			switch {
			case nullThreshold(k, v):
				delete(typed[id], k)
			case reservedNull(k, v):
				if _, in := typed[id][k]; in {
					typed[id][k] = reservedNullMarker
				}
			}
		}
	}
}

// hasNullCandidate reports whether the typed decode holds a value a null
// decodes to: a 0 default, or a ScheduledValue with no default and no window
// (its expiry is not asked: `{default: null, expires: …}` is null too,
// #2708), under a threshold key or a reserved one (#3341).
func hasNullCandidate(cfg *ThresholdConfig) bool {
	for _, v := range cfg.Defaults {
		if v == 0 {
			return true
		}
	}
	return hasEmptyValue(cfg.Tenants) || hasEmptyValue(cfg.Profiles)
}

func hasEmptyValue(bodies map[string]map[string]ScheduledValue) bool {
	for _, body := range bodies {
		for _, sv := range body {
			if sv.Default == "" && len(sv.Overrides) == 0 {
				return true
			}
		}
	}
	return false
}
