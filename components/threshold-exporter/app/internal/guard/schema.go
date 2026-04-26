package guard

// Schema validation — the first of the three planning §C-12 layers.
//
// Contract for PR-1: every required dotted-path field must resolve
// to a non-nil value in every tenant's effective config (post-merge).
// A missing field or an explicit YAML null at the path produces a
// SeverityError finding scoped to that tenant + field.
//
// Why nil is treated as "missing" rather than "explicitly cleared":
// in ADR-018 deepMerge semantics, YAML null in an override DELETES
// the inherited key, so a tenant that arrives at the schema check
// with `field=nil` at a required path has effectively opted out of
// the requirement. That's exactly the dangling-defaults scenario
// this guard exists to catch — flagging it as an error is the
// intended behaviour.
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
						"required field %q is present but null in tenant %q's effective config (YAML null deletes inherited keys per ADR-018)",
						field, tenantID),
				})
			}
		}
	}
	return out
}
