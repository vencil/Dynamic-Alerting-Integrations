package guard

// Top-level entry point — runs every check, collates findings,
// computes summary, returns the GuardReport.
//
// Determinism: every internal check returns findings in a stable
// order, and run.go does ONE final sort so the report is identical
// across runs (the GitHub Actions wrapper diffs reports between
// PR pushes — non-deterministic ordering would create noise
// comments). Sort key: severity (errors first), then tenant ID,
// then field path.

import (
	"fmt"
	"sort"
)

// CheckDefaultsImpact runs the guard against a single proposed
// `_defaults.yaml` change and returns the GuardReport.
//
// The function is pure (no IO, no globals). Caller errors are not
// thrown via panic — invalid input becomes a fatal error return so
// CI runners can distinguish "input bad" from "tenant bad".
//
// Errors:
//   - len(EffectiveConfigs)==0 → fmt error. The guard's whole
//     purpose is to validate against tenants; running it against
//     an empty tenant set is almost always a caller bug (the
//     correct path for "no tenants in this scope" is to skip
//     calling the guard entirely).
//
// Otherwise CheckDefaultsImpact never errors. A clean run is a
// GuardReport with zero Findings and PassedTenantCount equal to
// the number of tenants.
func CheckDefaultsImpact(input CheckInput) (*GuardReport, error) {
	if len(input.EffectiveConfigs) == 0 {
		return nil, fmt.Errorf("guard: no tenants supplied in EffectiveConfigs (caller should skip the guard entirely when scope is empty)")
	}

	var findings []Finding
	findings = append(findings, checkRequiredFields(input)...)
	findings = append(findings, checkRedundantOverrides(input)...)

	// Stable sort: errors before warnings, then by tenant id, then
	// by field path. Within the same (severity, tenant, field) we
	// don't expect duplicates from PR-1 checks but the final tie-
	// breaker on Kind keeps the order defined if PR-2/3 ever produce
	// overlapping findings.
	sort.SliceStable(findings, func(i, j int) bool {
		fi, fj := findings[i], findings[j]
		if fi.Severity != fj.Severity {
			// SeverityError comes first.
			return fi.Severity == SeverityError
		}
		if fi.TenantID != fj.TenantID {
			return fi.TenantID < fj.TenantID
		}
		if fi.Field != fj.Field {
			return fi.Field < fj.Field
		}
		return fi.Kind < fj.Kind
	})

	report := &GuardReport{
		Findings: findings,
		Summary: GuardSummary{
			TotalTenants: len(input.EffectiveConfigs),
		},
	}

	// Roll-up counts. PassedTenantCount needs the per-tenant view
	// of "any error?" — a tenant with two warnings still passes,
	// a tenant with one error doesn't.
	failingTenants := make(map[string]struct{})
	for _, f := range findings {
		switch f.Severity {
		case SeverityError:
			report.Summary.Errors++
			if f.TenantID != "" {
				failingTenants[f.TenantID] = struct{}{}
			}
		case SeverityWarn:
			report.Summary.Warnings++
		}
	}
	report.Summary.PassedTenantCount = len(input.EffectiveConfigs) - len(failingTenants)
	return report, nil
}

// sortedTenantIDs returns the keys of m sorted alphabetically.
// Defined here so both schema.go and redundant.go share one
// implementation; keeping it package-private avoids an external API
// we'd have to maintain for downstream consumers.
func sortedTenantIDs(m map[string]map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// sortedKeys is the map[string]any cousin of sortedTenantIDs. Used
// by checkRedundantOverrides to walk flattened leaves in stable
// order so the eventual report is reproducible.
func sortedKeys(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
