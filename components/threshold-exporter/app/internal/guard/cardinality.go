package guard

// Cardinality guard — PR-3 of the C-12 Dangling Defaults Guard family.
//
// Premise: the exporter's resolver (pkg/config resolve.go,
// resolveAtWithStats) enforces a per-tenant ceiling on the user_threshold
// rows it produces (the root `_defaults.yaml`'s max_metrics_per_tenant,
// else DefaultMaxMetricsPerTenant = 500). Tenants over the ceiling have
// their excess silently truncated with an ERROR log line. Two failure
// modes motivate this guard:
//
//   - A `_defaults.yaml` edit that adds N metrics pushes one or
//     more tenants past the ceiling. Operators only find out via
//     the log line AFTER deploy — and silent truncation means
//     some alerts simply stop firing without the tenant noticing.
//
//   - A tenant is already at 95% of the ceiling and an unrelated
//     defaults edit adds one more metric — the change passes review
//     because nobody checked headroom.
//
// COUNTING MODEL (#3380): the output plane's own count
// ----------------------------------------------------
// The predicted count per tenant is the number of user_threshold rows the
// exporter's resolver produces for it over the exporter's own build of the
// tree, BEFORE the cut — config.ThresholdConfig.PeakTenantRowCounts, handed
// in as CheckInput.PredictedRowCounts. That is the very count the cut is
// judged against, so this file holds no key-name rule of its own: a key the
// build does not deliver counts nothing, a `_critical` key, a dimensional
// key (a regex one included), a #1231 legacy twin or a custom-alert recipe
// counts the rows the resolver gives it, a "disable" counts none. A
// schedule that adds rows at some time of day is counted at its busiest
// minute — the cut truncates there.
//
// The limit compared against (CardinalityLimit) is a budget: an explicit
// --cardinality-limit may differ from the exporter's own cap
// (CheckInput.ExporterRowCap), so each finding says whether that cap
// truncates the rows, rather than assuming the budget is the cap. A count
// over the exporter's cap is an error whatever the budget (the guard exists
// to catch truncation before deploy); a limit ≤ 0 still turns the whole
// check off.
//
// ⛔ NO COUNT OF ITS OWN: before #3380 this file counted the top-level keys
// of the effective config, less a hand-kept skip-list of reserved prefixes;
// a key the exporter never serves (a subtree default the root does not
// declare) was counted as a row.

import (
	"fmt"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
)

// defaultCardinalityWarnRatio matches the value documented on
// CheckInput.CardinalityWarnRatio (80%).
const defaultCardinalityWarnRatio = 0.8

// checkCardinality compares each in-scope tenant's predicted row count
// (input.PredictedRowCounts) with input.CardinalityLimit, and with the
// exporter's own cap (input.ExporterRowCap, when known), and emits
// findings. Returns nothing when CardinalityLimit ≤ 0 (caller opted out);
// a tenant absent from PredictedRowCounts produces no row, so no finding.
func checkCardinality(input CheckInput) []Finding {
	if input.CardinalityLimit <= 0 {
		return nil
	}
	warnRatio := input.CardinalityWarnRatio
	if warnRatio <= 0 || warnRatio > 1 {
		warnRatio = defaultCardinalityWarnRatio
	}
	// Compute warn floor up front. Use float math so a limit of 100
	// with ratio 0.8 gives 80 exactly (rather than int-truncating to
	// 80 either way — both work, but floats make non-integer ratios
	// like 0.85 round predictably).
	warnFloor := int(float64(input.CardinalityLimit) * warnRatio)

	tenants := sortedTenantIDs(input.EffectiveConfigs)
	var out []Finding
	for _, tenantID := range tenants {
		count := input.PredictedRowCounts[tenantID]
		switch {
		case count > input.CardinalityLimit:
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     findingkind.FindingCardinalityExceeded,
				TenantID: tenantID,
				Field:    "",
				Message: fmt.Sprintf(
					"tenant %q: the exporter would produce %d user_threshold rows at its busiest minute of the day, which exceeds the per-tenant cardinality limit of %d",
					tenantID, count, input.CardinalityLimit) + exporterCapClause(count, input.ExporterRowCap),
			})
		case input.ExporterRowCap > 0 && count > input.ExporterRowCap:
			// Within the budget but over the exporter's own cap (an explicit
			// limit above it): the exporter truncates, so it is an error
			// whatever the budget says (#3380).
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     findingkind.FindingCardinalityExceeded,
				TenantID: tenantID,
				Field:    "",
				Message: fmt.Sprintf(
					"tenant %q: the exporter would produce %d user_threshold rows at its busiest minute of the day, within the per-tenant cardinality limit of %d but over the exporter's own max_metrics_per_tenant of %d; the exporter would silently truncate them to %d",
					tenantID, count, input.CardinalityLimit, input.ExporterRowCap, input.ExporterRowCap),
			})
		case count > warnFloor:
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     findingkind.FindingCardinalityWarning,
				TenantID: tenantID,
				Field:    "",
				Message: fmt.Sprintf(
					"tenant %q: the exporter would produce %d user_threshold rows at its busiest minute of the day, %d%% of the cardinality limit (%d), %d below it",
					tenantID, count, percentOf(count, input.CardinalityLimit), input.CardinalityLimit, input.CardinalityLimit-count) +
					exporterCapClause(count, input.ExporterRowCap),
			})
		}
	}
	return out
}

// exporterCapClause says what the exporter's own cut (exporterCap,
// CheckInput.ExporterRowCap) does to count rows — the cardinality limit is a
// budget that may differ from it (an explicit --cardinality-limit), so a
// finding only claims truncation when count is over the exporter's cap.
// "" when the cap is unknown (0).
func exporterCapClause(count, exporterCap int) string {
	switch {
	case exporterCap > 0 && count > exporterCap:
		return fmt.Sprintf("; the exporter would silently truncate them to its max_metrics_per_tenant of %d", exporterCap)
	case exporterCap > 0:
		return fmt.Sprintf("; the exporter's own max_metrics_per_tenant (%d) truncates none of them", exporterCap)
	case exporterCap < 0:
		return "; the exporter's max_metrics_per_tenant is negative (no cap), so it truncates none of them"
	}
	return ""
}

// percentOf returns ⌊count / limit × 100⌋. Defensive division —
// callers always pass limit > 0 (checkCardinality returns early
// when limit ≤ 0), but a zero-divide in a Sprintf path would crash
// the whole report rather than skip one tenant.
func percentOf(count, limit int) int {
	if limit <= 0 {
		return 0
	}
	return int(float64(count) * 100.0 / float64(limit))
}
