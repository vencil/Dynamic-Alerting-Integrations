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
// (rule-packs/threshold-registry.yaml, authored there directly since
// D-A PR-3). This table is a
// runtime mirror, PINNED to that section by aliases_registry_test.go — a
// drifted mirror fails CI. To open/close an alias window: edit the registry's
// deprecated_aliases section, then update this mirror to match. It is the
// only one: Python asks this package's rule through `da-guard key-spellings`
// and keeps no copy of the table or the rule (#3556).
//
// Lookup contract: EXACT key match only, plus the two derived shapes that
// canonicalKeyFor handles structurally, alone or together: "<base>_critical"
// and dimensional "<base>{...}" ("<base>_critical{...}" is both, #3556).
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

// CanonicalKeyFor resolves a deprecated spelling to its canonical one
// (exported for the overlay's reachability check and for testing).
func CanonicalKeyFor(key string) (string, bool) { return canonicalKeyFor(key) }

// SpellingsOf is every spelling of key's threshold, the canonical one first
// (canonicalKeyFor, then its legacySpellingFor): `[key]` for a key no alias
// touches. It is what `da-guard key-spellings` prints, so a reader that
// compares keys across #1231 spellings (Policy-as-Code, diagnose, the
// threshold tools) asks this rule instead of keeping its own (#3556).
func SpellingsOf(key string) (canonical string, spellings []string) {
	canonical, _ = canonicalKeyFor(key)
	spellings = []string{canonical}
	if legacy, ok := legacySpellingFor(canonical); ok {
		spellings = append(spellings, legacy)
	}
	return canonical, spellings
}

// LegacySpellingFor is CanonicalKeyFor's inverse: given a canonical key it
// returns the retired spelling that resolves to it, across all three shapes
// (exact, `_critical`-suffixed, dimensional).
//
// The overlay's reachability check needs both directions, because a root
// `_defaults.yaml` may still carry the retired spelling while a subtree has
// already moved to the canonical one. Exported rather than reconstructed at
// the call site: the caller's only alternative is scanning every platform
// default per candidate key per tenant, which is O(tenants x keys x defaults)
// on a path that runs on every reload.
//
// ⛔ IT DELEGATES RATHER THAN REIMPLEMENTS, and that is the point. The first
// version handled only the base shape, with a comment claiming `_critical` and
// dimensional keys "never take that route — they are served by their own
// resolvers". Measured false: five such shapes (`_state_X_critical`,
// `_silent_X_critical`, `_routing_X{…}`, `_state_X{…}`, `_silent_X{…}`) reach
// `declaredAnywhere`. A differential over the whole reachable key space found
// no behaviour change today — but only because the single alias in the
// registry has a base that carries none of those prefixes. That is an accident
// of the current table, not a property, and the next alias would have made the
// two predicates disagree. (#1569 blind review.)
func LegacySpellingFor(canonical string) (string, bool) { return legacySpellingFor(canonical) }

// canonicalKeyFor returns the canonical spelling for a tenant-config key and
// whether the key was a deprecated alias. The alias applies to the key's
// head (the part before `{`, or the whole key), exactly or with a
// `_critical` suffix; a `{...}` part is carried as written:
//
//	mysql_cpu                    → mysql_threads_running                    (exact)
//	mysql_cpu_critical           → mysql_threads_running_critical           (suffix-derived)
//	mysql_cpu{version="v2"}      → mysql_threads_running{version="v2"}      (dimensional base)
//	mysql_cpu_critical{db="a"}   → mysql_threads_running_critical{db="a"}   (both, #3556)
//
// Everything else — including typos that merely share the prefix, like
// mysql_cpu_util — is returned unchanged (exact-match contract, see the
// package comment's ⛔ note). The one rule serves every reader: the Python
// tools ask it through `da-guard key-spellings` and write none of their own
// (#3556).
func canonicalKeyFor(key string) (string, bool) {
	head, rest := splitKeyHead(key)
	if canon, ok := aliasHead(head, deprecatedKeyAliases); ok {
		return canon + rest, true
	}
	return key, false
}

// spellingFold is what foldSpellings did to one threshold of one map: the
// spelling the kept line was written in when it was not the canonical one
// (written), and the spelling of the other line it folded away when the map
// wrote both (folded). Keyed by the canonical key.
type spellingFold struct{ written, folded string }

// foldSpellings is m with every retired #1231 spelling folded into its
// canonical key (#4253) — the one rule for which line of a map a threshold
// is: with one spelling written, that line, now under the canonical key;
// with both, the line the layer adopts (adopts: the layer's own reading — a
// refused, null or non-finite value is a line it does not take), and the
// canonical one when it adopts both or neither. The folds record, per
// canonical key, what was written, for every reader that reports a retired
// spelling. m itself, and nil, when m writes no retired spelling: m is never
// written to.
func foldSpellings[V any](m map[string]V, adopts func(key string, v V) bool) (map[string]V, map[string]spellingFold) {
	var out map[string]V
	var folds map[string]spellingFold
	for k := range m {
		if !touchesAlias(k) {
			continue
		}
		canon, isAlias := canonicalKeyFor(k)
		if !isAlias {
			continue
		}
		if out == nil {
			out = make(map[string]V, len(m))
			for k2, v2 := range m {
				out[k2] = v2
			}
			folds = map[string]spellingFold{}
		}
		delete(out, k)
		av := m[k]
		cv, both := m[canon]
		switch {
		case !both:
			out[canon] = av
			folds[canon] = spellingFold{written: k}
		case adopts(canon, cv) || !adopts(k, av):
			folds[canon] = spellingFold{folded: k}
		default:
			out[canon] = av
			folds[canon] = spellingFold{written: k, folded: canon}
		}
	}
	if out == nil {
		return m, nil
	}
	return out, folds
}

// valueFold is ScheduledValue.fold: what the decode's #1231 fold did to the
// line (#4253).
type valueFold struct {
	// written is the retired spelling the line was written in; "" for the
	// canonical one.
	written string
	// folded is the spelling of the line folded away beside it — the same
	// map wrote both — and "" when it wrote one.
	folded string
	// other is the line folded away, as written (nil when there was none):
	// for the readers that name it (a value not served), and for when this
	// line's `expires:` passes — it is then as deleted, and the other line,
	// when the layer takes it, is what the layer writes (filterBodies).
	other *ScheduledValue
}

// foldBodies folds every body of m (foldSpellings, #4253), in place: a line
// the layer takes is a threshold-shaped one (isThresholdShaped — a number,
// a `disable`, a schedule); a value no reader takes is no line. The kept
// line carries what was folded (valueFold).
func foldBodies(m map[string]map[string]ScheduledValue) bool {
	twins := false
	for name, body := range m {
		folded, folds := foldSpellings(body, func(_ string, sv ScheduledValue) bool { return isThresholdShaped(sv) })
		if folds == nil {
			continue
		}
		for canon, f := range folds {
			sv := folded[canon]
			vf := &valueFold{written: f.written, folded: f.folded}
			if f.folded != "" {
				other := body[f.folded]
				if f.written == "" { // the line folded away is the retired one
					other.fold = &valueFold{written: f.folded}
				}
				vf.other = &other
			}
			sv.fold = vf
			folded[canon] = sv
		}
		m[name] = folded
		twins = twins || anyTwin(folds)
	}
	return twins
}

// anyTwin reports whether folds folded a line away beside another
// spelling's.
func anyTwin(folds map[string]spellingFold) bool {
	for _, f := range folds {
		if f.folded != "" {
			return true
		}
	}
	return false
}

// levelTakes is foldSpellings' "the layer takes this line" for a raw value
// of a subtree `_defaults.yaml` level or of a `tenants:` / `profiles:` body
// (the walker's maps): written and threshold-shaped (subtreeLevelRead) —
// isThresholdShaped on what the typed decode makes of it.
func levelTakes(key string, raw any) bool {
	_, written, shaped := subtreeLevelRead(key, raw)
	return written && shaped
}

// rootTakesRaw is levelTakes for the root `_defaults.yaml`'s raw values:
// one the root decode does not refuse (rootRefusesRaw), as rootTakes reads
// the typed decode's.
func rootTakesRaw(key string, raw any) bool { return raw != nil && !rootRefusesRaw(key, raw) }

// normalizeKeysTaking is normalizeKeys, then the #1231 twin rule with takes,
// the layer's reading (foldSpellings, #4253): of two spellings of one
// threshold in the map, the line the layer does not take is no line, and the
// canonical one wins when it takes both or neither. The walker's maps are
// what the layers wrote (/effective names a line as written), so the line
// kept keeps its spelling — only the one that loses goes, recorded as a dup
// (a second spelling of a threshold the map also writes, not served) as the
// layer wrote it. The dimensional re-spelling comes first, so two spellings
// whose labels are written differently are one threshold to the rule.
func normalizeKeysTaking[V any](m map[string]V, takes func(string, V) bool) (map[string]V, *keySpellings) {
	normalized, s := normalizeKeys(m)
	_, folds := foldSpellings(normalized, takes)
	var out map[string]V
	for canon, f := range folds {
		if f.folded == "" {
			continue // a lone retired spelling is a line as written
		}
		if out == nil {
			out = make(map[string]V, len(normalized))
			for k, v := range normalized {
				out[k] = v
			}
		}
		delete(out, f.folded)
		loser := s.writtenOf(f.folded)
		if s != nil {
			delete(s.written, f.folded)
		}
		// A null is no line (#2418): beside the other spelling it is no
		// duplicate.
		if nullThreshold(f.folded, any(normalized[f.folded])) {
			continue
		}
		served := canon
		if f.written != "" {
			served = f.written
		}
		if s == nil {
			s = &keySpellings{}
		}
		s.addDup(served, loser)
	}
	if s != nil && len(s.written) == 0 {
		s.written = nil
	}
	if out == nil {
		return normalized, s
	}
	return out, s
}

// TwinSpelledThresholds is, for one decoded tenant body (DecodeTenantBody),
// every threshold it writes under both #1231 spellings (#4253) — canonical
// key → the two spellings as written, the kept line's first. For the
// tenant-api write gate, which refuses such a pair only where the write
// puts it there.
func TwinSpelledThresholds(section map[string]ScheduledValue) map[string][2]string {
	var out map[string][2]string
	for k, sv := range section {
		if sv.fold == nil || sv.fold.folded == "" {
			continue
		}
		if out == nil {
			out = map[string][2]string{}
		}
		out[k] = [2]string{sv.writtenAs(k), sv.fold.folded}
	}
	return out
}

// TenantAsWritten is one tenant's decoded map (canonical keys, #4253) keyed
// as the layers wrote its lines: each line under its own spelling, and the
// line the decode folded away beside another spelling's back beside it — the
// map the decode read, for the readers that name a tenant's lines (da-guard's
// keys, unserved values, disable effects, schedules). m itself when no line
// was folded.
func TenantAsWritten(m map[string]ScheduledValue) map[string]ScheduledValue {
	folded := false
	for _, sv := range m {
		if sv.fold != nil {
			folded = true
			break
		}
	}
	if !folded {
		return m
	}
	out := make(map[string]ScheduledValue, len(m)+1)
	for k, sv := range m {
		if sv.fold == nil {
			out[k] = sv
			continue
		}
		out[sv.writtenAs(k)] = sv
		if sv.fold.other != nil {
			out[sv.fold.folded] = *sv.fold.other
		}
	}
	return out
}

// writesRetired reports whether sv's line, or the line the decode folded
// away beside it, is written in a retired spelling (#4253) — one retired key
// the tenant writes either way.
func writesRetired(sv ScheduledValue) bool {
	if sv.fold == nil {
		return false
	}
	if sv.fold.written != "" {
		return true
	}
	_, isAlias := canonicalKeyFor(sv.fold.folded)
	return isAlias
}

// WrittenSpellingOf is the spelling the tenant wrote its line of threshold
// canon in, overrides being its decoded map (keyed canonically, #4253): the
// retired one where that is the line the tenant layer kept. canon itself
// when the tenant writes no line of it.
func WrittenSpellingOf(overrides map[string]ScheduledValue, canon string) string {
	if sv, ok := overrides[canon]; ok {
		return sv.writtenAs(canon)
	}
	return canon
}

// writtenAs is the spelling sv's line was written in, key being the
// (canonical) key it is held under.
func (sv ScheduledValue) writtenAs(key string) string {
	if sv.fold != nil && sv.fold.written != "" {
		return sv.fold.written
	}
	return key
}

// mergeRootFold carries partial's fold record of root key k into merged, as
// the merge carries the value: the later file's line replaces the earlier
// one's, and with it what was folded beside it.
func mergeRootFold(merged *ThresholdConfig, partial ThresholdConfig, k string) {
	f, folded := partial.rootFolds[k]
	if !folded {
		delete(merged.rootFolds, k)
		return
	}
	if merged.rootFolds == nil {
		merged.rootFolds = map[string]spellingFold{}
	}
	merged.rootFolds[k] = f
}

// legacySpellingFor is the inverse of canonicalKeyFor over the same shapes:
// given a canonical-spelled key, it returns the deprecated spelling (if the
// key's head is an alias target, exactly or with `_critical`). Used by
// hasAliasEquivalent.
func legacySpellingFor(key string) (string, bool) {
	head, rest := splitKeyHead(key)
	if legacy, ok := aliasHead(head, legacyKeyByCanonical); ok {
		return legacy + rest, true
	}
	return "", false
}

// splitKeyHead splits a key at its first `{` (a dimensional key's label
// part, kept as written); a key with none, or starting with one, is all head.
func splitKeyHead(key string) (head, rest string) {
	if i := strings.IndexByte(key, '{'); i > 0 {
		return key[:i], key[i:]
	}
	return key, ""
}

// aliasHead maps a key head through table, exactly or with its `_critical`
// suffix carried over — the two shapes, one rule, for both directions.
func aliasHead(head string, table map[string]string) (string, bool) {
	if mapped, ok := table[head]; ok {
		return mapped, true
	}
	if base, found := strings.CutSuffix(head, criticalSuffix); found {
		if mapped, ok := table[base]; ok {
			return mapped + criticalSuffix, true
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
// inert member. Nothing validates the list's CONTENTS yet — the membership
// matrix, asserted on the shared key verdict, records those rows as known gaps
// rather than pretending they are covered
// (tests/shared/optional_overrides_membership_matrix.json).
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
	for k, sv := range overrides {
		// #4253: the decode folded a retired spelling into its canonical
		// key; the line keeps what it was written as (valueFold).
		if _, isAlias := canonicalKeyFor(k); isAlias || writesRetired(sv) {
			deprecated++
		}
	}
	if deprecated == 0 {
		return overrides, 0
	}
	return canonicalView(overrides), deprecated
}

// canonicalView is canonicalizeOverrides' map half over any value type: the
// input with every deprecated spelling moved onto its canonical key, the
// canonical spelling winning when both are present. The input is never
// mutated; a map without a deprecated key is returned as is.
//
// Generic so the walker plane's profile expansion (profile_overlay.go, over
// raw YAML values) canonicalizes a profile with THIS rule rather than a copy
// of it — ApplyProfiles (typed, /metrics) and the walker (untyped,
// /effective) must pick the same keys (#2117).
func canonicalView[V any](m map[string]V) map[string]V {
	clean := true
	for k := range m {
		if _, isAlias := canonicalKeyFor(k); isAlias {
			clean = false
			break
		}
	}
	if clean {
		return m
	}
	out := make(map[string]V, len(m))
	for k, v := range m {
		canon, isAlias := canonicalKeyFor(k)
		if !isAlias {
			out[k] = v
			continue
		}
		if _, canonPresent := m[canon]; canonPresent {
			continue // canonical wins; deprecated duplicate ignored
		}
		out[canon] = v
	}
	return out
}

// appendWithLegacyTwin appends row and, when canonicalKey is the target of a
// deprecated alias still inside the transition window (#1231), a second row
// carrying the legacy metric identity — same tenant/value/severity, with
// Component/Metric derived from the legacy key. Emitting the twin at the
// resolve layer (not the collector) keeps the cardinality guard counting it,
// and /simulate + GET /{id}/effective automatically consistent.
//
// canonicalKey is the row's BASE key (no `_critical` suffix, no `{labels}`):
// it drives the twin, which is keyed on the base. sink (nil on every public
// path) is told, for each row appended here, the tenant-config key the row
// serves: servedKey, the whole canonical key the caller resolved (#2115). The
// twin is reported under the same servedKey — it is that key's value served a
// second time under the old metric name, not a key of its own. sink is also
// told the metric key each row's component / metric labels come from (#2750):
// canonicalKey for row — every caller parsed row's labels from it — and
// legacyKey for the twin, and which of the two rows is the twin.
func appendWithLegacyTwin(rows []ResolvedThreshold, canonicalKey string, row ResolvedThreshold, sink rowSink, servedKey string) []ResolvedThreshold {
	rows = append(rows, row)
	if sink != nil {
		sink(servedKey, canonicalKey, false, row)
	}
	if legacyKey, ok := legacyKeyByCanonical[canonicalKey]; ok {
		twin := row
		twin.Component, twin.Metric = parseMetricKey(legacyKey)
		// Mark the twin with its canonical base key so truncationSortKey can
		// order it right behind the row it shadows (#1231 F3) — the twin must
		// never survive a cardinality cut that its canonical row did not.
		twin.legacyTwinOf = canonicalKey
		rows = append(rows, twin)
		if sink != nil {
			sink(servedKey, legacyKey, true, twin)
		}
	}
	return rows
}

// aliasBases is every base key on either side of the alias table (legacy
// and canonical), for touchesAlias' one-lookup reject.
var aliasBases = func() map[string]struct{} {
	m := make(map[string]struct{}, 2*len(deprecatedKeyAliases))
	for legacy, canon := range deprecatedKeyAliases {
		m[legacy] = struct{}{}
		m[canon] = struct{}{}
	}
	return m
}()

// touchesAlias reports whether key could have another spelling: its head
// (splitKeyHead), exactly or without a `_critical` suffix, is on either side
// of the alias table. It is exactly the union of the shapes canonicalKeyFor
// and legacySpellingFor recognise, so a false answer means otherSpellings is
// empty; it exists because the per-tenant merge asks this for every key and
// almost every key is not aliased (#2420 bench gate: MergePartialConfigs_1000
// was +29% time without it).
func touchesAlias(key string) bool {
	head, _ := splitKeyHead(key)
	if _, ok := aliasBases[head]; ok {
		return true
	}
	if base, found := strings.CutSuffix(head, criticalSuffix); found {
		_, ok := aliasBases[base]
		return ok
	}
	return false
}

// otherSpellings is every spelling of key's threshold other than key itself
// — its canonical form and the legacy form of that canonical, across the
// shapes canonicalKeyFor handles. Empty for a key no alias touches
// (every reserved `_` key, every key outside the table).
//
// The result is a slice of *buf, which the caller declares on its stack
// (`var buf [2]string`): a key has at most two other spellings, and
// returning a fresh slice cost one heap allocation per aliased key on the
// per-tenant merge path — measured on #2420's bench gate as +1000
// allocs/op on MergePartialConfigs_1000 (every bench tenant writes the
// canonical `mysql_threads_running`). Callers only range over the result,
// so buf stays on the stack.
func otherSpellings(key string, buf *[2]string) []string {
	if !touchesAlias(key) {
		return buf[:0]
	}
	canon, _ := canonicalKeyFor(key)
	n := 0
	if canon != key {
		buf[n] = canon
		n++
	}
	if legacy, ok := legacySpellingFor(canon); ok && legacy != key {
		buf[n] = legacy
		n++
	}
	return buf[:n]
}

// overlayAcrossSpellings writes src over dst key by key, where "key" means
// the THRESHOLD, not its spelling (#2368): a key src writes also removes
// from dst every other spelling of the same threshold that src does not
// write itself. It is the per-key "later layer wins" of every plane that
// stacks a tenant's layers — the flat merge (mergePartialInto, the
// fast-path reclaim; both through overlayTenantLayer, which adds the
// per-key `_metadata` rule), the walker (overlayTenant) and the tenant-api core
// (supplyFor) — so a tenant file writing `mysql_cpu` beats a platform
// `tenants:` entry writing `mysql_threads_running`.
//
// ⛔ WITHOUT THE REMOVAL THE EARLIER LAYER WON. Both spellings stayed in
// dst, and resolve's canonical-wins dedup (canonicalView) then served
// whichever layer happened to use the canonical spelling — measured on
// /metrics: the platform's 70 over the tenant's own 90, the tenant's key
// listed as unserved.
//
// ⚠️ A spelling src writes itself is never removed: when ONE layer carries
// both spellings, the canonical-wins dedup inside that layer still decides,
// as it always has (and ValidateTenantKeys says so). Removing it would make
// the answer depend on map iteration order.
func overlayAcrossSpellings[V any](dst, src map[string]V) {
	if len(dst) == 0 {
		// Nothing earlier to displace — the common case on the per-tenant
		// merge (a tenant declared by its own file alone), kept to a plain
		// copy (#2420 bench gate).
		for k, v := range src {
			dst[k] = v
		}
		return
	}
	var buf [2]string
	for k, v := range src {
		for _, s := range otherSpellings(k, &buf) {
			if _, own := src[s]; !own {
				delete(dst, s)
			}
		}
		dst[k] = v
	}
}

// JudgeableTenantOverrides is TenantOverridesRaw without every threshold the
// tenant's block writes twice (#2368, #4253): deleting the line kept serves
// the other one's value, which no inherited value can stand for — so the
// redundant-override check judges none of them.
func (ec *EffectiveConfig) JudgeableTenantOverrides() map[string]any {
	if len(ec.tenantTwins) == 0 {
		return ec.TenantOverridesRaw
	}
	out := make(map[string]any, len(ec.TenantOverridesRaw))
	for k, v := range ec.TenantOverridesRaw {
		if _, twice := ec.tenantTwins[k]; !twice {
			out[k] = v
		}
	}
	return out
}

// tenantDups is the tenant layer's dups (keySpellings), nil when none.
func tenantDups(s *keySpellings) map[string][]string {
	if s == nil {
		return nil
	}
	return s.dups
}

// hasAliasEquivalent reports whether overrides already contains key under ANY
// spelling: the key itself, its canonical form, or the legacy form of that
// canonical. The profile fill-in (profileFill, shared by ApplyProfiles and the
// walker plane) uses it so a profile value never displaces a tenant's own
// setting that is merely spelled with the other name of the same threshold
// (#1231 transition window) — tenant beats profile across spellings. Generic
// over the value type so ScheduledValue maps and raw YAML maps ask the same
// question.
func hasAliasEquivalent[V any](overrides map[string]V, key string) bool {
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
