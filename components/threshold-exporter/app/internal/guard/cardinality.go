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
// (input.PredictedRowCounts) with input.CardinalityLimit and emits
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
					"tenant %q: the exporter would produce %d user_threshold rows at its busiest minute of the day, which exceeds the per-tenant cardinality limit of %d; it would silently truncate the excess (max_metrics_per_tenant)",
					tenantID, count, input.CardinalityLimit),
			})
		case count > warnFloor:
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     findingkind.FindingCardinalityWarning,
				TenantID: tenantID,
				Field:    "",
				Message: fmt.Sprintf(
					"tenant %q: the exporter would produce %d user_threshold rows at its busiest minute of the day, %d%% of the cardinality limit (%d) — only %d slots before runtime truncation",
					tenantID, count, percentOf(count, input.CardinalityLimit), input.CardinalityLimit, input.CardinalityLimit-count),
			})
		}
	}
	return out
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
