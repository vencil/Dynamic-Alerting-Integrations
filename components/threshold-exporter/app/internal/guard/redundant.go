package guard

// Redundant-override check — Claude补 layer per planning §C-12.
//
// Premise: a tenant.yaml field that has the same value as the new
// `_defaults.yaml` at the same dotted path carries no information.
// At runtime ADR-017 deepMerge produces the same effective value
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
// What counts as redundant (#4330):
//   - A threshold key (a top-level key of the tenant's own file not
//     starting with `_`): CheckInput.RedundantThresholds — deleting the line
//     leaves what the exporter serves for the tenant the same from now on
//     (the exporter's own build and resolve), and no other finding owns the
//     line by that finding's own verdict (the shared key verdict,
//     value_not_served). No reading of values here.
//   - A reserved `_` key (whole, never a leaf nested under it) the registry
//     names the exporter as the one reader of, in every layer it can be
//     inherited from (config.RedundancyJudgedByValue): its scalar value
//     equals the inherited one (scalarsEqual). Every other reserved key —
//     one the route generator, the annotation or the custom-alert compiler
//     reads, a mapping or a list — is not judged (#4331: no oracle sees an
//     `expires:`, a weekday window, or a field another reader reads).

import (
	"fmt"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// checkRedundantOverrides runs the reverse-warn pass.
//
// Returns warnings (possibly empty). Caller-supplied
// TenantOverrides is required. Defaults source is one of:
//
//   - NewDefaultsByTenant[id] — per-tenant merged defaults (PR-5).
//     Used when present for that tenant.
//   - NewDefaults — single global merged defaults (PR-1).
//     Fallback when NewDefaultsByTenant lacks the tenant's entry.
//
// A tenant with no defaults source from either field has the check
// silently skipped (no finding emitted) — this lets callers wire
// the field opportunistically without forcing every tenant in scope
// to have a defaults entry.
//
// Determinism: walks tenants in sorted-ID order, then iterates the
// flattened override leaves in sorted-path order so the output is
// reproducible across runs.
func checkRedundantOverrides(input CheckInput) []Finding {
	if len(input.TenantOverrides) == 0 {
		return nil
	}

	// Cache the global flattened defaults so we don't re-walk the
	// map per tenant when only the legacy single-map field is set.
	var globalLeaves map[string]any
	if input.NewDefaults != nil {
		globalLeaves = flattenLeaves(input.NewDefaults)
	}

	tenants := sortedTenantIDs(input.TenantOverrides)
	var out []Finding
	var fields []string // this tenant's redundant fields, sorted before they are emitted
	emit := func(field string) { fields = append(fields, field) }
	flush := func(tenantID string) {
		sort.Strings(fields)
		for _, field := range fields {
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     findingkind.FindingRedundantOverride,
				TenantID: tenantID,
				Field:    field,
				Message: fmt.Sprintf(
					"tenant %q overrides %q with the same value as the new defaults; remove the override and rely on inheritance",
					tenantID, field),
			})
		}
		fields = nil
	}
	for _, tenantID := range tenants {
		overrides := input.TenantOverrides[tenantID]
		reserved := map[string]any{}
		for _, key := range sortedKeys(overrides) {
			if strings.HasPrefix(key, "_") {
				reserved[key] = overrides[key]
				continue
			}
			if !input.RedundantThresholds[tenantID][key] {
				continue
			}
			emit(key)
		}
		defaultLeaves := tenantDefaultsLeaves(input, tenantID, globalLeaves)
		if len(reserved) == 0 || defaultLeaves == nil {
			flush(tenantID)
			continue // no reserved key, or no defaults context for this tenant
		}
		// A reserved key is judged whole, never a leaf nested under it, and
		// only where the registry makes its value the line's one effect
		// (config.RedundancyJudgedByValue, #4331): an equal scalar is then
		// what the exporter reads either way. A mapping or a list
		// (scalarsEqual) can carry an `expires:`, a weekday window or a field
		// another reader reads, which no value comparison sees.
		for _, key := range sortedKeys(reserved) {
			if !config.RedundancyJudgedByValue(key) {
				continue
			}
			defaultValue, exists := defaultLeaves[key]
			if !exists || !scalarsEqual(reserved[key], defaultValue) {
				continue
			}
			if inheritedFromRefusedSubtreeKey(input, tenantID, key) {
				continue
			}
			emit(key)
		}
		flush(tenantID)
	}
	return out
}

// tenantDefaultsLeaves resolves the right defaults-leaves map for a
// given tenant: per-tenant entry wins, otherwise fall back to the
// global precomputed map. Returns nil when neither source has a
// defaults map for this tenant — caller treats that as "skip".
func tenantDefaultsLeaves(input CheckInput, tenantID string, globalLeaves map[string]any) map[string]any {
	if perTenant, ok := input.NewDefaultsByTenant[tenantID]; ok {
		if perTenant == nil {
			return nil
		}
		return flattenLeaves(perTenant)
	}
	return globalLeaves
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

// inheritedFromRefusedSubtreeKey reports whether the leaf at path belongs to
// a top-level key the tenant inherits from a SUBTREE `_defaults.yaml` and that
// subtree defaults refuse (config.SubtreeDefaultsRefusedKey): the
// subtree_default_reserved_key set, CheckInput.SubtreeReservedKeys — read off
// the exporter's own chain, no YAML parsed here.
//
// ⛔ SUCH AN OVERRIDE IS NOT REDUNDANT (#2388 r5). The merged defaults show the
// subtree's value, but the exporter does not apply it (#3341;
// subtree_default_reserved_key): the tenant's own key is the one that takes
// effect, and telling the tenant to delete it and "rely on inheritance"
// points the wrong way.
// Measured: subtree `_silent_mode: warning` + the same in the tenant's entry
// served ["warning"], and redundant_override said to remove the tenant's line.
// A key inherited from the ROOT only is not in the set and is still judged.
func inheritedFromRefusedSubtreeKey(input CheckInput, tenantID, path string) bool {
	top, _, _ := strings.Cut(path, ".")
	if !config.SubtreeDefaultsRefusedKey(top) {
		return false
	}
	_, fromSubtree := input.SubtreeReservedKeys[tenantID][top]
	return fromSubtree
}
