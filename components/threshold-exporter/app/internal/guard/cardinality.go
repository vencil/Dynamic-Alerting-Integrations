package guard

// Cardinality guard — PR-3 of the C-12 Dangling Defaults Guard family.
//
// Premise: the runtime in components/threshold-exporter/app/
// config_resolve.go::ResolveAt enforces a per-tenant ceiling on
// resolved metric thresholds (DefaultMaxMetricsPerTenant = 500).
// Tenants over the ceiling have their excess silently truncated
// with a WARN log line. Two failure modes that motivates this guard:
//
//   - A `_defaults.yaml` edit that adds N metrics pushes one or
//     more tenants past the ceiling. Operators only find out via
//     the WARN log AFTER deploy — and silent truncation means
//     some alerts simply stop firing without the tenant noticing.
//
//   - A tenant's effective config is already at 95% of the ceiling
//     and an unrelated defaults edit adds one more metric — the
//     change passes review because nobody checked headroom.
//
// PR-3 catches both pre-merge by predicting the post-merge metric
// count per tenant.
//
// COUNTING MODEL (intentionally a conservative upper bound)
// ---------------------------------------------------------
// Predicted metric count per tenant = number of top-level keys in
// the effective config that aren't "special". Special keys mirror
// the skip-list in config_resolve.go::ResolveAt (lines 47-54):
//
//   - prefix `_state_`         (state filters; ResolveStateFilters)
//   - prefix `_silent_`        (silent modes; ResolveSilentModes)
//   - prefix `_routing`        (routing block; ResolveRouting)
//   - exact match `_severity_dedup` (ADR-001)
//   - exact match `_metadata`  (ADR-018: never inherited, never resolved)
//
// What this counter MISSES:
//   - Dimensional expansion. A YAML like `redis_keys{db="db0"}` and
//     `redis_keys{db="db1"}` is two keys (counted as 2) but produces
//     2 thresholds at runtime — counted correctly by accident. But
//     a regex-based dimensional rule (`redis_keys{db=~"db[0-9]+"}`)
//     would expand to N thresholds at runtime that the guard counts
//     as 1. UNDER-COUNT in this case.
//
//   - Tenant-disabled metrics. The effective config is post-merge,
//     so tenant overrides that explicitly disable a default (YAML
//     null per ADR-018) are already removed by the merge engine
//     before the guard sees them. So the count is correctly
//     post-disable. ✓
//
//   - `_critical` suffix overrides. These produce additional
//     threshold rows at runtime but are stored as separate
//     top-level keys (e.g. `mysql_connections_critical`), so they
//     ARE counted. ✓
//
// Net direction: the counter UNDER-counts dimensional regex
// expansions and is exact otherwise. That's the safer bias —
// guard says "you're at the limit" only when you really might be,
// and a tenant that the guard says is safe could still hit
// runtime truncation if they use heavy regex dimensional rules.
// The CLI wrapper (PR-4) should expose a `--strict-dimensional`
// flag to fold the regex expansion estimate in once we have the
// fixture data to calibrate it.

import (
	"fmt"
	"strings"
)

// defaultCardinalityWarnRatio matches the value documented on
// CheckInput.CardinalityWarnRatio (80%).
const defaultCardinalityWarnRatio = 0.8

// checkCardinality runs the per-tenant cardinality prediction and
// emits findings. Returns nothing when CardinalityLimit ≤ 0
// (caller opted out).
//
// The check operates on input.EffectiveConfigs — the same merged
// per-tenant maps the schema validator (PR-1) reads. No extra
// caller plumbing required beyond setting CardinalityLimit.
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
		count := countMetricKeys(input.EffectiveConfigs[tenantID])
		switch {
		case count > input.CardinalityLimit:
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingCardinalityExceeded,
				TenantID: tenantID,
				Field:    "",
				Message: fmt.Sprintf(
					"tenant %q: predicted metric count %d exceeds the per-tenant cardinality limit of %d; runtime would silently truncate the excess (config_resolve.go::ResolveAt)",
					tenantID, count, input.CardinalityLimit),
			})
		case count > warnFloor:
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     FindingCardinalityWarning,
				TenantID: tenantID,
				Field:    "",
				Message: fmt.Sprintf(
					"tenant %q: predicted metric count %d is %d%% of the cardinality limit (%d) — only %d slots before runtime truncation",
					tenantID, count, percentOf(count, input.CardinalityLimit), input.CardinalityLimit, input.CardinalityLimit-count),
			})
		}
	}
	return out
}

// countMetricKeys returns the number of top-level keys in `effective`
// that the runtime would translate into metric threshold rows. See
// the package header for the precise skip-list rationale.
//
// Purely deterministic over the input map's contents; no map
// iteration order leaks into the count.
func countMetricKeys(effective map[string]any) int {
	n := 0
	for k := range effective {
		if isSpecialKey(k) {
			continue
		}
		n++
	}
	return n
}

// isSpecialKey reports whether `k` is a non-metric top-level key
// per the skip-list in config_resolve.go::ResolveAt (kept in
// lock-step manually). Adding a new prefix on the runtime side
// without updating here would over-count and trigger spurious
// cardinality findings.
//
// CAUTION when editing: any new "_*" semantic prefix added to
// ResolveAt's loop body needs to land here too.
func isSpecialKey(k string) bool {
	if k == "_severity_dedup" || k == "_metadata" {
		return true
	}
	return strings.HasPrefix(k, "_state_") ||
		strings.HasPrefix(k, "_silent_") ||
		strings.HasPrefix(k, "_routing")
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
