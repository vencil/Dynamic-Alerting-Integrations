package main

// Subtree defaults on the OUTPUT plane (#1521, second half).
//
// Making the flat scanner recurse put nested tenants into `GetConfig()`, so
// their alerts can fire at all. It did NOT make them fire at the right number.
// `ThresholdConfig.Defaults` is one global map with no subtree scope, so a
// nested tenant that declares no override of its own was emitted at the ROOT
// default while `/effective` — and `diagnose --show-inheritance`, which #1447
// calls the only way out of "config looks right but the alert never fires" —
// reported the subtree's. Measured on a two-level tree: `/effective` said 60,
// the series read 50.
//
// ⛔ That residual is worse than the absence it replaced in one specific way:
// the tenant is now present everywhere, so the divergence audit (#1526) sees a
// reconciled tree and says nothing. Silence plus a wrong number.
//
// ⛔ WHY AN OVERLAY AND NOT A SCOPED `Defaults`. Giving `Defaults` subtree
// scope means a second resolution order inside the collector's hot path, which
// runs on every scrape. The chain is already computed once per reload and
// cached (`hierarchy.graph.TenantDefaults` + `hierarchy.parsedDefaults`), so
// materialising it into the tenant's own map at COMMIT time keeps the scrape
// path exactly as it is. This is the same shape as `ApplyProfiles`.
//
// ⚠️ Provenance is not encoded in the emitted series, checked rather than
// assumed: `user_threshold` carries tenant / metric / severity / component,
// and `component` is derived from the metric key's prefix
// (`pkg/config/parse.go`), not from whether the value was authored or
// inherited. So an inherited value written here is indistinguishable from a
// root default in the output — which is the point — and nothing downstream
// reads it as "this tenant customised the key".

import (
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/config"

	"gopkg.in/yaml.v3"
)

// applySubtreeDefaults writes each tenant's inherited SUBTREE defaults into its
// own override map, for keys the tenant has not set itself, and declares any
// key the root does not already carry so the collector can emit it. Returns the
// number of (tenant, key) pairs filled in — 0 for a flat tree, which is what
// makes it free for deployments that never nest.
//
// ⛔ ROOT DEFAULTS ARE SKIPPED BY PATH, NOT BY INDEX. The earlier version
// dropped `chain[0]` on the assumption that it is always the conf.d root's
// `_defaults.yaml`. It is not: `collectDefaultsChain` appends only the levels
// that actually HAVE a defaults file, so on a tree whose root carries none,
// `chain[0]` is the shallowest SUBTREE file — and skipping it dropped every
// key that level introduced. Measured on root(none) → finance(60) →
// finance/us(70): `/effective` reported both keys, the series reported
// neither. Comparing the file's directory against the scan root answers the
// real question ("is this the global defaults file?") instead of a proxy for
// it. (#1569 blind review.)
//
// ⛔ Root defaults are still skipped, and for the original reason: they are
// already in `cfg.Defaults` and already applied to every tenant, so writing
// them per-tenant as well would turn every tenant's map into a full copy of
// the platform surface — O(tenants × keys) memory for no change in behaviour.
//
// ⛔ A KEY THE TENANT ALREADY HAS IS NEVER TOUCHED, including `"disable"`.
// The tenant's own file wins over anything it inherits; that is the whole
// inheritance model, and `disable` in particular must survive — silently
// re-enabling a threshold a tenant switched off would be the worst possible
// failure mode for this function.
func applySubtreeDefaults(
	cfg *ThresholdConfig,
	root string,
	tenantDefaults map[string][]string,
	parsed map[string]map[string]any,
) (int, map[string][]string) {
	if cfg == nil || len(cfg.Tenants) == 0 || len(tenantDefaults) == 0 {
		return 0, nil
	}
	// ⛔ ABSOLUTE, because the chain is. `scanDirHierarchical` stores every
	// defaults path under `filepath.Abs(rootPath)` + `Clean`, while `m.path`
	// is whatever `-config-dir` was given — frequently relative. Comparing a
	// relative root against an absolute chain entry never matches, so EVERY
	// defaults file looked like a subtree file, including the root's.
	// Measured on the repo's own golden fixtures: three trees with no
	// subdirectory at all had their ROOT defaults keys written into tenant
	// maps and declared (`_metadata`, `alert_group`, `threshold`).
	// Mirrors the hierarchical scanner's own derivation exactly, including
	// its choice NOT to resolve symlinks, so the two cannot drift.
	rootDir := absScanRoot(root)
	// ⛔ KEYS THIS OVERLAY CANNOT DELIVER, per tenant, reported rather than
	// forced through. See the block above `unreachableKeys` for why writing
	// them anyway was worse than not writing them.
	unreachable := map[string]map[string]struct{}{}

	filled := 0
	for tenantID, overrides := range cfg.Tenants {
		// ⛔ WHICH KEYS THIS OVERLAY WROTE, tracked separately from the tenant's
		// own map — because after the first subtree file writes a key, asking
		// `overrides[key]` again cannot tell "the tenant authored this" from
		// "I put it there one level ago".
		//
		// `chain` is root-first (`EffectiveConfig.DefaultsChain`: "L0→Ln
		// defaults file paths (root first)"), so this loop walks SHALLOWEST to
		// deepest and the deeper file must win. Conflating the two made the
		// shallowest subtree win instead — measured on
		// root 50 → finance 60 → finance/us-east 70: `/effective` said 70 and
		// the series said 60, which is this ticket's own defect reintroduced
		// one directory deeper. The single-level tests could not see it.
		// (CodeRabbit, #1569.)
		var inherited map[string]struct{}
		for _, defaultsPath := range tenantDefaults[tenantID] {
			if filepath.Dir(filepath.Clean(defaultsPath)) == rootDir {
				continue // the global defaults file — already in cfg.Defaults
			}
			for key, raw := range parsed[defaultsPath] {
				if _, present := overrides[key]; present {
					if _, mine := inherited[key]; !mine {
						continue // the tenant authored it — never touched
					}
				}
				value, ok := scheduledValueFromRaw(raw)
				if !ok || !isThresholdShaped(value) {
					continue
				}
				// ⛔ A KEY NO EMITTER ITERATES IS NOT DELIVERED BY WRITING IT.
				// `resolveBaseRows` walks `cfg.Defaults`; `resolveDeclaredRows`
				// walks `cfg.OptionalOverrides`. A key that appears only in a
				// subtree `_defaults.yaml` is in neither, because nested `_`
				// files are deliberately excluded from the merged config so
				// they cannot re-price the whole tree. Writing it into the
				// tenant's map put a value where nothing reads it AND made
				// `ValidateTenantKeys` log `unknown key` on every commit.
				//
				// ⛔ AND THE EARLIER FIX FOR THAT WAS WORSE. Declaring such
				// keys in `cfg.OptionalOverrides` did make them emit, but that
				// field is a flat global list derived, in that design, from
				// whichever tenants happened to leave the key unset — four
				// separate defects, each measured: a `default_foo`/`foo` pair
				// produced a duplicate label set that fails the WHOLE
				// Prometheus `Gather` (every family in /metrics, not one); an
				// unrelated tenant editing its own file deleted another
				// tenant's live series; the subtree's only tenant setting the
				// key itself meant the key was never declared, so its OWN
				// value stopped emitting; and an `expires:` in a subtree
				// defaults file was charged as a validation error to every
				// tenant under it, blocking their writes.
				//
				// Delivering these keys needs per-subtree scope in
				// `ThresholdConfig`, which is #1568. Until then the honest
				// state is "not delivered, and LOUD about it" — recorded here
				// and reported by the divergence audit. The one thing this
				// must never be again is silent. (#1569 blind review.)
				if !keyCanReachTheOutputPlane(cfg, key, value) {
					if unreachable[tenantID] == nil {
						unreachable[tenantID] = map[string]struct{}{}
					}
					unreachable[tenantID][key] = struct{}{}
					continue
				}
				if overrides == nil {
					overrides = map[string]ScheduledValue{}
					cfg.Tenants[tenantID] = overrides
				}
				if inherited == nil {
					inherited = map[string]struct{}{}
				}
				if _, mine := inherited[key]; !mine {
					filled++ // count keys filled, not times overwritten
				}
				inherited[key] = struct{}{}
				overrides[key] = value
			}
		}
	}
	return filled, unreachableKeys(unreachable)
}

// keyCanReachTheOutputPlane reports whether SOMETHING downstream will iterate
// this key if the overlay writes it into a tenant's override map.
//
// ⛔ AN EARLIER VERSION OF THIS CHECK WAS `cfg.Defaults ∪ cfg.OptionalOverrides`
// AND THAT IS TOO STRICT — measured, three separate shapes were dropped that a
// tenant writing the identical key in its OWN file gets emitted:
//
//	inherited `mysql_connections{env="prod"}: 70`  → no series; the same key
//	    authored by a tenant emitted `env=prod … = 70`
//	inherited `mysql_connections_critical: 95`     → no series; authored emitted
//	    `severity=critical … = 95`
//	inherited `_state_maintenance: "disable"`      → the tenant's maintenance
//	    filter came back ON (`ResolveStateFilters` = [{t1 maintenance warning}])
//	    and a legitimate tree got a divergence ERROR
//
// The reason is that those three do not go through the declared surface at all:
// `resolveDimensionalRows`, `resolveCriticalRows` and `ResolveStateFiltersAt`
// each iterate the tenant's override map DIRECTLY. Refusing them bought
// nothing and lost behaviour — and for `_state_` it lost a SAFETY setting,
// silently re-enabling something a subtree had switched off.
//
// So the refusal is now narrowed to exactly the shape it was built for: a
// plain base key that only `resolveBaseRows` / `resolveDeclaredRows` could
// serve, and that neither of them will find. (#1569 blind review.)
func keyCanReachTheOutputPlane(cfg *ThresholdConfig, key string, sv ScheduledValue) bool {
	if keyBypassesTheDeclaredSurface(cfg, key, sv) {
		return true
	}
	if declaredAnywhere(cfg, key) {
		return true
	}
	// ⛔ CANONICALIZE BEFORE GIVING UP. `ResolveAtWithStats` runs
	// `canonicalizeDefaults`/`canonicalizeOverrides` before any row is built,
	// so a subtree key written with a retired spelling emits under its
	// canonical name. Measured: with `mysql_threads_running` as a root default
	// and the retired spelling carrying 42 inherited from a subtree, a tenant authoring
	// `mysql_cpu` emitted 42 on both the canonical and the legacy twin while
	// the inheritor was refused and stayed at the root's 80.
	if canon, ok := config.CanonicalKeyFor(key); ok {
		return declaredAnywhere(cfg, canon)
	}
	return false
}

// keyBypassesTheDeclaredSurface reports whether this key is served by a
// resolver that reads the tenant's override map directly.
//
// ⛔ THE ARMS MIRROR EACH RESOLVER'S ENTRY CONDITION, NOT ITS KEY NAMING.
// A prefix is only safe to bypass on when the resolver ALSO keys off that
// prefix; where it keys off a declared set instead, this asks the set. Getting
// that wrong is #1339's shape one directory down — two mechanisms enumerating
// one key space with different predicates — and it is what put a reader-less
// key into a tenant map with nothing reporting it.
//
// ⚠️ `_critical` and dimensional keys stay unconditional even though both
// resolvers have a further condition (`defaults[base]` must exist / the label
// set must parse). Both WARN on their own when it fails, so the outcome is
// loud without this gate repeating it — and refusing them here would drop the
// common case where the base IS a root default. Measured: a `_critical` with
// no base default produces two WARN lines, an unparseable dimensional key
// two more; `_state_<undeclared>` and `_routing_bogus` produced none, which
// is why only those two moved.
func keyBypassesTheDeclaredSurface(cfg *ThresholdConfig, key string, sv ScheduledValue) bool {
	// ⛔ A KEY CAN MATCH TWO ARMS, AND A `switch` TAKES THE FIRST.
	// `_state_bogus_critical` is `_critical`-shaped, `_state_bogus{env="x"}`
	// is dimensional-shaped: both matched an unconditional arm and never
	// reached the `_state_` arm's consumer lookup below, so the fix that arm
	// makes was silently unreachable for them. Measured on four shapes
	// (`_state_X_critical`, `_state_X{…}`, `_routing_X{…}`, `_silent_X{…}`),
	// each written into the tenant map with no reader, no report and no WARN.
	// Found by an adversarial reviewer of the arm-order-blind first version of
	// this predicate — which is itself the lesson: the shapes a table misses
	// are the ones where two of its rows overlap.
	//
	// The exclusions below are not invented here. They are copied from what
	// `resolveDimensionalRows` and `resolveCriticalRows` THEMSELVES skip, and
	// that is the whole point: this predicate models those two functions, so
	// where they refuse a key it must not claim they will read it.
	switch {
	case strings.Contains(key, "{") && !reservedShapeWins(key): // resolveDimensionalRows
		return true
	case strings.HasSuffix(key, criticalKeySuffix) &&
		!strings.HasPrefix(key, "_state_") &&
		!strings.HasPrefix(key, "_silent_"): // resolveCriticalRows
		return true
	case strings.HasPrefix(key, "_state_"):
		// ⛔ ASK THE CONSUMER, DO NOT PATTERN-MATCH ITS NAME.
		// `ResolveStateFiltersAt` iterates `c.StateFilters` and looks up
		// `"_state_"+filterName` in each tenant's map — it never walks the
		// tenant's keys. So a `_state_` key naming a filter the platform does
		// not declare is read by nobody, and `_state_` being a valid reserved
		// PREFIX means `ValidateTenantKeys` does not warn either. Bypassing on
		// the prefix wrote such a key into the tenant map with no reader and no
		// report — the exact "silence plus a wrong number" this gate exists to
		// prevent, one shape narrower. Measured on a tree whose root declares
		// no `state_filters:` at all: `_state_no_such_filter: disable` landed
		// in the tenant map, changed no resolver output, and appeared in
		// neither the divergence report nor any WARN. (#1569 sweep B-1.)
		if _, declared := cfg.StateFilters[strings.TrimPrefix(key, "_state_")]; !declared {
			return false
		}
		// ⛔ AND THE CONSUMER READS `Default`, NOT `ResolveValue(now)`.
		// `ResolveStateFiltersAt` and `IsMaintenanceActive` both take
		// `sv.Default` verbatim, so a schedule's windows are invisible to
		// them — while `isThresholdShaped` accepts a schedule and
		// `/effective` renders it. Measured: a subtree declaring
		// `_state_maintenance` as `default: enable` with a window saying
		// `disable` resolved to "disable" on the config plane and left the
		// filter ACTIVE on the collector plane. An active maintenance filter
		// INHIBITS the tenant's alerts, so this direction loses real pages.
		// Refusing it makes the gap loud instead. (#1569 blind review.)
		return len(sv.Overrides) == 0
	case key == "_silent_mode", // ResolveSilentModesAt
		key == "_severity_dedup": // ResolveSeverityDedup
		return true
	}
	// ⛔ EVERY OTHER RESERVED KEY IS DELIBERATELY ABSENT, AND THEY DO REACH
	// HERE. An earlier version of this comment claimed `_routing` /
	// `_routing_profile` / `_metadata` / `_custom_alerts` / `_namespaces` /
	// `_profile` "need a mapping, a list or a name — none of which survives
	// `isThresholdShaped`, so this gate never sees them". Measured false for
	// all six: `_profile: disable` passes through `config.IsDisabled` and a
	// numeric passes through `ParseFloat`, so each arrives here and is
	// refused-and-named. That outcome is right — none of those keys does
	// anything useful with a threshold-shaped value — but the reasoning was
	// not, and a wrong reason is what the next person edits against.
	// (#1569 blind review.)
	//
	// What the old
	// `HasPrefix(key, "_routing")` arm DID admit was junk under that prefix:
	// `_routing_bogus: 5` is threshold-shaped, `ResolveRouting` reads only the
	// exact key `_routing`, and `_routing` is a valid reserved prefix so
	// nothing warns. Same silent shape as `_state_` above; both were found by
	// crossing every resolver's entry condition against every key shape this
	// overlay can write, rather than by re-reading the predicate.
	//
	// The mirror-image case is `_silent_bogus`: also unreadable, but
	// `_silent_` is NOT a reserved prefix, so `ValidateTenantKeys` already
	// WARNs "unknown reserved key". Dropping it from the bypass makes it
	// refused-and-reported too, which is the same verdict said twice — not a
	// regression, just no longer relying on a warning from another subsystem.
	return false
}

// criticalKeySuffix mirrors pkg/config's unexported `criticalSuffix`.
const criticalKeySuffix = "_critical"

// declaredAnywhere reports whether the platform recognises this exact key on
// either surface `resolveBaseRows` / `resolveDeclaredRows` iterate.
// reservedShapeWins mirrors the reserved-key exclusions at the top of
// `resolveDimensionalRows`, verbatim. Kept as its own function so a change
// there is a one-line change here rather than a condition to re-derive.
func reservedShapeWins(key string) bool {
	return strings.HasPrefix(key, "_state_") ||
		strings.HasPrefix(key, "_silent_") ||
		key == "_severity_dedup" ||
		strings.HasPrefix(key, "_routing")
}

func declaredAnywhere(cfg *ThresholdConfig, key string) bool {
	if _, global := cfg.Defaults[key]; global {
		return true
	}
	for _, d := range cfg.OptionalOverrides {
		if d == key {
			return true
		}
	}
	// ⛔ THE ALIAS RELATION IS SYMMETRIC AND THIS ONLY WALKED ONE WAY.
	// `keyCanReachTheOutputPlane` canonicalizes the SUBTREE's key before
	// giving up, which covers "the subtree still uses the retired spelling".
	// It does not cover the other, more common transition state: the ROOT
	// still uses the retired spelling and the subtree has already moved to
	// the canonical one — the very spelling the rename NOTICE tells authors
	// to switch to. `resolveBaseRows` walks `canonicalizeDefaults(c.Defaults)`,
	// so the root's legacy default is already canonical by the time rows are
	// built and the subtree's key DOES reach the plane. Measured with
	// the retired spelling as the only root default: a tenant AUTHORING
	// the canonical `mysql_threads_running` at 42 emitted 42, while that key
	// inherited from a subtree was refused and the tenant stayed at 80 —
	// looser than configured, and the divergence report's own remediation
	// ("declare the key in the ROOT _defaults.yaml") was already satisfied,
	// under the other spelling. Its `_critical` twin inherited fine the whole
	// time, so one metric had its critical tier following the subtree and its
	// base tier following the root. (#1569 blind review, sweep B-1 follow-up.)
	//
	// The measurement above used the retired spelling as the tree's only root
	// default, valued 80. Spelled out rather than shown because the #1231 hook
	// reads a live `key: value` pair anywhere in the repo, comments included,
	// and it is right to — the exemption belongs to the one test file that
	// needs the fixture, not to prose.
	if legacy, aliased := config.LegacySpellingFor(key); aliased {
		if _, global := cfg.Defaults[legacy]; global {
			return true
		}
		for _, d := range cfg.OptionalOverrides {
			if d == legacy {
				return true
			}
		}
	}
	return false
}

// unreachableKeys flattens the per-tenant sets into sorted slices.
//
// ⛔ SORTED because this feeds an operator-facing ERROR line and a gauge; Go
// map iteration is random and a diagnostic that reorders itself every reload
// reads as churn rather than as a stable fact.
func unreachableKeys(byTenant map[string]map[string]struct{}) map[string][]string {
	if len(byTenant) == 0 {
		return nil
	}
	out := make(map[string][]string, len(byTenant))
	for tenantID, keys := range byTenant {
		list := make([]string, 0, len(keys))
		for k := range keys {
			list = append(list, k)
		}
		sort.Strings(list)
		out[tenantID] = list
	}
	return out
}

// scheduledValueFromRaw renders one defaults-file value exactly as a tenant
// file's own value would have been parsed: by handing it to
// `ScheduledValue.UnmarshalYAML`.
//
// ⛔ THIS USED TO BE A HAND-WRITTEN TYPE SWITCH over string/int/int64/float64,
// and the switch was the bug. `ScheduledValue` is not a string — it carries
// `Overrides` (time windows) and `Expiry` — so a subtree defaults file using
// the schedule form was rejected by the switch and silently dropped, leaving
// the tenant emitting the ROOT value while `/effective` rendered the schedule.
// Round-tripping through the same unmarshaller the tenant path uses makes the
// two planes agree by construction instead of by a type list someone has to
// remember to extend; it also removes two smaller holes the switch had — a
// YAML integer too large for `int64` decodes to `uint64` and fell through to
// "reject", and floats needed a hand-rolled shortest-round-trip format.
// (#1569 blind review.)
//
// ok=false only for a value that cannot be marshalled or that the tenant path
// would also refuse.
//
// ⚠️ The `raw == nil` guard below is REDUNDANT, stated because this file's
// standard is that a comment names what was measured: removing it changes
// nothing, since the round-trip renders nil as "null" and the shape filter
// rejects that anyway. It stays as an explicit statement of intent for
// `key:` with no value, not because anything depends on it. (#1569.)
func scheduledValueFromRaw(raw any) (ScheduledValue, bool) {
	var sv ScheduledValue
	if raw == nil {
		return sv, false
	}
	// ⛔ SCALAR FAST PATH, and it is a fix rather than an optimisation reflex.
	// The round-trip costs ~8µs per call against ~10ns for this switch —
	// measured, ~700-1000× — and it runs once per (tenant, inherited key), so
	// on the 1000-tenant hierarchical bench it moved
	// `FullDirLoad_Hierarchical_1000` from +29.67% to +34.74% against
	// merge-base. That increment is mine, not the structural
	// two-enumerators cost the PR already discloses.
	//
	// ⛔ ONLY THE THREE TYPES BELOW, because only they were MEASURED to give a
	// byte-identical result to the round-trip. The table run covered
	// `yes`/`no`/`on`/`off`/`null`/`~`/`007`/`0x10`/empty/space-padded strings
	// and MaxInt64/MinInt64.
	//
	// ⛔ float64 IS DELIBERATELY ABSENT: the two disagree there. YAML renders
	// 1e6 as "1e+06" while `strconv.FormatFloat(…,'f',-1,64)` renders
	// "1000000", and at MaxFloat64 the second is 300-odd digits. Both parse
	// back to the same number, but "the overlay writes what the tenant file
	// would have held" is the property this function exists to guarantee, and
	// only the round-trip has it. Floats in a defaults file are rare next to
	// integers, so the slow path costs nothing that matters.
	// `TestTheScalarFastPathMatchesTheRoundTrip` pins all of this.
	switch v := raw.(type) {
	case string:
		sv.Default = v
		return sv, true
	case int:
		sv.Default = strconv.Itoa(v)
		return sv, true
	case int64:
		sv.Default = strconv.FormatInt(v, 10)
		return sv, true
	case bool:
		// ⛔ A YAML BOOL IS NEVER A THRESHOLD, and this branch is load-bearing
		// rather than defensive. The round-trip renders `false` as the STRING
		// "false", and `config.IsDisabled` counts "false" and "off" as disable
		// synonyms — so a plain feature flag in a subtree defaults file
		// (`paging_enabled: false`) passed the threshold-shape filter, entered
		// the tenant's THRESHOLD map, and was appended to the platform's
		// declared surface. Measured: tenant map
		// `map[paging_enabled:false redis_evicted_keys:44]`, declared
		// `[paging_enabled redis_evicted_keys]`, no warning anywhere.
		// A tenant writing the string "false" in its own file is a deliberate
		// disable; a YAML bool in a defaults file is a flag. The types are
		// distinguishable here and nowhere downstream. (#1569 blind review —
		// the earlier version of this function had the branch and this round
		// dropped it.)
		return sv, false
	}
	encoded, err := yaml.Marshal(raw)
	if err != nil {
		return sv, false
	}
	if err := yaml.Unmarshal(encoded, &sv); err != nil {
		return sv, false
	}
	return sv, true
}

// isThresholdShaped reports whether an inherited defaults value is something
// the collector could actually emit as a threshold.
//
// ⛔ THE OVERLAY MUST NOT CARRY ARBITRARY HIERARCHY CONFIG. A conf.d subtree
// `_defaults.yaml` is free to hold keys that are not thresholds at all —
// `region: us-east`, `pages: [b]`, `_metadata: {...}` — and `/effective`
// serves them, because the hierarchical plane is a general config merge. The
// collector plane is not: `ThresholdConfig.Tenants` is a map of THRESHOLDS.
// Copying a non-threshold into it is not merely useless, it is loud in two
// places, measured on the repo's own `full-l0-l3` golden fixture:
//
//   - declared → `resolveDeclaredRows` logs `WARN: invalid declared threshold
//     "us-east" …` — and that runs inside `ResolveAtWithStats`, i.e. ON EVERY
//     SCRAPE, so three junk keys became three WARN lines per scrape;
//   - not declared → `ValidateTenantKeys` logs `WARN: unknown key … not in
//     defaults` on every config commit instead.
//
// Neither is acceptable, and both disappear if the key never enters the map.
// The emitted series count was 0 either way — `resolveDeclaredRows` refuses
// to parse them — so nothing is lost by leaving them where they already work.
//
// ⚠️ LIMIT, STATED: this also excludes `_state_` / `_silent_` / `_routing`
// reserved keys declared in a SUBTREE defaults file, which are resolved from
// their own top-level config sections rather than from a tenant's threshold
// map. Inheriting those through a subtree is a separate feature with its own
// resolution path; it is not silently half-done here.
// ⛔⛔ THE ADMISSION RULE IS DELIBERATELY LOOSER THAN `defaults:`'s OWN TYPE,
// AND THIS IS THE SPEC FOR WHY.
//
// `defaults:` is read by two consumers with two different types, and this
// overlay is the first thing that bridges them:
//
//	collector plane  — `ThresholdConfig.Defaults` is `map[string]float64`.
//	                   A root `_defaults.yaml` whose `defaults:` block holds a
//	                   string, a list or a nested mapping fails to parse and
//	                   the WHOLE FILE is dropped with an ERROR. Measured on all
//	                   three shapes.
//	hierarchy plane  — `parseDefaultsBytes` → `extractDefaultsBlock` reads the
//	                   same block as an arbitrary nested document and ADR-017
//	                   deep-merges it (dict merge, array replace, null-as-delete
//	                   for `_`-reserved keys). This is not a leak: 9 of the
//	                   repo's 17 `_defaults.yaml` files carry exactly that —
//	                   `threshold: {cpu: 70}`, `alert_group: baseline`,
//	                   `receivers: [...]` — and the ADR-017 golden fixtures are
//	                   built on it. (6 more are all-float; 2 have no `defaults:`
//	                   block at all.) Those 9 files live in 6 trees, and
//	                   measured, ALL SIX resolve to `defaults=0 rows=0` on the
//	                   collector plane, by design; the real shipped config
//	                   resolves to `defaults=8 rows=64` with no drops.
//
//	                   ⚠️ TWO EARLIER VERSIONS OF THIS PARAGRAPH WERE WRONG, in
//	                   opposite directions, in a comment about the cost of
//	                   asserting rather than measuring. First it said 12 of 17,
//	                   which no counting rule reaches. The "correction" then
//	                   said 7 of those 9 emit nothing and named
//	                   `opt-out-null-threshold` and `wrapper-siblings` as the
//	                   exceptions — but those two are all-float, i.e. not among
//	                   the 9 at all. Both numbers here are now from a script
//	                   over the repo, not from memory.
//
// ⛔ ADR-017 IS NOW THE AUTHORITY ON THE ABOVE, and it agrees. #1516/#1555
// rewrote it to state the same split independently: only keys inside
// `defaults:` enter `effective`; a value that will not parse as float64
// (mapping / list / string / bool) makes `parsePartialConfig` return ok=false
// and the WHOLE file is dropped; and `_`-prefixed keys are consumed from the
// `tenants:` block, not from `defaults:`. Read
// docs/adr/017-defaults-yaml-inheritance-dual-hash.md §「已知的可達例外」
// before changing anything here — the measurements above were taken before
// that rewrite landed and are kept only because they are what this code was
// built against.
//
// So `isThresholdShaped` is a TYPE COERCION at a plane boundary, and the value
// domain it admits is the DESTINATION's, not the source's: what lands in
// `cfg.Tenants[t][key]` is a `ScheduledValue`, which legitimately holds
// `"disable"`, `"70:critical"` and a schedule — none of which `map[string]float64`
// would accept. Judging the source by the destination's domain is the whole
// point; judging it by `map[string]float64` instead would stop `"disable"`
// inheriting, and ADR-017 §Null names `"disable"` as the sanctioned way for a
// threshold key to opt out of the chain.
//
// ⚠️ THE COST, STATED. Because the admitted domain is wider than the source
// block's declared type, this function can admit key/value pairs that a ROOT
// `_defaults.yaml` could not legally contain — which is why the reachability
// gate below has to adjudicate reserved-key shapes at all. Every "silent shape"
// found by the #1569 sweeps is downstream of this coercion, not of any one
// predicate. Narrowing the coercion is a real option and was considered; it was
// not taken because it would break `"disable"` inheritance and the ADR-017
// semantics above. Anyone revisiting it should start here, not at the gate.
func isThresholdShaped(sv ScheduledValue) bool {
	if thresholdScalar(sv.Default) {
		return true
	}
	// ⛔ A SCHEDULE CAN CARRY ITS VALUE ENTIRELY IN THE WINDOWS. `default: ""`
	// with a populated `overrides:` list is a shape `ScheduledValue.ResolveValue`
	// honours — measured on a tenant-authored control, which emitted 90 inside
	// the window — so judging `Default` alone dropped the inherited copy and
	// left the two planes disagreeing again: `/effective` rendered the whole
	// schedule while the series carried the ROOT's 50. Found independently by
	// two reviewers. (#1569.)
	//
	// ⚠️ An `overrides:`-only mapping with NO `default:` key at all is a
	// different shape and is correctly rejected: `UnmarshalYAML` takes its
	// arbitrary-mapping branch there, so nothing is decoded into `Overrides`
	// and the tenant path resolves it to the platform value too. Measured
	// both ways.
	for _, window := range sv.Overrides {
		if thresholdScalar(window.Value) {
			return true
		}
	}
	return false
}

// thresholdScalar reports whether one rendered value is something
// `resolveBaseRows` could turn into a series.
func thresholdScalar(value string) bool {
	raw := strings.TrimSpace(value)
	if raw == "" {
		return false
	}
	if config.IsDisabled(strings.ToLower(raw)) {
		return true
	}
	// `value:severity` is the inline-severity form resolveBaseRows accepts.
	valuePart := raw
	if idx := strings.Index(raw, ":"); idx >= 0 {
		valuePart = raw[:idx]
	}
	_, err := strconv.ParseFloat(strings.TrimSpace(valuePart), 64)
	return err == nil
}
