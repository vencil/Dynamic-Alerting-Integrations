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
	"strconv"
)

// applySubtreeDefaults writes each tenant's L1..Ln inherited defaults into its
// own override map, for keys the tenant has not set itself. Returns the number
// of (tenant, key) pairs filled in — 0 for a flat tree, which is what makes it
// free for deployments that never nest.
//
// ⛔ ROOT DEFAULTS ARE SKIPPED ON PURPOSE. chain[0] is the root
// `_defaults.yaml`, already in `cfg.Defaults` and already applied to every
// tenant. Writing it per-tenant as well would turn every tenant's map into a
// full copy of the platform surface — O(tenants × keys) memory for no change
// in behaviour.
//
// ⛔ A KEY THE TENANT ALREADY HAS IS NEVER TOUCHED, including `"disable"`.
// The tenant's own file wins over anything it inherits; that is the whole
// inheritance model, and `disable` in particular must survive — silently
// re-enabling a threshold a tenant switched off would be the worst possible
// failure mode for this function.
func applySubtreeDefaults(
	cfg *ThresholdConfig,
	tenantDefaults map[string][]string,
	parsed map[string]map[string]any,
) int {
	if cfg == nil || len(cfg.Tenants) == 0 || len(tenantDefaults) == 0 {
		return 0
	}
	filled := 0
	for tenantID, overrides := range cfg.Tenants {
		chain := tenantDefaults[tenantID]
		if len(chain) < 2 {
			continue // root-only chain: nothing a subtree adds
		}
		for _, defaultsPath := range chain[1:] {
			for key, raw := range parsed[defaultsPath] {
				if _, authored := overrides[key]; authored {
					continue
				}
				value, ok := scalarToThresholdString(raw)
				if !ok {
					// Non-scalar (a nested mapping, a list) is not a threshold
					// value. `Resolve()` ignores it too; staying quiet here
					// keeps the two planes agreeing on what a defaults file
					// contributes.
					continue
				}
				if overrides == nil {
					overrides = map[string]ScheduledValue{}
					cfg.Tenants[tenantID] = overrides
				}
				overrides[key] = ScheduledValue{Default: value}
				filled++
			}
		}
	}
	return filled
}

// scalarToThresholdString renders a YAML scalar the way a tenant file would
// have written it. Returns ok=false for anything that is not a threshold
// scalar.
//
// ⚠️ Floats go through `strconv.FormatFloat(..., 'f', -1, 64)` — the shortest
// representation that round-trips — rather than `%v`, which renders 1e+06 for
// a large threshold and would not parse back the same way downstream.
func scalarToThresholdString(raw any) (string, bool) {
	switch v := raw.(type) {
	case string:
		return v, true
	case int:
		return strconv.Itoa(v), true
	case int64:
		return strconv.FormatInt(v, 10), true
	case float64:
		return strconv.FormatFloat(v, 'f', -1, 64), true
	case bool:
		// A bool is never a threshold. Excluded explicitly so it cannot fall
		// through to a default branch and become the string "true".
		return "", false
	default:
		return "", false
	}
}
