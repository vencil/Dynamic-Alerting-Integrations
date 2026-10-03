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

	"github.com/vencil/threshold-exporter/pkg/config"
)

// FindingRootDefaultNullUndeclared (warn; #2518): the tenant's config sets
// Field, which the conf.d root `_defaults.yaml` writes as null — not
// declared — so the exporter serves no series for it. Field may also be a
// `<base>_critical` key whose base the root writes as null: the critical row
// needs the base in the root defaults.
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
		keys := append([]config.RootNullKey(nil), input.RootNullUndeclared[id]...)
		sort.Slice(keys, func(i, j int) bool { return keys[i].Key < keys[j].Key })
		for _, rk := range keys {
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     FindingRootDefaultNullUndeclared,
				TenantID: id,
				Field:    rk.Key,
				Message:  rootNullMessage(rk),
			})
		}
	}
	return out
}

// rootNullMessage is the finding's message for one key; which shape it has
// (a base threshold, or a critical row whose base is null) is the build's
// answer (config.RootNullKey.CriticalRow), not judged here.
func rootNullMessage(rk config.RootNullKey) string {
	const was = "(before #2518 the null was a threshold of 0 and the tenant's value was served)"
	written := rootSpellings(rk)
	if rk.CriticalRow {
		return fmt.Sprintf("The tenant sets `%s` (in its own file, a root platform file's `tenants:` entry or a "+
			"profile), the critical tier of `%s`, but the conf.d root `_defaults.yaml` writes %s as null. A null "+
			"is no value, so the root does not declare `%s`, and the exporter serves a critical row only for a base "+
			"the root `_defaults.yaml` holds: no series is served for `%s` and that critical alert can never fire %s. "+
			"Write a number for `%s` in the conf.d root `_defaults.yaml` instead of null (that also serves `%s` at "+
			"warning severity to every tenant that does not set it); listing `%s` under `optional_overrides:` does "+
			"not serve the critical row.",
			rk.Key, rk.NullKey, written, rk.NullKey, rk.Key, was, rk.NullKey, rk.NullKey, rk.NullKey)
	}
	return fmt.Sprintf("The tenant sets threshold `%s` (in its own file, a root platform file's `tenants:` "+
		"entry or a profile), but the conf.d root `_defaults.yaml` writes %s as null. A null is no value, so "+
		"the root does not declare `%s` and the exporter serves no series for it: that alert can never fire "+
		"%s. %s", rk.Key, written, rk.Key, was, rootNullFix(rk.Key))
}

// rootSpellings is how the message names what the root writes as null: the
// spelling(s) the build found in the root file (config.RootNullKey
// .RootSpellings), with a note when none of them is the threshold the
// message is about — so the text it quotes is text the root holds.
func rootSpellings(rk config.RootNullKey) string {
	if len(rk.RootSpellings) == 0 {
		return fmt.Sprintf("`%s`", rk.NullKey) // a caller-built input without spellings
	}
	quoted := make([]string, len(rk.RootSpellings))
	same := false
	for i, s := range rk.RootSpellings {
		quoted[i] = "`" + s + "`"
		if s == rk.NullKey {
			same = true
		}
	}
	out := strings.Join(quoted, " and ")
	if !same {
		out += fmt.Sprintf(" (the other spelling of `%s`)", rk.NullKey)
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
