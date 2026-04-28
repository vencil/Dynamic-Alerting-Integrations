package handler

// ============================================================
// v2.8.0 Phase B Track C (B-6) PR-2 — tenant-scoped authz helpers.
//
// The chi-route-level RBAC middleware grants PermRead/PermWrite on
// the route's primary tenant (extracted from `{id}` path param).
// But several endpoints accept a LIST of tenant IDs in the request
// body or operate on a stored list (groups.Members, view filters)
// — for those, route-level auth alone leaves an info-disclosure
// surface ("if you can hit `PUT /api/v1/groups/{id}`, you can
// rewrite that group to reference any tenants you can see in the
// platform"). These helpers close that gap.
//
// Design contract:
//   - Always check using the **request's** IDP groups (from the
//     auth middleware context), not the route handler's idea of
//     who the user is. Single source of truth.
//   - Return the FULL list of forbidden tenant IDs to the caller
//     (not just the first). Operators fixing their permission
//     misalignment shouldn't have to retry-and-discover.
//   - These are write-side checks — the read-side filtering helpers
//     in group.go (`hasAccessibleMember`, `filterAccessibleMembers`)
//     handle the symmetrical read concern.
// ============================================================

import (
	"github.com/vencil/tenant-api/internal/rbac"
)

// tenantsLackingPermission returns the subset of `tenantIDs` for
// which the caller (identified by `idpGroups`) does NOT have
// `want` permission. An empty slice means "all good — caller may
// proceed". A nil/empty `idpGroups` produces a forbidden list
// equal to `tenantIDs` (caller is anonymous → cannot write
// anything).
//
// Open-read mode: when the RBAC manager is in open mode (no
// `_rbac.yaml`), `HasPermission` returns true for every tenant.
// In that mode this helper returns an empty slice — no
// restrictions. This matches existing behaviour elsewhere in the
// handler package (e.g. `hasAccessibleMember`).
//
// Edge cases:
//   - tenantIDs is nil/empty → empty result (nothing to check)
//   - duplicate ids in input → de-duplicated output (forbidden ids
//     aren't repeated; matches what an operator wants to see)
func tenantsLackingPermission(rbacMgr *rbac.Manager, idpGroups, tenantIDs []string, want rbac.Permission) []string {
	if len(tenantIDs) == 0 {
		return nil
	}
	seen := make(map[string]bool, len(tenantIDs))
	forbidden := make([]string, 0)
	for _, tid := range tenantIDs {
		if tid == "" || seen[tid] {
			continue
		}
		seen[tid] = true
		if !rbacMgr.HasPermission(idpGroups, tid, want) {
			forbidden = append(forbidden, tid)
		}
	}
	return forbidden
}
