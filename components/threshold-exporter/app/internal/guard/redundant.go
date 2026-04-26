package guard

// Redundant-override check — Claude补 layer per planning §C-12.
//
// Premise: a tenant.yaml field that has the same value as the new
// `_defaults.yaml` at the same dotted path carries no information.
// At runtime ADR-018 deepMerge produces the same effective value
// either way, so the override is dead weight that:
//   - Bloats per-tenant YAML (the GitOps anti-pattern Phase .c
//     fights).
//   - Hides genuine intent — a future reviewer can't tell whether
//     the tenant author MEANT to override (keeping the value pinned
//     even if defaults change) or just forgot to remove a stale
//     line.
//
// PR-1 emits SeverityWarn so reviewers see the cleanup hint without
// blocking merge. The duplication is harmless at runtime.
//
// Equality semantics:
//   - Scalars (string / int / float / bool): direct ==.
//   - Nil values: nil == nil. A tenant that explicitly sets a field
//     to YAML null while the new defaults also have it as null is
//     redundant (and weird, but we still flag).
//   - Maps + slices: NOT compared in PR-1. Comparing structured
//     values needs reflect.DeepEqual or a recursive walker; the
//     return is marginal (most redundant overrides are scalars)
//     and the false-positive risk on slice-order drift is real.
//     PR-2 may extend if customer feedback warrants.

import "fmt"

// checkRedundantOverrides runs the reverse-warn pass.
//
// Returns warnings (possibly empty). Caller-supplied
// TenantOverrides + NewDefaults are required; passing nil for
// either disables the check entirely (no findings emitted).
//
// Determinism: walks tenants in sorted-ID order, then iterates the
// flattened override leaves in sorted-path order so the output is
// reproducible across runs.
func checkRedundantOverrides(input CheckInput) []Finding {
	if len(input.TenantOverrides) == 0 || input.NewDefaults == nil {
		return nil
	}

	defaultLeaves := flattenLeaves(input.NewDefaults)
	tenants := sortedTenantIDs(input.TenantOverrides)

	var out []Finding
	for _, tenantID := range tenants {
		overrideLeaves := flattenLeaves(input.TenantOverrides[tenantID])
		paths := sortedKeys(overrideLeaves)
		for _, path := range paths {
			tenantValue := overrideLeaves[path]
			defaultValue, exists := defaultLeaves[path]
			if !exists {
				continue
			}
			if !scalarsEqual(tenantValue, defaultValue) {
				continue
			}
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     FindingRedundantOverride,
				TenantID: tenantID,
				Field:    path,
				Message: fmt.Sprintf(
					"tenant %q overrides %q with the same value as the new defaults; remove the override and rely on inheritance",
					tenantID, path),
			})
		}
	}
	return out
}

// scalarsEqual is the PR-1 equality test for redundant-override
// detection. Returns true for matching scalars + nil; returns
// false for any structured value (map / slice) so we don't false-
// positive on order drift in slices or key drift in maps.
//
// Per the package header: PR-1 deliberately scopes redundancy
// detection to scalars; PR-2 may layer reflect.DeepEqual when the
// customer corpora justify the false-positive trade-off.
func scalarsEqual(a, b any) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	switch a.(type) {
	case map[string]any, []any:
		return false
	}
	switch b.(type) {
	case map[string]any, []any:
		return false
	}
	return a == b
}
