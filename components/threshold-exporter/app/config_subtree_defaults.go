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
) int {
	if cfg == nil || len(cfg.Tenants) == 0 || len(tenantDefaults) == 0 {
		return 0
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
	rootDir := filepath.Clean(root)
	if abs, aerr := filepath.Abs(root); aerr == nil {
		rootDir = filepath.Clean(abs)
	}
	// Keys this overlay introduced that the ROOT defaults do not carry. See
	// declareSubtreeKeys for why they have to be declared, and what it costs.
	subtreeOnly := map[string]struct{}{}

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
				if _, global := cfg.Defaults[key]; !global {
					subtreeOnly[key] = struct{}{}
				}
			}
		}
	}
	declareSubtreeKeys(cfg, subtreeOnly)
	return filled
}

// declareSubtreeKeys adds keys that exist ONLY in a subtree `_defaults.yaml`
// to the platform's declared surface.
//
// ⛔ WITHOUT THIS THE OVERLAY IS A NO-OP FOR SUCH KEYS, which is not obvious
// from the write itself. Nothing iterates a tenant's override map to decide
// what to emit: `resolveBaseRows` walks `cfg.Defaults` and `resolveDeclaredRows`
// walks `cfg.OptionalOverrides`, and a nested `_` file contributes to neither
// (its content is deliberately kept out of the merged config so it cannot
// re-price the whole tree). So the value landed in a map no emitter reads.
// Measured: tenant map held `redis_evicted_keys:100`, `/effective` reported
// 100, and `user_threshold` had no such series — plus a
// `WARN: unknown key "redis_evicted_keys" not in defaults` on every commit,
// because `ValidateTenantKeys` checks the same two surfaces. (#1569 blind
// review.)
//
// ⛔ `OptionalOverrides` is the right surface and not a workaround: its whole
// definition is "the platform RECOGNISES this key but asserts no value", and
// `resolveDeclaredRows` emits a row only when the tenant supplied one — which
// is exactly the state the overlay just created. Adding it to `cfg.Defaults`
// instead would give every tenant in the tree a platform value it never
// inherited, the leak `TestASubtreeDefaultNeverLeaksIntoTheGlobalOnes` exists
// to forbid.
//
// ⚠️ COST, STATED RATHER THAN HIDDEN: `OptionalOverrides` is a flat global
// list with no subtree scope of its own, so a key introduced by
// `finance/_defaults.yaml` becomes a key the write gate will accept from a
// tenant OUTSIDE `finance/` too. It still emits nothing for such a tenant
// unless that tenant supplies a value, and the file that declared it is a
// platform-scoped `_` file either way — but the accepted-key surface is
// genuinely wider than the directory that declared it. Narrowing it needs
// per-subtree scope in `ThresholdConfig`, which is #1568's territory, not a
// patch here.
func declareSubtreeKeys(cfg *ThresholdConfig, subtreeOnly map[string]struct{}) {
	if len(subtreeOnly) == 0 {
		return
	}
	for _, existing := range cfg.OptionalOverrides {
		delete(subtreeOnly, existing)
	}
	if len(subtreeOnly) == 0 {
		return
	}
	added := make([]string, 0, len(subtreeOnly))
	for key := range subtreeOnly {
		added = append(added, key)
	}
	sort.Strings(added) // map iteration is random; the config must not be
	cfg.OptionalOverrides = append(cfg.OptionalOverrides, added...)
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
// would also refuse, and for an explicit null — `key:` with no value is a
// declaration, not a threshold.
func scheduledValueFromRaw(raw any) (ScheduledValue, bool) {
	var sv ScheduledValue
	if raw == nil {
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
func isThresholdShaped(sv ScheduledValue) bool {
	raw := strings.TrimSpace(sv.Default)
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
