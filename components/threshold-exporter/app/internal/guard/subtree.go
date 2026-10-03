package guard

// Subtree defaults the exporter cannot deliver (#1976).
//
// A key that only a subtree `_defaults.yaml` names — not the conf.d root
// `_defaults.yaml`, not `optional_overrides:` — shows in the tenant's
// effective config, but the exporter serves no series for it: the collector
// iterates the root defaults and the declared surface, and a nested `_` file
// feeds neither. The exporter logs that at load time (ERROR) and counts the
// tenant on da_config_subtree_undeliverable_tenants. This check reports, before
// the change merges, the keys of that set for which "declare it at the root"
// is the fix: not the reserved keys, not the keys the exporter never serves
// as a threshold row (`_silent_*`, `_state_*`, …; both reported by
// checkSubtreeReservedKeys, #2388), and not keys
// the subtree switches off (pkg/config undeliverableThresholds).
//
// ⛔ NO VERDICT OF ITS OWN. Which keys are undeliverable is the exporter's
// build's answer (pkg/config FlatBuild.Unreachable, judged by
// keyCanReachTheOutputPlane, filtered by pkg/config's
// undeliverableThresholds), handed in by the caller as
// CheckInput.UndeliverableInherited; nothing here re-derives it.
//
// Warn for now; planned to become an error in the next minor release.

import (
	"fmt"
	"sort"
	"strings"
)

// FindingSubtreeDefaultUndeliverable (warn; #1976): the tenant inherits Field,
// a threshold key (see undeliverableThresholds) a subtree `_defaults.yaml` names and the conf.d root `_defaults.yaml`
// and `optional_overrides:` do not declare. The exporter serves no series for
// it. Planned to become an error in the next minor release.
const FindingSubtreeDefaultUndeliverable FindingKind = "subtree_default_undeliverable"

// checkSubtreeUndeliverable reports one FindingSubtreeDefaultUndeliverable per
// (tenant, key) of input.UndeliverableInherited, for the tenants in
// input.EffectiveConfigs only (the caller's scope).
func checkSubtreeUndeliverable(input CheckInput) []Finding {
	if len(input.UndeliverableInherited) == 0 {
		return nil
	}
	tenants := make([]string, 0, len(input.UndeliverableInherited))
	for id := range input.UndeliverableInherited {
		if _, inScope := input.EffectiveConfigs[id]; inScope {
			tenants = append(tenants, id)
		}
	}
	sort.Strings(tenants)
	var out []Finding
	for _, id := range tenants {
		keys := append([]string(nil), input.UndeliverableInherited[id]...)
		sort.Strings(keys)
		for _, k := range keys {
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     FindingSubtreeDefaultUndeliverable,
				TenantID: id,
				Field:    k,
				Message: fmt.Sprintf("Threshold `%s` is inherited from a subtree `_defaults.yaml`, but neither the conf.d root "+
					"`_defaults.yaml` nor `optional_overrides:` declares it, so the exporter serves no series for it "+
					"and that alert can never fire (the tenant's effective config still shows a value for it; the "+
					"exporter logs an ERROR and counts the tenant on da_config_subtree_undeliverable_tenants). %s "+
					"This warning becomes an "+
					"error in the next minor release (#1976). Not reported here: reserved keys and keys the exporter "+
					"never serves as a threshold row (`_silent_*`, `_state_*`, …; reported as subtree_default_reserved_key, #2388), "+
					"and keys the subtree "+
					"switches off.", k, undeliverableFix(k)),
			})
		}
	}
	return out
}

// undeliverableFix is the finding's fix sentence for key k.
//
// ⛔ A `_` KEY GETS THE ROOT ONLY. resolveDeclaredRows (the reader of
// `optional_overrides:`) skips every `_`-prefixed key, so declaring a `_` key
// there serves nothing and leaves this finding standing (the build's
// reachability test asks the same skip, #2707). Declared in the root
// `_defaults.yaml` it is served. The `_` keys that reach this finding are
// the ones undeliverableThresholds keeps (not reserved, not skipped by the
// row generator).
func undeliverableFix(k string) string {
	if strings.HasPrefix(k, "_") {
		return fmt.Sprintf("Declare `%s` in the conf.d root `_defaults.yaml`; `optional_overrides:` does not "+
			"serve keys starting with `_`.", k)
	}
	return fmt.Sprintf("Declare `%s` in the conf.d root `_defaults.yaml` or in `optional_overrides:`.", k)
}
