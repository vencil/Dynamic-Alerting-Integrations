package guard

// Schema validation — the first of the three planning §C-12 layers.
//
// Contract for PR-1: every required dotted-path field must resolve
// to a non-nil value in every tenant's effective config (post-merge).
// A missing field or an explicit YAML null at the path produces a
// SeverityError finding scoped to that tenant + field.
//
// Why nil is treated as "missing" rather than "explicitly cleared":
// a required path that resolves to nil carries no value for the
// exporter to act on, however it got there. That is the
// dangling-defaults scenario this guard exists to catch.
//
// ⚠️ Rationale updated by #1339. It used to read "YAML null in an
// override DELETES the inherited key", which was the blanket ADR-017
// rule at the time. The rule is now per-key: null deletes only a
// reserved (`_`-prefixed) key; on a THRESHOLD key it is a no-op,
// because the emitting path (collector.go → ResolveAtWithStats)
// ignores it and falls back to the platform default. Deleting it here
// would have flagged a tenant whose series is being exported fine.
//
// So `cpu: ~` in a tenant override no longer reaches this check as
// missing. The reachable ways a required path still lands on nil are
// (a) neither the defaults chain nor the tenant ever supplied it, and
// (b) a nil nested inside a subtree the tenant introduced wholesale —
// deepMerge copies a brand-new subtree verbatim, nils included.
//
// PR-1 doesn't enforce field types or value ranges. PR-2/3 may add
// a `RequiredFieldSpec` shape with type + range constraints once
// the v2.8.0 mandatory-fields list locks down. Until then the
// caller's RequiredFields list captures pure presence assertions.

import "fmt"

// checkRequiredFields runs the schema validation pass.
//
// Returns the findings (possibly empty). Findings are NOT sorted
// here — run.go does the global sort once across all checks for
// stable output.
//
// Determinism: iterates input.RequiredFields in caller-supplied
// order, then tenants in sorted ID order. Two runs over the same
// input emit findings in the same sequence even before the
// downstream sort.
func checkRequiredFields(input CheckInput) []Finding {
	if len(input.RequiredFields) == 0 || len(input.EffectiveConfigs) == 0 {
		return nil
	}

	tenants := sortedTenantIDs(input.EffectiveConfigs)
	var out []Finding
	for _, tenantID := range tenants {
		merged := input.EffectiveConfigs[tenantID]
		for _, field := range input.RequiredFields {
			value, found := resolvePath(merged, field)
			if !found {
				out = append(out, Finding{
					Severity: SeverityError,
					Kind:     FindingMissingRequired,
					TenantID: tenantID,
					Field:    field,
					Message: fmt.Sprintf(
						"required field %q is missing from tenant %q's effective config after merging the new defaults",
						field, tenantID),
				})
				continue
			}
			if value == nil {
				out = append(out, Finding{
					Severity: SeverityError,
					Kind:     FindingMissingRequired,
					TenantID: tenantID,
					Field:    field,
					Message: fmt.Sprintf(
						"required field %q is present but null in tenant %q's effective config — no value for the exporter to act on (a null on a threshold key is NOT an opt-out; use \"disable\" to stop alerting)",
						field, tenantID),
				})
			}
		}
	}
	return out
}
