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
// ⚠️ RESERVED (`_`-prefixed) KEYS ARE NOT TOUCHED. For them ADR-017's null
// is a real instruction — "delete the inherited value" — honoured by
// deepMerge on the walker and, on /metrics, by the empty value displacing
// the platform's (a `_state_<f>: null` turns a platform `disable` off).
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
// a write of nothing. raw is the generic YAML decode of the value.
func nullThreshold(key string, raw any) bool {
	return raw == nil && !strings.HasPrefix(key, "_")
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
// (nullThreshold) of a `tenants:` or `profiles:` body.
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
		return // cannot happen: the typed decode of the same bytes succeeded
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

// rootNullUndeclared is, per tenant of cfg (the built config: tenant files,
// platform entries, profiles and the subtree overlay applied), each key of
// the tenant's map that is a spelling of a key the root carrier wrote as null
// (rootNull, rootNullDefaults) and that nothing on the output plane serves
// (keyCanReachTheOutputPlane, the subtree overlay's own test), with its
// value — filtered by undeliverableThresholds, the filter #1976's report
// uses (reserved keys, keys resolveBaseRows never serves, switched-off keys).
// nil when there is none.
//
// ⛔ ONE CAUSE, ONE REPORT. A key a subtree `_defaults.yaml` hands down is
// never in a tenant's map when it cannot be delivered — the overlay records
// it in Unreachable instead (subtree_default_undeliverable) — so the two
// sets cannot share a (tenant, key).
func rootNullUndeclared(cfg *ThresholdConfig, rootNull []string) map[string]map[string]ScheduledValue {
	if len(rootNull) == 0 {
		return nil
	}
	isNull := make(map[string]struct{}, len(rootNull))
	for _, k := range rootNull {
		isNull[k] = struct{}{}
	}
	hit := func(k string) bool {
		if _, ok := isNull[k]; ok {
			return true
		}
		var buf [2]string
		for _, s := range otherSpellings(k, &buf) {
			if _, ok := isNull[s]; ok {
				return true
			}
		}
		return false
	}
	var out map[string]map[string]ScheduledValue
	for tenantID, overrides := range cfg.Tenants {
		for k, v := range overrides {
			if !hit(k) || keyCanReachTheOutputPlane(cfg, k, v) {
				continue
			}
			if out == nil {
				out = map[string]map[string]ScheduledValue{}
			}
			if out[tenantID] == nil {
				out[tenantID] = map[string]ScheduledValue{}
			}
			out[tenantID][k] = v
		}
	}
	return undeliverableThresholds(out)
}

func dropNullBodies(typed map[string]map[string]ScheduledValue, raw map[string]map[string]any) {
	for id, body := range raw {
		for k, v := range body {
			if nullThreshold(k, v) {
				delete(typed[id], k)
			}
		}
	}
}

// hasNullCandidate reports whether the typed decode holds a value a null
// decodes to: a 0 default, or an empty ScheduledValue under a threshold key.
func hasNullCandidate(cfg *ThresholdConfig) bool {
	for _, v := range cfg.Defaults {
		if v == 0 {
			return true
		}
	}
	return hasEmptyThreshold(cfg.Tenants) || hasEmptyThreshold(cfg.Profiles)
}

func hasEmptyThreshold(bodies map[string]map[string]ScheduledValue) bool {
	for _, body := range bodies {
		for k, sv := range body {
			if sv.Default == "" && len(sv.Overrides) == 0 && sv.Expiry == nil && !strings.HasPrefix(k, "_") {
				return true
			}
		}
	}
	return false
}
