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
//   - Always check using the **request's** verified principal
//     (rbac.RequestPrincipal — attached by the auth middleware),
//     not the route handler's idea of who the user is. Single
//     source of truth; never hand-build a principal from parts.
//   - Return the FULL list of forbidden tenant IDs to the caller
//     (not just the first). Operators fixing their permission
//     misalignment shouldn't have to retry-and-discover.
//   - These are write-side checks — the read-side filtering helpers
//     in group.go (`hasAccessibleMember`, `filterAccessibleMembers`)
//     handle the symmetrical read concern.
// ============================================================

import (
	"net/http"

	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

// OrgAllowed is the single composition point of tenantorg.OrgsForTenant and
// rbac.AllowedInOrg (ADR-027 / LD-6 P4b): it resolves the target tenant's
// organization list AT DECISION TIME and feeds it to the org-scope-aware
// write-plane permission check. Every per-tenant write/admin authorization
// decision in the handler tree goes through here (or through the
// RequireOrgWrite wrapper below) — never through the org-blind rbac.Allowed;
// the org_write_guard tripwire pins that. A pure predicate so batch/member
// loops can call it per item and shape their own per-item error.
//
// tenantOrg may be nil (a Deps built without wiring the manager, e.g. a
// handler test literal): OrgsForTenant is nil-receiver-safe and reports the
// tenant as unlabeled, which with no org-scoped rule is byte-identical to
// the pre-P4b permission check.
func OrgAllowed(rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager,
	p *rbac.VerifiedPrincipal, tenantID string, want rbac.Permission) bool {
	orgs, _ := tenantOrg.OrgsForTenant(tenantID)
	return rbacMgr.AllowedInOrg(p, tenantID, want, orgs)
}

// RequireOrgWrite is the top-of-handler convenience wrapper over OrgAllowed
// for single-tenant write handlers: it answers "may the request's verified
// principal perform `want` on tenantID?" and, when the answer is no, writes
// the canonical 403 envelope (same WriteJSONErrorWithCode shape as the
// federation handlers) and returns false so the caller can bail with a bare
// `return`. The message names the org axis so an operator denied by an
// org-scoped rule knows which knob to look at, but deliberately reveals
// neither the tenant's org list nor any principal claim value (principal.go
// logging discipline — the org names are an enumeration oracle).
func RequireOrgWrite(w http.ResponseWriter, r *http.Request, d *Deps, tenantID string, want rbac.Permission) bool {
	// A Deps literal without an RBAC manager is a TEST-ONLY state (nil-safe
	// contract, mirroring TenantOrg above): main.go always wires a non-nil
	// manager — even open mode is a non-nil Manager — and the route-level
	// RBAC middleware dereferences the same manager, so a nil here can never
	// be reached by a routed production request. Treating it as "no RBAC
	// layer configured" preserves the pre-P4b behavior of the handlers this
	// wrapper now guards, which performed no in-handler permission check.
	if d.RBAC == nil {
		return true
	}
	if OrgAllowed(d.RBAC, d.TenantOrg, rbac.RequestPrincipal(r), tenantID, want) {
		return true
	}
	WriteJSONErrorWithCode(w, r, http.StatusForbidden, CodeForbidden,
		"insufficient permissions for tenant "+tenantID+
			" (permission and organization-scope checks, ADR-027)")
	return false
}

// tenantsLackingPermission returns the subset of `tenantIDs` for
// which the caller (identified by principal `p`) does NOT have
// `want` permission. An empty slice means "all good — caller may
// proceed". A nil principal is the anonymous caller (no groups) —
// it produces a forbidden list equal to `tenantIDs` (anonymous →
// cannot write anything).
//
// The check is org-scope-aware (ADR-027 / LD-6 P4b): each tenant is
// evaluated through OrgAllowed, so an org-scoped rule only grants a
// member tenant it covers. tenantOrg may be nil (test literals) —
// see OrgAllowed.
//
// Open mode (no `_rbac.yaml`): the permission check grants READ
// only — a write/admin check denies every tenant, so for
// PermWrite/PermAdmin this helper returns the full input list.
// In practice the route-level middleware's platform-scope gate
// (`Allowed(p, "*", PermWrite)`) already 403s an open-mode write
// request before any handler calling this helper runs; the
// per-tenant denial here is the defense-in-depth layer (see
// TestTenantsLackingPermission_OpenModeWriteRejectsAll).
//
// Edge cases:
//   - tenantIDs is nil/empty → empty result (nothing to check)
//   - duplicate ids in input → de-duplicated output (forbidden ids
//     aren't repeated; matches what an operator wants to see)
func tenantsLackingPermission(rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, p *rbac.VerifiedPrincipal, tenantIDs []string, want rbac.Permission) []string {
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
		if !OrgAllowed(rbacMgr, tenantOrg, p, tid, want) {
			forbidden = append(forbidden, tid)
		}
	}
	return forbidden
}

// filterByRBAC returns the subset of `items` whose tenant ID
// (extracted via `tenantID(item)`) the caller has the given
// permission on. Items with empty tenant IDs are passed through
// — that's how administrative entries (e.g. PRs not bound to a
// single tenant) end up surfacing to readers without false-403'ing
// on a missing tag.
//
// Empty / nil input → returned as-is. Open-read mode (no
// _rbac.yaml) → Allowed returns true for every tenant, so
// this helper effectively becomes the identity transform — no
// restrictions, allocation cost only.
//
// Deliberately org-blind (ADR-027 / LD-6 P4b): this is a read-plane filter,
// and org visibility on the read plane is ScopeAllowed's job (list/search);
// read-listing under org-scope lands in P4c. Calling the org-blind Allowed
// here is one of the three exemptions the org_write_guard tripwire allowlists
// — see internal/rbac/org_write_guard_test.go.
//
// Generic over the slice element type so it covers the four
// near-identical filter wrappers below (members []string,
// PRInfo, async.TaskResult, etc.) without copy-paste drift on
// the loop body.
func filterByRBAC[T any](
	rbacMgr *rbac.Manager,
	p *rbac.VerifiedPrincipal,
	items []T,
	tenantID func(T) string,
	perm rbac.Permission,
) []T {
	if len(items) == 0 {
		return items
	}
	out := make([]T, 0, len(items))
	for _, item := range items {
		tid := tenantID(item)
		if tid == "" || rbacMgr.Allowed(p, tid, perm) {
			out = append(out, item)
		}
	}
	return out
}
