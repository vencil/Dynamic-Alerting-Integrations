package config

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
//
// ⛔ A RESERVED KEY A SUBTREE DOES NOT CARRY IS NEVER WRITTEN (#3341):
// subtreeReservedUnsupported — `_state_*`, `_silent_mode`, `_severity_dedup`,
// `_metadata`, … — whatever its value, `disable` and numbers included. Until
// #3341 this overlay applied a `disable` or a number of some of them and
// dropped every other value, so a subtree could switch a state filter off but
// not on; da-guard's subtree_default_reserved_key is now an error, and the
// value belongs in each tenant's own entry.
func applySubtreeDefaults(
	cfg *ThresholdConfig,
	root string,
	tenantDefaults map[string][]string,
	parsed map[string]map[string]any,
) (int, map[string]map[string]ScheduledValue, chainRejections) {
	if cfg == nil || len(cfg.Tenants) == 0 || len(tenantDefaults) == 0 {
		return 0, nil, chainRejections{}
	}
	// ⛔ ABSOLUTE, because the chain is. `scanDirTree` stores every defaults
	// path under the absolutised, cleaned AND symlink-resolved root
	// (`absScanRoot`), while `m.path` is whatever `-config-dir` was given —
	// frequently relative. Comparing a relative root against an absolute
	// chain entry never matches, so EVERY defaults file looked like a
	// subtree file, including the root's. Measured on the repo's own golden
	// fixtures: three trees with no subdirectory at all had their ROOT
	// defaults keys written into tenant maps and declared (`_metadata`,
	// `alert_group`, `threshold`). The SAME derivation the walker uses, so
	// the two cannot drift.
	rootDir := AbsScanRoot(root)
	// ⛔ KEYS THIS OVERLAY CANNOT DELIVER, per tenant, reported rather than
	// forced through. See the block above `unreachableKeys` for why writing
	// them anyway was worse than not writing them. Each key keeps the value
	// of the deepest level that writes it in a threshold shape (the chain is
	// root-first, so a later level overwrites; a level whose value fails
	// scheduledValueFromRaw / isThresholdShaped is skipped above and leaves
	// the shallower value). The value is scheduledValueFromRaw's rendering —
	// normalised (a YAML `1e6` becomes "1e+06"), not the file's text (#1976:
	// `da-guard served-values` lists threshold keys in `unserved`).
	unreachable := map[string]map[string]ScheduledValue{}
	// ⛔ WHICH VALUES THIS OVERLAY REFUSES (#2296): defaults file (absolute)
	// → the keys whose value is not threshold-shaped (refusedKeys), so
	// /effective can say why it shows a value /metrics does not serve
	// (FlatBuild.RejectedChainValues). A property of the file's bytes, read
	// once per file of some tenant's chain (nil: read, nothing refused) —
	// not "refused for some tenant under it", which moved one tenant's
	// verdict with another tenant's file (#2065). The tenants it does not
	// apply to are recorded apart, in tenantSets: tenant → file → the keys
	// of that file it sets itself under any spelling (tenantAuthoredThreshold,
	// asked where the overlay asks it). Rare, so a reload of a tree whose
	// files refuse a key costs nothing per tenant that does not set it.
	var rejected map[string]map[string]bool
	var tenantSets map[string]map[string]map[string]bool

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
			level := parsed[defaultsPath]
			refused, read := rejected[defaultsPath]
			if !read {
				if rejected == nil {
					rejected = map[string]map[string]bool{}
				}
				refused = refusedKeys(level)
				rejected[defaultsPath] = refused
			}
			for key, raw := range level {
				if subtreeReservedUnsupported(key) {
					continue // not carried by a subtree (#3341; subtreeReservedKeys reports it)
				}
				// ⛔ "THE TENANT AUTHORED IT" IS ASKED PER THRESHOLD, NOT PER
				// SPELLING (#2414). A tenant writing the retired spelling has
				// set the same threshold a subtree file names canonically;
				// asking `overrides[key]` alone let the subtree value in beside
				// it, and resolve's canonical-wins dedup then served the
				// subtree's number over the tenant's own.
				if tenantAuthoredThreshold(overrides, inherited, key) {
					if refused[key] {
						if tenantSets == nil {
							tenantSets = map[string]map[string]map[string]bool{}
						}
						if tenantSets[tenantID] == nil {
							tenantSets[tenantID] = map[string]map[string]bool{}
						}
						if tenantSets[tenantID][defaultsPath] == nil {
							tenantSets[tenantID][defaultsPath] = map[string]bool{}
						}
						tenantSets[tenantID][defaultsPath][key] = true
					}
					continue // the tenant authored it — never touched
				}
				value, verdict := subtreeLevelValue(cfg, key, raw)
				if verdict == subtreeRefused {
					continue // in refused (unless null: it fills nothing)
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
				// Delivering these keys would need per-subtree scope in
				// `ThresholdConfig`; #1976 chose to refuse them at
				// validation time instead (da-guard's
				// subtree_default_undeliverable, an error). So the state
				// here stays "not delivered, and LOUD about it" — recorded
				// here and reported by da_config_subtree_undeliverable_tenants
				// (config_subtree_undeliverable.go) for a tree that reaches
				// the exporter without da-guard. The one thing this must
				// never be again is silent. (#1569 blind review.)
				if verdict == subtreeUnreachable {
					if unreachable[tenantID] == nil {
						unreachable[tenantID] = map[string]ScheduledValue{}
					}
					unreachable[tenantID][key] = value
					continue
				}
				if overrides == nil {
					overrides = map[string]ScheduledValue{}
					cfg.Tenants[tenantID] = overrides
				}
				if inherited == nil {
					inherited = map[string]struct{}{}
				}
				// The deeper level wins per threshold too: another spelling a
				// SHALLOWER level handed down is displaced, exactly as the
				// same spelling would be overwritten. A spelling this level
				// writes itself stays (canonical-wins inside one file, as
				// overlayAcrossSpellings rules for one layer).
				var buf [2]string
				for _, s := range otherSpellings(key, &buf) {
					if _, mine := inherited[s]; !mine {
						continue
					}
					if levelWritesSpelling(level, s) {
						continue
					}
					delete(overrides, s)
					delete(inherited, s)
					filled--
				}
				if _, mine := inherited[key]; !mine {
					filled++ // count keys filled, not times overwritten
				}
				inherited[key] = struct{}{}
				// value is already servedValue's (subtreeLevelValue, #3295):
				// a reserved key's window was dropped before any verdict.
				overrides[key] = value
			}
		}
	}
	refusals := chainRejections{byFile: relRejected(rootDir, rejected)}
	for tenantID, files := range tenantSets {
		if refusals.tenantSets == nil {
			refusals.tenantSets = make(map[string]map[string]map[string]bool, len(tenantSets))
		}
		refusals.tenantSets[tenantID] = relRejected(rootDir, files)
	}
	if len(unreachable) == 0 {
		return filled, nil, refusals
	}
	return filled, unreachable, refusals
}

// subtreeLevelVerdict is what applySubtreeDefaults does with one subtree
// level's value of a key, for a tenant that does not set the key itself
// (subtreeLevelValue).
type subtreeLevelVerdict int

const (
	// subtreeNotCarried: a reserved key a subtree does not carry
	// (subtreeReservedUnsupported, #3341) — not applied, whatever the value.
	subtreeNotCarried subtreeLevelVerdict = iota
	// subtreeRefused: not threshold-shaped — not applied (refusedKeys).
	subtreeRefused
	// subtreeUnreachable: threshold-shaped, but no resolver reads the key
	// from the tenant's map — not applied (keyCanReachTheOutputPlane).
	subtreeUnreachable
	// subtreeApplied: written into the tenant's map (as servedValue).
	subtreeApplied
)

// subtreeLevelValue is applySubtreeDefaults' verdict on raw, one subtree
// level's value of key, with the value it applies (subtreeLevelRead). The
// one judgement of "does the overlay apply it": applySubtreeDefaults acts on
// it, and the load's #3269 WARN words a scheduled reserved key by it
// (SubtreeScheduledReserved).
func subtreeLevelValue(cfg *ThresholdConfig, key string, raw any) (ScheduledValue, subtreeLevelVerdict) {
	if subtreeReservedUnsupported(key) {
		return ScheduledValue{}, subtreeNotCarried
	}
	value, written, shaped := subtreeLevelRead(key, raw)
	if !written || !shaped {
		return value, subtreeRefused // or fills nothing: not applied either way
	}
	if !keyCanReachTheOutputPlane(cfg, key) {
		return value, subtreeUnreachable
	}
	return value, subtreeApplied
}

// subtreeLevelRead is raw, one defaults level's value of key, as the
// overlay reads it — servedValue's: a reserved key's time window and
// `expires:` / `reason:` dropped, its `default` alone — with whether the
// level writes the key at all (written) and whether that value is
// threshold-shaped, i.e. one the overlay could hand down (shaped). Every
// "does this level write key" question asks it (subtreeLevelValue,
// refusedKeys, levelWritesSpelling, subtreeReservedKeys).
//
// written is false for a value that writes nothing — the level is as if it
// did not name key: a null; a schedule with a null `default:` and no window
// (nullSchedule, #2708); and a reserved key's schedule whose `default:` is
// null, since its `default` alone is what is read (#3295). A `default: ""`
// is written (an empty string, not a null), as before.
//
// ⛔ STRIP FIRST, THEN JUDGE (#3295). The readers of a reserved key read its
// `default` alone (#3269), so judging the whole schedule let a window decide
// what is handed down although no reader sees it: `_silent_mode: {default:
// warning, overrides: [{value: 90}]}` passed on the window's 90 and silenced
// the tenant's warnings, while the scalar `warning` is refused. Stripped
// first, a windowed reserved key is exactly its `default` written alone at
// the same level. A threshold key is untouched: servedValue returns it
// whole, and its `default: ""` + windows shape still passes (#1569).
func subtreeLevelRead(key string, raw any) (value ScheduledValue, written, shaped bool) {
	if raw == nil || nullSchedule(raw) {
		return value, false, false
	}
	value, ok := scheduledValueFromRaw(raw)
	if !ok {
		return value, true, false
	}
	if scheduledReserved(key, value) {
		// scheduledValueFromRaw renders a null default as "", as it does a
		// written ""; only the raw mapping tells them apart.
		if d, has := raw.(map[string]any)["default"]; has && d == nil {
			return ScheduledValue{}, false, false
		}
	}
	value = servedValue(key, value)
	return value, true, isThresholdShaped(value)
}

// chainRejections is applySubtreeDefaults' account of the subtree values it
// refuses (FlatBuild.RejectedChainValues, FlatBuild.rejectedTenantSets).
type chainRejections struct {
	byFile     map[string]map[string]bool
	tenantSets map[string]map[string]map[string]bool
}

// refusedKeys is the keys of a subtree defaults level whose value
// applySubtreeDefaults refuses as not threshold-shaped (subtreeLevelRead,
// the overlay's own reading), for any tenant that does not set the key
// itself. A value that writes nothing (a null) is not refused: it fills
// nothing; nor is a reserved key a subtree does not carry
// (subtreeReservedUnsupported): no value of it is applied, so the overlay
// does not judge one (subtreeReservedKeys reports it, #3341). nil when none.
func refusedKeys(level map[string]any) map[string]bool {
	var out map[string]bool
	for key, raw := range level {
		if subtreeReservedUnsupported(key) {
			continue
		}
		if _, written, shaped := subtreeLevelRead(key, raw); !written || shaped {
			continue
		}
		if out == nil {
			out = map[string]bool{}
		}
		out[key] = true
	}
	return out
}

// subtreeRelPath is p relative to rootDir, slash-separated; p itself when Rel
// fails. The same rendering as subtreeReservedKeys' file list.
func subtreeRelPath(rootDir, p string) string {
	if r, err := filepath.Rel(rootDir, p); err == nil {
		return filepath.ToSlash(r)
	}
	return p
}

// RenderYAMLFlow renders v as ONE line of YAML (a scalar as itself, a mapping
// as `{a: 1}`), for messages that quote a config value to paste as
// `key: <rendered>`; the paste decodes to v. v is a value decoded from YAML
// (yaml.Node.Encode panics on Go types YAML has no form for, e.g. a chan —
// callers pass decoded values only). ok=false when it does not fit one line
// (or the encode reports an error) — the caller then omits the value rather
// than quote something that would not paste back.
//
// ⛔ ONE LINE (#2388 A r3). yaml.v3 renders a string holding a newline as a
// block scalar (`|` + indented lines); pasted after `key:` inside a tenant
// entry, that made the file fail to parse and the tenant vanish from
// served-values. A string holding a control character or leading/trailing
// whitespace is double-quoted (escapes, one line). yaml.v3 does not wrap long
// lines (measured with a 300-character string), so no other path is kept
// (#2388 A r4: an unreachable `%q` fallback, which would have changed a
// mapping's type, was removed). A `<<` key inside a mapping value would be
// re-read as a merge key on paste: the one shape this does not keep.
func RenderYAMLFlow(v any) (string, bool) {
	var n yaml.Node
	if err := n.Encode(v); err != nil {
		return "", false
	}
	var walk func(*yaml.Node)
	walk = func(x *yaml.Node) {
		switch x.Kind {
		case yaml.MappingNode, yaml.SequenceNode:
			x.Style = yaml.FlowStyle
		case yaml.ScalarNode:
			if x.Tag == "!!str" && needsDoubleQuote(x.Value) {
				x.Style = yaml.DoubleQuotedStyle
			}
		}
		for _, c := range x.Content {
			walk(c)
		}
	}
	walk(&n)
	b, err := yaml.Marshal(&n)
	if err != nil {
		return "", false
	}
	out := strings.TrimSpace(string(b))
	if strings.Contains(out, "\n") {
		return "", false
	}
	return out, true
}

// needsDoubleQuote: s cannot sit in a plain or single-quoted one-line scalar
// unchanged — it holds a control character, or leading/trailing whitespace a
// plain scalar would lose.
func needsDoubleQuote(s string) bool {
	if s != strings.TrimSpace(s) {
		return true
	}
	for _, r := range s {
		if r < 0x20 || r == 0x7f {
			return true
		}
	}
	return false
}

// levelWritesSpelling reports whether one defaults level WRITES spelling s:
// present, not null, and threshold-shaped as the overlay reads it
// (subtreeLevelRead) — the values this overlay would hand down. It is the
// one definition both planes use for "this level wrote that spelling": the
// overlay above (a spelling the same level writes is not displaced) and the
// walker's MergedDefaults fold (noteSpellingWriters).
//
// ⛔ ONE PREDICATE, NOT TWO. The first version asked `level[s]` here and
// skipped only nil in the walker, so a level writing the canonical spelling
// as null beside a legacy value counted as "writes both" on /metrics (the
// shallower canonical value survived and won the canonical-wins dedup) but
// as "writes only the legacy one" in the guard — which then advised
// deleting a tenant key whose removal moved /metrics. (#2414 round 2.)
func levelWritesSpelling(level map[string]any, s string) bool {
	raw, ok := level[s]
	if !ok {
		return false
	}
	_, written, shaped := subtreeLevelRead(s, raw)
	return written && shaped
}

// tenantAuthoredThreshold reports whether the tenant's own map (overrides
// minus what this overlay put there, `inherited`) sets key's threshold under
// ANY spelling — hasAliasEquivalent, with the overlay's own writes excluded.
//
// ⚠️ A threshold the tenant (or a platform entry, or a profile) wrote as
// null is not in overrides at all: ParseConfigFile drops it (#2518,
// null_threshold.go), so presence here already means "wrote a value" and
// the subtree value fills a null exactly as it fills an absent key.
func tenantAuthoredThreshold(overrides map[string]ScheduledValue, inherited map[string]struct{}, key string) bool {
	authored := func(s string) bool {
		if _, present := overrides[s]; !present {
			return false
		}
		_, mine := inherited[s]
		return !mine
	}
	if authored(key) {
		return true
	}
	var buf [2]string
	for _, s := range otherSpellings(key, &buf) {
		if authored(s) {
			return true
		}
	}
	return false
}

// keyCanReachTheOutputPlane reports whether SOMETHING downstream will iterate
// this key if the overlay writes it into a tenant's override map.
//
// ⛔ AN EARLIER VERSION OF THIS CHECK WAS `cfg.Defaults ∪ cfg.OptionalOverrides`
// AND THAT IS TOO STRICT — measured, two separate shapes were dropped that a
// tenant writing the identical key in its OWN file gets emitted:
//
//	inherited `mysql_connections{env="prod"}: 70`  → no series; the same key
//	    authored by a tenant emitted `env=prod … = 70`
//	inherited `mysql_connections_critical: 95`     → no series; authored emitted
//	    `severity=critical … = 95`
//
// The reason is that those do not go through the declared surface at all:
// `resolveDimensionalRows` and `resolveCriticalRows` iterate the tenant's
// override map DIRECTLY. So the refusal is narrowed to exactly the shape it
// was built for: a plain base key that only `resolveBaseRows` /
// `resolveDeclaredRows` could serve, and that neither of them will find.
// (#1569 blind review.)
//
// ⚠️ The overlay asks this only of the keys a subtree carries — thresholds
// and the routing keys (subtreeReservedUnsupported, #3341); every other `_`
// key is skipped before any verdict.
func keyCanReachTheOutputPlane(cfg *ThresholdConfig, key string) bool {
	if keyBypassesTheDeclaredSurface(key) {
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
	if canon, ok := canonicalKeyFor(key); ok {
		return declaredAnywhere(cfg, canon)
	}
	return false
}

// keyBypassesTheDeclaredSurface reports whether this key is served by a
// resolver that reads the tenant's override map directly.
//
// ⛔ THE ARMS MIRROR EACH RESOLVER'S ENTRY CONDITION, NOT ITS KEY NAMING.
// A prefix is only safe to bypass on when the resolver ALSO keys off that
// prefix; where it keys off a declared set instead, it must ask the set.
// Getting that wrong is #1911's shape one directory down — two mechanisms
// enumerating one key space with different predicates.
//
// ⚠️ `_critical` and dimensional keys stay unconditional even though both
// resolvers have a further condition (`defaults[base]` must exist / the label
// set must parse). Both WARN on their own when it fails, so the outcome is
// loud without this gate repeating it — and refusing them here would drop the
// common case where the base IS a root default.
//
// The exclusions below are not invented here. They are copied from what
// `resolveDimensionalRows` and `resolveCriticalRows` THEMSELVES skip: this
// predicate models those two functions, so where they refuse a key it must
// not claim they will read it.
//
// ⛔ NO RESERVED-KEY ARM (#3341). The `_state_<declared filter>`,
// `_silent_mode` and `_severity_dedup` arms answered for keys
// ResolveStateFiltersAt / ResolveSilentModesAt / ResolveSeverityDedup read
// from the tenant's map; a subtree no longer hands any of them down
// (subtreeReservedUnsupported), so no caller asks. A routing key reaches
// here and is refused unless declared: `ResolveRouting` reads only the exact
// key `_routing`, and `_routing` being a valid reserved prefix means
// `ValidateTenantKeys` does not warn about `_routing_bogus: 5` either.
func keyBypassesTheDeclaredSurface(key string) bool {
	switch {
	case strings.Contains(key, "{") && !reservedShapeWins(key): // resolveDimensionalRows
		return true
	case strings.HasSuffix(key, criticalKeySuffix) &&
		!strings.HasPrefix(key, "_state_") &&
		!strings.HasPrefix(key, "_silent_"): // resolveCriticalRows
		return true
	}
	return false
}

// criticalKeySuffix mirrors pkg/config's unexported `criticalSuffix`.
const criticalKeySuffix = "_critical"

// reservedShapeWins mirrors the reserved-key exclusions at the top of
// `resolveDimensionalRows`, verbatim. Kept as its own function so a change
// there is a one-line change here rather than a condition to re-derive.
func reservedShapeWins(key string) bool {
	return strings.HasPrefix(key, "_state_") ||
		strings.HasPrefix(key, "_silent_") ||
		IsRoutingLayerKey(key)
}

// declaredAnywhere reports whether the platform recognises this exact key on
// either surface `resolveBaseRows` / `resolveDeclaredRows` iterate.
func declaredAnywhere(cfg *ThresholdConfig, key string) bool {
	if _, global := cfg.Defaults[key]; global {
		return true
	}
	// ⛔ FOR THIS REACHABILITY TEST, `optional_overrides:` DOES NOT DECLARE A
	// NON-RESERVED `_` KEY. For a threshold key the consumer of that list is
	// resolveDeclaredRows, and its leading-underscore net skips every key
	// starting with `_` before reading the tenant's value. Counting such a
	// listing as declared made this test claim the key reaches the plane when
	// nothing reads it. (ValidateTenantKeys still accepts the listed key; not
	// changed here.) Measured before #2707: root `_foo_x: null` plus
	// `optional_overrides: [_foo_x]`, tenant `_foo_x: 50` — no
	// root_default_null_undeclared, while served-values listed the 50 in
	// `unserved`; the same listing silenced subtree_default_undeliverable and
	// the exporter's ERROR for a subtree `_myth`. A root NUMBER for the key
	// is served (resolveBaseRows), so the defaults arm above is unchanged.
	//
	// ⚠️ RESERVED KEYS ARE NOT GATED — resolveDeclaredRows is not their
	// consumer; they keep the pre-#2707 rule. The only reserved keys the
	// overlay still asks about are the routing keys (subtreeReservedUnsupported,
	// #3341).
	//
	// The gate is on `key`, the spelling resolveDeclaredRows sees: the
	// declared set is canonicalized, so it gates the legacy listing below
	// too; the legacy DEFAULTS arm is not gated.
	listable := optionalOverridesCanDeclare(key)
	for _, d := range cfg.OptionalOverrides {
		if listable && d == key {
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
	// looser than configured, and the undeliverable report's own remediation
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
	if legacy, aliased := legacySpellingFor(key); aliased {
		if _, global := cfg.Defaults[legacy]; global {
			return true
		}
		for _, d := range cfg.OptionalOverrides {
			if listable && d == legacy {
				return true
			}
		}
	}
	return false
}

// optionalOverridesCanDeclare reports whether listing key under
// `optional_overrides:` counts as declaring it for declaredAnywhere: false
// only for a non-reserved key starting with `_`, the threshold keys
// resolveDeclaredRows' leading-underscore net skips. Reserved keys keep
// counting (see the ⛔ in declaredAnywhere). resolveDeclaredRows' other
// skips (a valued default, `{`, `_critical`) hand the key to a sibling
// resolver that declaredAnywhere's defaults arm or
// keyBypassesTheDeclaredSurface already asks, so they are not repeated here.
func optionalOverridesCanDeclare(key string) bool {
	return !strings.HasPrefix(key, "_") || IsReservedKey(key)
}

// undeliverableThresholds is the part of the build's unreachable set that
// #1976 reports to readers outside the exporter (LoadReport.Undeliverable,
// ScopedTenants.Undeliverable — both go through here, so the two cannot
// filter differently): the keys for which the finding's advice — declare the
// key in the root `_defaults.yaml` (or, for a key not starting with `_`, in
// `optional_overrides:`) — is the fix. nil when none is left.
// FlatBuild.Unreachable (the exporter's ERROR and gauge) is not filtered.
//
// Three exclusions, each taken from an existing authority rather than a
// naming rule of its own:
//
// The first two exclusions are subtreeDefaultsRefusedKey, the predicate
// #2388's subtree_default_reserved_key finding reports on (less the routing
// keys, which the route generator reports) — one definition, so a key is
// never in both findings nor in neither:
//
//  1. IsReservedKey(k) — the reserved keys the config model recognises
//     (`_metadata`, `_profile`, `_state_*`, `_routing*`, …). Declaring one in
//     the root `defaults:` is not a fix: it produces a meaningless or
//     conflicting threshold row (measured: a root `_namespaces: 1` grows a
//     user_threshold row, a root `_metadata` makes served-values fail), and
//     declaring it in `optional_overrides:` silences the verdict while the
//     subtree value still does not take effect (measured, before #3295,
//     with a scheduled `_state_maintenance`). Some were even listed although
//     a deeper level delivers the key (before #3295, `_state_<f>` as a
//     schedule at one level, a scalar below). Reported by
//     subtreeReservedKeys instead (#2388).
//  2. baseRowsSkipKey(k) — the keys resolveBaseRows itself never turns into a
//     row: every `_` key (#3045), whether declared or not — `_silent_bogus`,
//     `_myth2` or `_namespaces` declared at the root still serves nothing, so
//     the advice cannot help. Also reported by
//     subtreeReservedKeys (#2388).
//  3. disabledEverywhere(v) — a key the subtree switches OFF (isDisabled, the
//     resolver's own disable test, on the default and every window). It is
//     undelivered, but "that alert can never fire" is what the subtree asked
//     for; declaring it at the root would instead start serving it for every
//     tenant outside the subtree (measured). Not #2388's — just not reported.
//
// ⚠️ A `_` key the registry does not list (`_myth: 5`) was a threshold to
// the exporter before #3045 — a root `_myth` served a user_threshold row —
// and was reported here. It now serves no row in any layer, so exclusion 2
// leaves it out. It still stays in FlatBuild.Unreachable (the exporter's
// ERROR and gauge) when the root does not declare it: resolveDeclaredRows
// skips every `_` key, and declaredAnywhere asks that same skip
// (optionalOverridesCanDeclare, #2707).
func undeliverableThresholds(byTenant map[string]map[string]ScheduledValue) map[string]map[string]ScheduledValue {
	var out map[string]map[string]ScheduledValue
	for tenantID, keys := range byTenant {
		for k, v := range keys {
			if subtreeDefaultsRefusedKey(k) || disabledEverywhere(v) {
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
	return out
}

// subtreeDefaultsRefusedKey reports whether k is a key a subtree
// `_defaults.yaml` is not meant to carry in its defaults (#2388): a reserved
// key (IsReservedKey) or one resolveBaseRows never turns into a row
// (baseRowsSkipKey). The one definition both reports use: #1976's
// undeliverableThresholds leaves these keys out (declaring them at the root
// is not a fix), and subtreeReservedKeys reports them, less the routing keys
// the route generator reports (subtreeReservedUnsupported).
func subtreeDefaultsRefusedKey(k string) bool {
	return IsReservedKey(k) || baseRowsSkipKey(k)
}

// subtreeReservedUnsupported reports whether a subtree `_defaults.yaml` does
// not carry k (#2388, #3341): subtreeDefaultsRefusedKey less the routing keys
// (IsRoutingKey: `_routing`, `_routing_<…>`), which the route generator's
// checks own (see subtreeReservedKeys). The one definition the overlay
// (applySubtreeDefaults: never written), the walker's reported view
// (effectiveView: never laid) and da-guard's subtree_default_reserved_key
// (subtreeReservedKeys: reported) share.
func subtreeReservedUnsupported(k string) bool {
	return subtreeDefaultsRefusedKey(k) && !IsRoutingKey(k)
}

// SubtreeReservedWrite is one subtree defaults file writing a key it does not
// carry (subtreeReservedKeys): the file, root-relative slash path, and its
// value of the key as parsed (YAML-shaped) — what an operator moves into the
// tenant's own entry to keep it.
type SubtreeReservedWrite struct {
	File  string
	Value any
}

// subtreeReservedKeys is, per tenant, the subtreeReservedUnsupported keys a
// SUBTREE level of its defaults chain writes: tenantID → key → each subtree
// defaults file writing it with its value, in chain order (root-first). nil
// when there is none (#2388). da-guard reports each as the error
// subtree_default_reserved_key; the exporter applies none of them (#3341).
//
// Same inputs and the same "is this the root's file" test as
// applySubtreeDefaults (the conf.d root `_defaults.yaml` is skipped by path),
// so it reads the chain the exporter's build reads. It asks nothing of the
// value (beyond null, below) or of the tenant's own map.
//
// ⚠️ ROUTING KEYS ARE LEFT OUT (subtreeReservedUnsupported), and only here
// (#1976's report leaves them out through the shared
// subtreeDefaultsRefusedKey). The route generator's checks own every such key
// in a subtree defaults file (da-guard only lists it as undetermined
// evidence, reason routing_key), and IsRoutingKey is the same test as the
// generator's own (_grar_parse._is_routing_key): one inside `defaults:` is
// routing_in_unread_location (an error), `_routing_enforced` below the root
// is routing_enforced_below_root (an error), and a top-level
// `_routing_defaults` in a subtree `_defaults.yaml` with no `defaults:`
// wrapper is the route generator's documented spelling (#2326) — which
// `parsed` holds too, because the merge then reads the whole document.
// Listing them here would repeat an error, or report a valid file. A key
// under the `_routing` reserved prefix that is NOT a routing key
// (`_routingProfile`, `_routings`) is listed. Inside `defaults:` no
// generator check names it (`--validate --strict` passes such a file); at
// the top level of a file with no `defaults:` wrapper the generator names it
// too, as routing_key_ignored (blocking under --validate), so there it is
// reported by both (#2388 r2). guard.TopLevelReadElsewhere is NOT used for
// this: it names only the top-level `_routing_defaults` /
// `_routing_enforced`, and would leave a `_routing` inside `defaults:`
// reported twice.
//
// ⚠️ A NULL VALUE IS NOT LISTED (#2388 r1b): per the #2518 ruling a null in a
// defaults level writes nothing, so a null reserved key is treated as not
// written. A shallower level writing the same key non-null is still listed.
// "Writes nothing" is subtreeLevelRead's: a schedule whose `default:` is
// null is a null too (#2708, #3295).
//
// ⚠️ The `_custom_alerts` (and any other guard.TopLevelReadElsewhere) key at
// the TOP level of an unwrapped subtree file is listed here — `parsed` cannot
// tell top level from `defaults:` — and left out by da-guard, which can
// (checkSubtreeReservedKeys); the custom-alert compiler reads it there.
func subtreeReservedKeys(root string, tenantDefaults map[string][]string, parsed map[string]map[string]any) map[string]map[string][]SubtreeReservedWrite {
	if len(tenantDefaults) == 0 {
		return nil
	}
	rootDir := AbsScanRoot(root)
	var out map[string]map[string][]SubtreeReservedWrite
	for tenantID, chain := range tenantDefaults {
		for _, defaultsPath := range chain {
			if filepath.Dir(filepath.Clean(defaultsPath)) == rootDir {
				continue // the global defaults file — not a subtree level
			}
			rel := subtreeRelPath(rootDir, defaultsPath)
			for key, raw := range parsed[defaultsPath] {
				if !subtreeReservedUnsupported(key) {
					continue
				}
				if _, written, _ := subtreeLevelRead(key, raw); !written {
					continue
				}
				if out == nil {
					out = map[string]map[string][]SubtreeReservedWrite{}
				}
				if out[tenantID] == nil {
					out[tenantID] = map[string][]SubtreeReservedWrite{}
				}
				out[tenantID][key] = append(out[tenantID][key], SubtreeReservedWrite{File: rel, Value: raw})
			}
		}
	}
	return out
}

// disabledEverywhere reports whether every value sv can resolve to is a
// disable by resolveBaseRows' own test (thresholdRowDisabled): its default
// and each window's value. A schedule that is a number in
// any window is not — it would serve a row then.
func disabledEverywhere(sv ScheduledValue) bool {
	off := func(s string) bool { o, _ := thresholdRowDisabled(s); return o }
	if !off(sv.Default) {
		return false
	}
	for _, w := range sv.Overrides {
		if !off(w.Value) {
			return false
		}
	}
	return true
}

// unreachableKeys flattens the per-tenant sets into sorted slices.
//
// ⛔ SORTED because this feeds an operator-facing ERROR line and a gauge; Go
// map iteration is random and a diagnostic that reorders itself every reload
// reads as churn rather than as a stable fact.
func unreachableKeys[V any](byTenant map[string]map[string]V) map[string][]string {
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
// ⚠️ Reserved keys other than the routing keys never get here: a subtree
// does not carry them (subtreeReservedUnsupported, #3341).
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
// rewrote it to state the same split independently: of a `_defaults.yaml`,
// the keys inside `defaults:` enter `effective`, and so does the `tenants:`
// block of a root platform file (Decision 5); a value inside `defaults:` that
// will not parse as float64 (mapping / list / string / bool) makes
// `parsePartialConfig` return ok=false and the WHOLE file is dropped
// (Decision 6); and `_`-prefixed keys are consumed from the `tenants:` block,
// not from `defaults:`. Read docs/adr/017-defaults-yaml-inheritance-dual-hash.md
// (Decisions 5 and 6, and Consequences) before changing anything here — the measurements above were taken before
// that rewrite landed and are kept only because they are what this code was
// built against.
//
// So `isThresholdShaped` is a TYPE COERCION at a plane boundary, and the value
// domain it admits is the DESTINATION's, not the source's: what lands in
// `cfg.Tenants[t][key]` is a `ScheduledValue`, which legitimately holds
// `"disable"`, `"70:critical"` and a schedule — none of which `map[string]float64`
// would accept. Judging the source by the destination's domain is the whole
// point; judging it by `map[string]float64` instead would stop `"disable"`
// inheriting, and ADR-017 Decision 4 names `"disable"` as the sanctioned way for a
// threshold key to opt out of the chain.
//
// ⚠️ THE COST, STATED. Because the admitted domain is wider than the source
// block's declared type, this function can admit key/value pairs that a ROOT
// `_defaults.yaml` could not legally contain — which is why the reachability
// gate below had to adjudicate reserved-key shapes, until #3341 stopped
// handing those down. Every "silent shape"
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
	// different shape, and this overlay treats it the same way the tenant path
	// does: `UnmarshalYAML` takes its arbitrary-mapping branch there, nothing
	// is decoded into `Overrides`, and both a tenant authoring it and a subtree
	// handing it down resolve to the platform value. Measured both ways (50,
	// not the window's 90).
	//
	// ⛔ THAT IS CONSISTENT, NOT CORRECT, and an earlier version of this
	// comment said "correctly rejected". `/effective` renders the schedule
	// verbatim — `{"mysql_connections":{"overrides":[{"value":"90",…}]}}` —
	// while the series carries 50, and nothing reports the gap. It is the same
	// silent plane divergence this whole ticket is about, one shape over. It is
	// NOT introduced here (the tenant-authored control behaves identically, so
	// the overlay adds no divergence of its own) and fixing it means changing
	// `ScheduledValue.UnmarshalYAML`, which is a wider blast radius than this
	// PR. Recorded rather than fixed. (#1569, prompted by re-measuring a stale
	// CodeRabbit note that claimed the `default: ""` + windows shape was
	// dropped — that one is fixed and emits 90.)
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
	if off, _ := thresholdRowDisabled(raw); off {
		return true
	}
	// `value:severity` is the inline-severity form resolveBaseRows accepts —
	// read by the same function, so a severity it refuses (#3071), a
	// non-finite number (#3140) and `disable:<severity>` (#3141) are refused
	// here too.
	_, _, ok := parseThresholdValue(raw)
	return ok
}

// SubtreeDefaultsRefusedKey is subtreeDefaultsRefusedKey, for da-guard's
// defaults_toplevel_ignored fix (#2388 r4): the same predicate, not a copy.
func SubtreeDefaultsRefusedKey(k string) bool { return subtreeDefaultsRefusedKey(k) }

// Exported forms of the functions above, for package main's forwarders
// (config_subtree_defaults.go). Behavior pin: every one is `return x(args...)`.

func ScheduledValueFromRaw(raw any) (ScheduledValue, bool) { return scheduledValueFromRaw(raw) }
