package guard

// `_custom_alerts` entries the exporter does not serve (#3234, #3235).
//
// An entry the exporter rejects or drops serves no row: that alert can never
// fire. Which finding each such entry is — value_not_served for a refused
// `threshold:` (#3209), custom_alert_duplicate_series for an entry an earlier
// one already serves (#2031), custom_alert_rejected for any other rejection —
// is CustomAlertFindingKind: da-guard's refused / rejected inputs are
// dispatched by it and `da-guard served-values` prints it per entry
// (`custom_alerts[].finding`); validate-config lists the findings without
// judging anything itself (guard_errors, #3387). The duplicate findings are still built from
// config.CustomAlertDuplicates (the same walk);
// TestGuard_EveryUnservedCustomAlertIsAFinding pins the two to one answer.
//
// ⛔ NO VERDICT OF ITS OWN: the status, the reason and whether the entry is
// switched off are the exporter's (config.CustomAlertEntries).

import (
	"fmt"
	"sort"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// CustomAlertFindingKind is the finding da-guard reports for one
// `_custom_alerts` entry, or the zero Kind for none: a served or disabled
// entry, and a switched-off entry the exporter would reject switched back on
// (the exporter takes it as a valid opt-out and counts no error for it).
func CustomAlertFindingKind(e config.CustomAlertEntry) findingkind.Kind {
	switch {
	case e.Status == config.CustomAlertDuplicated:
		return findingkind.FindingCustomAlertDuplicateSeries
	case e.Status != config.CustomAlertRejected, e.SwitchedOff:
		return findingkind.Kind{}
	case e.Refused != "":
		return findingkind.FindingValueNotServed
	default:
		return findingkind.FindingCustomAlertRejected
	}
}

// CustomAlertBlockFindingKind is the finding da-guard reports for a
// tenant's `_custom_alerts` block that does not decode (blockErr from
// config.CustomAlertEntries; "" when it decodes): the exporter serves none of
// it and counts it once on da_custom_alert_parse_errors, so it is
// custom_alert_rejected, on Field `_custom_alerts`. The zero Kind for none.
func CustomAlertBlockFindingKind(blockErr string) findingkind.Kind {
	if blockErr == "" {
		return findingkind.Kind{}
	}
	return findingkind.FindingCustomAlertRejected
}

// RejectedCustomAlert is one entry of CheckInput.CustomAlertsRejected: a
// `_custom_alerts` element CustomAlertFindingKind reports as
// custom_alert_rejected.
type RejectedCustomAlert struct {
	Index int    // the entry's index in the tenant's built list
	Name  string // its `name:` as decoded
	// Reason is the exporter's own rejection text (CustomAlertEntry.Reason).
	Reason string
}

// checkCustomAlertsRejected reports one custom_alert_rejected error per
// tenant of input.CustomAlertBlockErrors (Field `_custom_alerts`) and per
// entry of input.CustomAlertsRejected, for the tenants in
// input.EffectiveConfigs.
func checkCustomAlertsRejected(input CheckInput) []Finding {
	seen := map[string]bool{}
	for id := range input.CustomAlertsRejected {
		seen[id] = true
	}
	for id := range input.CustomAlertBlockErrors {
		seen[id] = true
	}
	ids := make([]string, 0, len(seen))
	for id := range seen {
		if _, inScope := input.EffectiveConfigs[id]; inScope {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	var out []Finding
	for _, id := range ids {
		if why := input.CustomAlertBlockErrors[id]; why != "" {
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     CustomAlertBlockFindingKind(why),
				TenantID: id,
				Field:    "_custom_alerts",
				Message: fmt.Sprintf("the exporter cannot decode the tenant's _custom_alerts (%s) — /metrics serves "+
					"none of its custom alerts and none of them can fire (it counts on da_custom_alert_parse_errors). "+
					"Write it as a list of custom-alert mappings.", why),
			})
		}
		for _, ca := range input.CustomAlertsRejected[id] {
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     findingkind.FindingCustomAlertRejected,
				TenantID: id,
				Field:    fmt.Sprintf("_custom_alerts[%d]", ca.Index),
				Message: fmt.Sprintf("the exporter rejects _custom_alerts[%d] (%q): %s — /metrics serves no series "+
					"for it and that alert can never fire (it counts on da_custom_alert_parse_errors). Fix the "+
					"field the reason names, or delete the entry.", ca.Index, ca.Name, ca.Reason),
			})
		}
	}
	return out
}
