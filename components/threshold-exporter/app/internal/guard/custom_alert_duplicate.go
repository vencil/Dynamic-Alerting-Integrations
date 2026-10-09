package guard

// `_custom_alerts` entries whose series an earlier entry already serves
// (#2031).
//
// Two entries of one tenant's list putting the same series on /metrics — a
// custom alert with the same name and shape twice, or two slo_burn_rate
// alerts of one shape (one user_slo_objective{tenant, recipe_id}) — used to
// fail the exporter's whole Gather: /metrics answered 500 for every tenant.
// The exporter now serves the first and drops the later one
// (da_custom_alert_parse_errors, an ERROR line); this check refuses it before
// merge.
//
// ⛔ NO VERDICT OF ITS OWN: the entries are the exporter's own reading of the
// tenant's built map (config.CustomAlertDuplicates, the function its resolver
// drops them with), handed in as CheckInput.CustomAlertDuplicates.

import (
	"fmt"
	"sort"
)

// FindingCustomAlertDuplicateSeries (error; #2031): a `_custom_alerts` entry
// the exporter drops because an earlier entry already serves its series.
const FindingCustomAlertDuplicateSeries FindingKind = "custom_alert_duplicate_series"

// checkCustomAlertDuplicates reports one finding per entry of
// input.CustomAlertDuplicates, for the tenants in input.EffectiveConfigs.
func checkCustomAlertDuplicates(input CheckInput) []Finding {
	ids := make([]string, 0, len(input.CustomAlertDuplicates))
	for id := range input.CustomAlertDuplicates {
		if _, inScope := input.EffectiveConfigs[id]; inScope {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	var out []Finding
	for _, id := range ids {
		for _, d := range input.CustomAlertDuplicates[id] {
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingCustomAlertDuplicateSeries,
				TenantID: id,
				Field:    fmt.Sprintf("_custom_alerts[%d]", d.Index),
				Message: fmt.Sprintf("_custom_alerts[%d] (%q) puts on /metrics the series %s that _custom_alerts[%d] (%q) "+
					"already puts there, so the exporter serves only the first and drops this entry (it counts on "+
					"da_custom_alert_parse_errors). Delete one of the two, or change this one's name, metric, "+
					"objective or shape.", d.Index, d.Name, d.Series, d.Of, d.OfName),
			})
		}
	}
	return out
}
