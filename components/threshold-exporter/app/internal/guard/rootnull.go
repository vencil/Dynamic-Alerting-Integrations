package guard

// Root defaults written as null (#2518).
//
// A threshold the conf.d root `_defaults.yaml` writes as null (`key:`, `~`,
// `null`) is no write: the root does not declare the key. Before #2518 it
// decoded to a threshold of 0, which declared the key — so a tenant that set
// its own value was served. Now nothing serves that tenant's value: the
// collector iterates the root defaults and `optional_overrides:`, and the key
// is in neither. This check names each such (tenant, key) before the change
// merges; `da-guard served-values` already lists the key under `unserved`.
//
// ⛔ NO VERDICT OF ITS OWN. Which keys are affected is the exporter's build's
// answer (pkg/config FlatBuild.RootNullUndeclared: the root carrier's null
// keys as ParseConfigFile drops them, crossed with each tenant's built map
// and the build's own reachability test, filtered by undeliverableThresholds),
// handed in by the caller as CheckInput.RootNullUndeclared.
//
// ⛔ ONE CAUSE, ONE FINDING. A key only a subtree `_defaults.yaml` hands down
// is subtree_default_undeliverable's (the cause there is the subtree value
// nothing declares); the build never puts such a key in a tenant's map, so it
// never reaches this set. This finding is for a value on the tenant's side —
// its own file, a root platform file's `tenants:` entry or a profile.

import (
	"fmt"
	"sort"
	"strings"
)

// FindingRootDefaultNullUndeclared (warn; #2518): the tenant's config sets
// Field, which the conf.d root `_defaults.yaml` writes as null — not
// declared — so the exporter serves no series for it.
const FindingRootDefaultNullUndeclared FindingKind = "root_default_null_undeclared"

// checkRootNullUndeclared reports one FindingRootDefaultNullUndeclared per
// (tenant, key) of input.RootNullUndeclared, for the tenants in
// input.EffectiveConfigs only (the caller's scope).
func checkRootNullUndeclared(input CheckInput) []Finding {
	if len(input.RootNullUndeclared) == 0 {
		return nil
	}
	tenants := make([]string, 0, len(input.RootNullUndeclared))
	for id := range input.RootNullUndeclared {
		if _, inScope := input.EffectiveConfigs[id]; inScope {
			tenants = append(tenants, id)
		}
	}
	sort.Strings(tenants)
	var out []Finding
	for _, id := range tenants {
		keys := append([]string(nil), input.RootNullUndeclared[id]...)
		sort.Strings(keys)
		for _, k := range keys {
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     FindingRootDefaultNullUndeclared,
				TenantID: id,
				Field:    k,
				Message: fmt.Sprintf("The tenant sets threshold `%s` (in its own file, a root platform file's `tenants:` "+
					"entry or a profile), but the conf.d root `_defaults.yaml` writes it as null. A null is no value, so "+
					"the root does not declare `%s` and the exporter serves no series for it: that alert can never fire "+
					"(before #2518 the null was a threshold of 0 and the tenant's value was served). %s", k, k, rootNullFix(k)),
			})
		}
	}
	return out
}

// rootNullFix is the finding's fix sentence for key k. Both were measured to
// serve the tenant's value: a number in the root `_defaults.yaml`, and the
// key listed under `optional_overrides:` (with the null line left or
// removed). A `_` key gets the root only — `optional_overrides:` does not
// serve keys starting with `_` (see undeliverableFix).
func rootNullFix(k string) string {
	if strings.HasPrefix(k, "_") {
		return fmt.Sprintf("Write a number for `%s` in the conf.d root `_defaults.yaml` instead of null.", k)
	}
	return fmt.Sprintf("Write a number for `%s` in the conf.d root `_defaults.yaml` instead of null, or list it "+
		"under `optional_overrides:` to declare it without a platform value.", k)
}
