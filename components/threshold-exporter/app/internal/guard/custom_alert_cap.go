package guard

// A tenant's own `_custom_alerts` over the per-tenant cap (#3042).
//
// Each custom-alert recipe of its own shape is a rule of its own, so the
// rule count grows with the tenants × their own recipes; the cap bounds it.
// Counted is what the exporter serves (config.CustomAlertRowCount, the
// resolver's own reading), as tenant-api's write check counts; a list a
// profile fills in is a shape the profile's tenants share and is not
// counted. validate-config's custom_alert_cap judges the same.
//
// ⛔ NO VERDICT OF ITS OWN: the counts are handed in as
// CheckInput.CustomAlertsOverCap.

import (
	"fmt"
	"sort"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// checkCustomAlertsOverCap reports one finding per tenant of
// input.CustomAlertsOverCap, for the tenants in input.EffectiveConfigs.
func checkCustomAlertsOverCap(input CheckInput) []Finding {
	ids := make([]string, 0, len(input.CustomAlertsOverCap))
	for id := range input.CustomAlertsOverCap {
		if _, inScope := input.EffectiveConfigs[id]; inScope {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	out := make([]Finding, 0, len(ids))
	for _, id := range ids {
		out = append(out, Finding{
			Severity: SeverityError,
			Kind:     findingkind.FindingCustomAlertOverCap,
			TenantID: id,
			Field:    "_custom_alerts",
			Message: fmt.Sprintf("The tenant's own `_custom_alerts` asks the exporter to serve %d custom-alert rows (counted before max_metrics_per_tenant truncates them), "+
				"over the per-tenant cap of %d (a switched-off recipe serves none, an slo_burn_rate two). "+
				"Remove recipes, or move ones several tenants share into a profile, whose recipes are not counted.",
				input.CustomAlertsOverCap[id], config.MaxCustomRecipesDefault),
		})
	}
	return out
}
