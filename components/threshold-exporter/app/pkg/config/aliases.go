package config

// aliases.go — deprecated tenant-config key aliases (#1231).
//
// The mysql_cpu → mysql_threads_running rename (#944 semantic fix, #1200 WS1a,
// #1231 owner-approved execution) retires a poisoned key name: the metric has
// always measured mysql threads_running saturation, never host CPU%. During
// the 2-release transition window both spellings must keep working, so resolve
// and validation canonicalize deprecated spellings onto the new key AT THE
// BOUNDARY (a non-destructive view — the parsed config, GET /{id} raw output
// and on-disk YAML are never rewritten), and resolve dual-emits the legacy
// user_threshold series next to the canonical one so downstream rules/dashboards
// migrate without a gap.
//
// SSOT note: the alias SSOT is the registry's deprecated_aliases section
// (rule-packs/threshold-registry.yaml, authored in
// scripts/tools/ops/_registry_lib.py DEPRECATED_KEY_ALIASES). This table is a
// runtime mirror, PINNED to that section by aliases_registry_test.go — a
// drifted mirror fails CI. To open/close an alias window: edit the authored
// table, regen the registry, then update this mirror (and the Python one in
// _grar_validate.py) to match.
//
// Lookup contract: EXACT key match only, plus exactly two derived shapes that
// canonicalKeyFor handles structurally ("<base>_critical" and dimensional
// "<base>{...}").
// ⛔ Never prefix-match against this table: "mysql_cpu" is a string prefix of
// BOTH "mysql_cpu_critical" (legitimate, handled via suffix derivation) and of
// typos like "mysql_cpu_util" (must keep failing unknown-key validation). A
// strings.HasPrefix lookup would silently canonicalize — and therefore
// accept — keys that were never valid.

import "strings"

const criticalSuffix = "_critical"

// deprecatedKeyAliases maps a retired (deprecated) base metric key to its
// canonical replacement. The deprecated spelling keeps resolving (as its
// canonical key) for the 2-release transition window, then the entry is
// removed and the old name is permanently retired.
var deprecatedKeyAliases = map[string]string{
	// #944 / #1231: threads_running saturation, NOT host CPU%.
	"mysql_cpu": "mysql_threads_running",
}

// legacyKeyByCanonical is the reverse view (canonical base key → legacy base
// key), built once at package init. It drives the transition-window dual-emit
// in resolveBaseRows / resolveCriticalRows / resolveDimensionalRows: while an
// alias entry exists, the canonical row always ships with a legacy twin —
// regardless of which spelling the tenant used — so `user_threshold{metric="cpu"}`
// and `{metric="threads_running"}` fire in lockstep with the same value (and,
// for dimensional rows, the same label set).
var legacyKeyByCanonical = func() map[string]string {
	m := make(map[string]string, len(deprecatedKeyAliases))
	for legacy, canonical := range deprecatedKeyAliases {
		m[canonical] = legacy
	}
	return m
}()

// canonicalKeyFor returns the canonical spelling for a tenant-config key and
// whether the key was a deprecated alias. Exactly three shapes canonicalize:
//
//	mysql_cpu                → mysql_threads_running                (exact)
//	mysql_cpu_critical       → mysql_threads_running_critical       (suffix-derived)
//	mysql_cpu{version="v2"}  → mysql_threads_running{version="v2"}  (dimensional base)
//
// Everything else — including typos that merely share the prefix, like
// mysql_cpu_util — is returned unchanged (exact-match contract, see the
// package comment's ⛔ note).
func canonicalKeyFor(key string) (string, bool) {
	if canon, ok := deprecatedKeyAliases[key]; ok {
		return canon, true
	}
	if base, found := strings.CutSuffix(key, criticalSuffix); found {
		if canon, ok := deprecatedKeyAliases[base]; ok {
			return canon + criticalSuffix, true
		}
	}
	if i := strings.IndexByte(key, '{'); i > 0 {
		if canon, ok := deprecatedKeyAliases[key[:i]]; ok {
			return canon + key[i:], true
		}
	}
	return key, false
}

// legacySpellingFor is the inverse of canonicalKeyFor for the same three
// shapes: given a canonical-spelled key, it returns the deprecated spelling
// (if the key's base is an alias target). Used by tenantHasAliasEquivalent.
func legacySpellingFor(key string) (string, bool) {
	if legacy, ok := legacyKeyByCanonical[key]; ok {
		return legacy, true
	}
	if base, found := strings.CutSuffix(key, criticalSuffix); found {
		if legacy, ok := legacyKeyByCanonical[base]; ok {
			return legacy + criticalSuffix, true
		}
	}
	if i := strings.IndexByte(key, '{'); i > 0 {
		if legacy, ok := legacyKeyByCanonical[key[:i]]; ok {
			return legacy + key[i:], true
		}
	}
	return "", false
}

// canonicalizeDefaults returns a canonical VIEW of a defaults map. Fast path:
// when no deprecated spelling is present (the steady state), the ORIGINAL map
// is returned unchanged — zero allocations on the per-scrape hot path.
//
// Dedup contract: when a map carries BOTH spellings of the same threshold,
// the canonical entry wins and the deprecated one is ignored. This is the
// explicit guard that keeps one threshold identity from producing two rows
// with identical label sets (which would fail the whole Prometheus Gather).
// ValidateTenantKeys surfaces the conflict as a notice.
func canonicalizeDefaults(defaults map[string]float64) map[string]float64 {
	clean := true
	for k := range defaults {
		if _, isAlias := canonicalKeyFor(k); isAlias {
			clean = false
			break
		}
	}
	if clean {
		return defaults
	}
	out := make(map[string]float64, len(defaults))
	for k, v := range defaults {
		canon, isAlias := canonicalKeyFor(k)
		if !isAlias {
			out[k] = v
			continue
		}
		if _, canonPresent := defaults[canon]; canonPresent {
			continue // canonical wins; deprecated duplicate ignored
		}
		out[canon] = v
	}
	return out
}

// canonicalizeOptionalOverrides returns the platform's declared-without-value
// key list as a canonical membership SET — the other half of the membership
// universe ValidateTenantKeys checks against, mirroring canonicalizeDefaults.
//
// ⛔ Canonicalizing is not a nicety. ValidateTenantKeys compares the tenant's
// CANONICALIZED key against canonicalizeDefaults' output, so a list entry left
// in a deprecated spelling (`mysql_cpu`) would never match the canonical tenant
// key (`mysql_threads_running`): the platform would be declaring a key that
// stays permanently un-settable, and nothing would say so — the exact
// silent-membership-drift #1189 is about, reappearing inside its own fix.
//
// ⚠️ Scope, honestly: this closes the DEPRECATED-SPELLING cause of that, and
// only that. An entry that is a typo, carries stray whitespace (nothing strips
// it), names a key no rule pack consumes, or is written in a `_critical` /
// dimensional spelling the cascade never looks up whole, is still a silently
// inert member. Nothing validates the list's CONTENTS on either side yet — the
// shared verdict table records those rows as known gaps rather than pretending
// they are covered (tests/shared/optional_overrides_membership_matrix.json).
//
// A set, not a map view: membership is the entire payload, so both spellings of
// one threshold collapse onto the same canonical entry for free (the
// canonical-wins dedup canonicalizeDefaults spells out is structural here).
//
// ⚠️ This DOES run per scrape as of the declared-key emission loop
// (ResolveAtWithStats hoists it beside canonDefaults), so the empty-list fast
// path is load-bearing rather than cosmetic: with nothing declared — every
// shipped config today — it must not allocate on the hot path. A nil map still
// answers `_, ok := m[k]` correctly, so callers need no nil check.
func canonicalizeOptionalOverrides(keys []string) map[string]struct{} {
	if len(keys) == 0 {
		return nil
	}
	out := make(map[string]struct{}, len(keys))
	for _, k := range keys {
		canon, _ := canonicalKeyFor(k)
		out[canon] = struct{}{}
	}
	return out
}

// canonicalizeOverrides returns a canonical VIEW of one tenant's overrides map
// plus the number of deprecated spellings found (drives the
// da_config_deprecated_keys gauge). Same fast-path + canonical-wins dedup
// contract as canonicalizeDefaults; the input map is never mutated, so raw
// views (GET /{id}) keep showing the tenant's file verbatim.
func canonicalizeOverrides(overrides map[string]ScheduledValue) (map[string]ScheduledValue, int) {
	deprecated := 0
	for k := range overrides {
		if _, isAlias := canonicalKeyFor(k); isAlias {
			deprecated++
		}
	}
	if deprecated == 0 {
		return overrides, 0
	}
	out := make(map[string]ScheduledValue, len(overrides))
	for k, v := range overrides {
		canon, isAlias := canonicalKeyFor(k)
		if !isAlias {
			out[k] = v
			continue
		}
		if _, canonPresent := overrides[canon]; canonPresent {
			continue // canonical wins; deprecated duplicate ignored
		}
		out[canon] = v
	}
	return out, deprecated
}

// appendWithLegacyTwin appends row and, when canonicalKey is the target of a
// deprecated alias still inside the transition window (#1231), a second row
// carrying the legacy metric identity — same tenant/value/severity, with
// Component/Metric derived from the legacy key. Emitting the twin at the
// resolve layer (not the collector) keeps the cardinality guard counting it,
// and /simulate + GET /{id}/effective automatically consistent.
func appendWithLegacyTwin(rows []ResolvedThreshold, canonicalKey string, row ResolvedThreshold) []ResolvedThreshold {
	rows = append(rows, row)
	if legacyKey, ok := legacyKeyByCanonical[canonicalKey]; ok {
		twin := row
		twin.Component, twin.Metric = parseMetricKey(legacyKey)
		// Mark the twin with its canonical base key so truncationSortKey can
		// order it right behind the row it shadows (#1231 F3) — the twin must
		// never survive a cardinality cut that its canonical row did not.
		twin.legacyTwinOf = canonicalKey
		rows = append(rows, twin)
	}
	return rows
}

// tenantHasAliasEquivalent reports whether overrides already contains key
// under ANY spelling: the key itself, its canonical form, or the legacy form
// of that canonical. ApplyProfiles uses this for its fill-in check so a
// profile value never displaces a tenant's own setting that is merely spelled
// with the other name of the same threshold (#1231 transition window) — the
// four-layer priority (tenant beats profile) must hold across spellings.
func tenantHasAliasEquivalent(overrides map[string]ScheduledValue, key string) bool {
	if _, ok := overrides[key]; ok {
		return true
	}
	canon, _ := canonicalKeyFor(key)
	if canon != key {
		if _, ok := overrides[canon]; ok {
			return true
		}
	}
	if legacy, ok := legacySpellingFor(canon); ok {
		if _, ok := overrides[legacy]; ok {
			return true
		}
	}
	return false
}
