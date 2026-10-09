package guard

// Values /metrics does not serve as written (#2065).
//
// A threshold value the exporter cannot use does not fail its load: the
// resolver serves the platform default instead, serves no series, never
// applies a schedule window it cannot read, or the subtree overlay keeps a
// shallower level's value. The exporter states it at load time (gauge
// da_config_values_not_served + WARN); this check refuses it before merge.
//
// ⛔ NO VERDICT OF ITS OWN. Which (tenant, key) pairs are affected, and why,
// is the effective config's NotServed (pkg/config: the resolver's own record
// and the build's refused subtree values) — the table `da-guard effective`
// prints as not_served — handed in as CheckInput.ValuesNotServed. This check
// keeps the reasons whose cause is a written value, on threshold keys
// (config.ValueNotServedAsWritten: a reserved key a subtree `_defaults.yaml`
// writes is subtree_default_reserved_key's or a routing check's); the other
// reasons have findings of their own (parse_failed is exit 3, undeliverable is
// subtree_default_undeliverable, root_null_undeclared is
// root_default_null_undeclared, root_defaults_unwrapped is the
// finding of that name).

import (
	"fmt"
	"sort"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// FindingValueNotServed (error; #2065): the tenant's effective config shows
// Field with a value /metrics does not serve as written. The message starts
// with the not_served reason.
const FindingValueNotServed FindingKind = "value_not_served"

// checkValuesNotServed reports one FindingValueNotServed per (tenant, key) of
// input.ValuesNotServed that config.ValueNotServedAsWritten selects (the
// written-value reasons, on a threshold key), for the tenants in
// input.EffectiveConfigs only.
func checkValuesNotServed(input CheckInput) []Finding {
	if len(input.ValuesNotServed) == 0 {
		return nil
	}
	var out []Finding
	for _, id := range sortedNotServedTenants(input) {
		keys := input.ValuesNotServed[id]
		names := make([]string, 0, len(keys))
		for k := range keys {
			names = append(names, k)
		}
		sort.Strings(names)
		for _, k := range names {
			ns := keys[k]
			if !config.ValueNotServedAsWritten(k, ns.Reason) {
				continue
			}
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingValueNotServed,
				TenantID: id,
				Field:    k,
				Message:  valueNotServedMessage(k, ns),
			})
		}
	}
	return out
}

func sortedNotServedTenants(input CheckInput) []string {
	out := make([]string, 0, len(input.ValuesNotServed))
	for id := range input.ValuesNotServed {
		if _, inScope := input.EffectiveConfigs[id]; inScope {
			out = append(out, id)
		}
	}
	sort.Strings(out)
	return out
}

// valueNotServedMessage is the finding's message: the reason first, then the
// file, the consequence and the fix.
func valueNotServedMessage(k string, ns config.NotServedKey) string {
	file := ns.File
	if file == "" {
		file = "the tenant's config"
	}
	switch ns.Reason {
	case config.NotServedValueUnparsed:
		return fmt.Sprintf("value_unparsed: %s writes `%s` with a value that is not a number the exporter reads "+
			"(e.g. `abc`, a list, `7O:critical`), so /metrics serves the platform default for it instead and the "+
			"alert fires at that default, not at the written threshold. Write a number, optionally with "+
			"`:<severity>` (e.g. `70` or `70:critical`), or `disable`.", file, k)
	case config.NotServedValueUnparsedDropped:
		return fmt.Sprintf("value_unparsed_dropped: %s writes `%s` with a value that is not a number the exporter "+
			"reads, and the key has no platform default to fall back to (a `_critical` tier, a dimensional key or "+
			"an `optional_overrides:` key), so /metrics serves no series for it and that alert can never fire. "+
			"Write a number (e.g. `90`), or `disable`.", file, k)
	case config.NotServedWindowInvalid:
		return fmt.Sprintf("window_invalid: %s writes `%s` as a schedule with an override whose `window:` the "+
			"exporter does not accept — not a UTC `HH:MM-HH:MM` (hours 00-23, minutes 00-59), missing, or a start "+
			"equal to its end — so that override never applies. Fix the window (e.g. `01:00-09:00`; `22:00-06:00` crosses midnight) or remove the override.",
			file, k)
	case config.NotServedSpellingDuplicate:
		return fmt.Sprintf("spelling_duplicate: %s writes `%s` beside another spelling of the same threshold in the "+
			"same mapping (dimensional labels in another order or quoted differently, or the retired #1231 name), "+
			"so /metrics serves the other spelling's value and not this one. Keep one spelling and delete the other.",
			file, k)
	default: // config.NotServedValueRejected
		return fmt.Sprintf("value_rejected: %s writes `%s` with a value that is not threshold-shaped (e.g. "+
			"`overrides:` written as a string instead of a list of `{window, value}`), so the exporter does not "+
			"apply it to this tenant, which keeps a shallower level's value or none. Write a number or a "+
			"`{default, overrides: [{window, value}]}` schedule.", file, k)
	}
}
