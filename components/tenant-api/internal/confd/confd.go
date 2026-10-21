// Package confd answers, for tenant-api, "which conf.d file is a tenant's":
// the enumeration (ListTenantFiles), the per-tenant resolver
// (ResolveTenantFile) and the write plane's addressing rule
// (IsAddressableTenantID). Every scanner that enumerates tenants (tenant
// list, federation account backfill, orphan detection) skips on the same
// predicate, and ValidateTenantID rejects any id whose {id}.yaml would not
// satisfy it. Keeping the write-accepted id namespace structurally equal to
// the scanned-file namespace is what stops a caller from addressing a
// reserved control file (_defaults.yaml, _rbac.yaml, _domain_policy.yaml, …)
// as a tenant — the class of bug that a prefix-by-prefix denylist re-opens
// every time a new reserved convention is added to the scanner alone.
//
// ⛔ THE NAME RULE IS NOT HERE. "Is this name a tenant file, and for which
// id" is confdname.TenantNamedBy (threshold-exporter's pkg/confdname), the
// one Go copy the exporter's walker and da-guard use too (ADR-036 §7 step 9,
// #3206). This package composes it; it does not restate it. Its properties
// that tenant-api relies on — the extension is matched in any case (#1537:
// the exporter reads `upper.YAML`, and a tenant-api that rejected it left a
// live tenant out of the orphan detector's known set), and the id keeps the
// STEM'S ORIGINAL case (`Upper.YAML` → `Upper`, never `upper`: the exporter
// takes ids from the `tenants:` keys, so folding here would rename a tenant
// on this plane only) — are pinned in that package.
package confd

import (
	"strings"

	"github.com/vencil/threshold-exporter/pkg/confdname"
)

// IsTenantConfigFile reports whether name would be picked up as a tenant
// config by the conf.d scanners (not reserved, YAML suffix).
//
// Two caller shapes, and only one of them saw #1537's change:
//
//   - ENUMERATORS pass a real directory entry name, so `upper.YAML` went from
//     skipped to scanned. That is the fix.
//   - VALIDATORS (handler.ValidateTenantID, gitops.Writer) pass a synthesised
//     `id + ".yaml"`, whose suffix is already lowercase — so the folded test
//     and the exact test decide identically and the write-accepted id
//     namespace did NOT widen. That is not an argument, it is measured, and
//     it is pinned by TestValidatorCallShapeDidNotWiden in this package so it
//     stays true if the extension rule is touched again.
func IsTenantConfigFile(name string) bool {
	_, ok := confdname.TenantNamedBy(name)
	return ok
}

// IsAddressableTenantID reports whether id may be used to ADDRESS a tenant
// file — the write plane's question, as opposed to IsTenantConfigFile's "would
// a scanner pick this name up". It is the same rule plus the structural checks
// that keep `filepath.Join(configDir, id+".yaml")` inside configDir.
//
// Those structural checks live here rather than at each write site because
// IsTenantConfigFile alone does NOT reject separators: measured, "a/b",
// "a/../../b" and "/abs" all satisfy it (nothing in a name rule cares about
// directories), so a second, weaker hand-written copy at a write site is the
// exact shape this package exists to prevent (#1911 family, #1681).
func IsAddressableTenantID(id string) bool {
	// An empty-id guard and a filepath.Base guard both used to sit here; each
	// was measured unkillable (the last line already answers the same way).
	if strings.ContainsAny(id, `/\`) || strings.Contains(id, "..") {
		return false
	}
	return IsTenantConfigFile(id + ".yaml")
}
