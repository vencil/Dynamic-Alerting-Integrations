package guard

// Subtree defaults the exporter cannot deliver (#1976).
//
// A key that only a subtree `_defaults.yaml` names — not the conf.d root
// `_defaults.yaml`, not `optional_overrides:` — shows in the tenant's
// effective config, but the exporter serves no series for it: the collector
// iterates the root defaults and the declared surface, and a nested `_` file
// feeds neither. The exporter logs that at load time (ERROR) and counts the
// tenant on da_config_subtree_undeliverable_tenants. This check reports the
// THRESHOLD keys of that set (not starting with `_`) before the change
// merges. Reserved / `_` keys in a subtree `_defaults.yaml` are not this
// finding's: its fix (declare the key at the root) is wrong for them; see
// #2388.
//
// ⛔ NO VERDICT OF ITS OWN. Which keys are undeliverable is the exporter's
// build's answer (pkg/config FlatBuild.Unreachable, judged by
// keyCanReachTheOutputPlane, filtered to threshold keys by pkg/config's
// undeliverableThresholds), handed in by the caller as
// CheckInput.UndeliverableInherited; nothing here re-derives it.
//
// Warn for now; planned to become an error in the next minor release.

import (
	"fmt"
	"sort"
)

// FindingSubtreeDefaultUndeliverable (warn; #1976): the tenant inherits Field,
// a threshold key a subtree `_defaults.yaml` names and the conf.d root `_defaults.yaml`
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
					"and that alert can never fire (the tenant's effective config still shows the value; the exporter "+
					"logs an ERROR and counts the tenant on da_config_subtree_undeliverable_tenants). Declare `%s` in "+
					"the conf.d root `_defaults.yaml` or in `optional_overrides:`. This warning becomes an error in "+
					"the next minor release (#1976). Only threshold keys are reported here; reserved (`_`) keys in "+
					"a subtree `_defaults.yaml` are covered by #2388.", k, k),
			})
		}
	}
	return out
}
