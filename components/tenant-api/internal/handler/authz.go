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
	"os"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

// ScopeMetaFunc resolves one tenant's (environment, domain) for a write-plane
// scope decision (#1597).
//
// Resolution happens at DECISION TIME, straight from the tenant's own file. A
// stale authorization input on the plane that MUTATES state is a real (if
// small) window, and the cost argument for caching does not hold here: the same
// request is about to run a git commit, which is orders of magnitude more
// expensive than one file read. (The read/collection filters deliberately do
// NOT take one of these — they are per-item loops, and binding the metadata
// axis there is a separate plane with its own migration, out of scope here.)
//
// It fails SOFT to the empty pair — an unlabeled tenant — which is exactly what
// the pre-#1597 metadata-blind write plane behaved like. Resolution never
// invents a label it could not read.
type ScopeMetaFunc func(tenantID string) (environment, domain string)

// WriteScopeMeta builds the write-plane resolver: one targeted read of the
// tenant's actual file (confd.ResolveTenantFile, so a `<id>.yml` tenant is not
// silently treated as unlabeled — #1673).
func WriteScopeMeta(configDir string) ScopeMetaFunc {
	return func(tenantID string) (string, string) {
		if configDir == "" {
			return "", ""
		}
		path, err := confd.ResolveTenantFile(configDir, tenantID)
		if err != nil {
			return "", "" // absent, ambiguous or unsafe id → unlabeled
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return "", ""
		}
		var summary TenantSummary
		extractMetadata(&summary, data, tenantID)
		return summary.Environment, summary.Domain
	}
}

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
	p *rbac.VerifiedPrincipal, tenantID string, want rbac.Permission, meta ScopeMetaFunc) bool {
	orgs, _ := tenantOrg.OrgsForTenant(tenantID)
	var environment, domain string
	if meta != nil {
		environment, domain = meta(tenantID)
	}
	return rbacMgr.AllowedInOrg(p, tenantID, want, orgs, environment, domain)
}

// OrgAllowedRead is the READ/VISIBILITY-plane sibling of OrgAllowed (ADR-027 /
// LD-6 P4c): it resolves the tenant's org list and routes through
// rbac.AllowedInOrgRead, which records its would-deny on axis="org" (the same
// series the list-plane ScopeAllowed uses) rather than the write plane's
// axis="org_write". Used by the collection read filters (filterByRBAC over
// PR lists / task results, and hasAccessibleMember/filterAccessibleMembers for
// the ListGroups and GetGroup member lists) so those reads stop being a
// cross-org tenant-reference oracle once the org flag flips — the read plane
// closes in lockstep with read-by-id. Like OrgAllowed, tenantOrg may be nil
// (nil-receiver-safe → unlabeled).
func OrgAllowedRead(rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager,
	p *rbac.VerifiedPrincipal, tenantID string, want rbac.Permission) bool {
	orgs, _ := tenantOrg.OrgsForTenant(tenantID)
	return rbacMgr.AllowedInOrgRead(p, tenantID, want, orgs)
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
	return requireOrgWriteWithMeta(w, r, d, tenantID, want, WriteScopeMeta(d.ConfigDir),
		"insufficient permissions for tenant "+tenantID+
			" (permission and organization-scope checks, ADR-027)")
}

// RequireOrgWriteProposed is the POST-STATE half of the write gate (#1597
// follow-up). RequireOrgWrite authorizes against the tenant's metadata as it is
// ON DISK; that is the right question to ask before reading the body, but it is
// not the only one, because `_metadata` lives INSIDE the file a whole-file PUT
// replaces. A caller authorized for environment=production could therefore
// submit a body setting environment=dev and have it committed — the write would
// be checked against production and land in dev, planting configuration in a
// scope the caller does not administer (and removing the tenant from their own
// reach).
//
// This is specific to the metadata axis. The org axis cannot be attacked this
// way: org membership lives in the admin-only `_tenant_orgs.yaml`, a separate
// file this path cannot write. Nor can the batch path reach it — it refuses to
// overwrite a structured key like `_metadata` with a scalar patch value
// (tenant_batch.go). Whole-file PUT is the exposed shape.
//
// The check runs the SAME predicate against the PROPOSED metadata, so it
// inherits the axis's flag: in shadow it changes nothing, and it closes the gap
// exactly when --rbac-metadata-write-scope-enforce is flipped. Call it AFTER
// the body is read and BEFORE any write; RequireOrgWrite still runs first so a
// denied caller learns nothing from a body it was never allowed to submit.
func RequireOrgWriteProposed(w http.ResponseWriter, r *http.Request, d *Deps,
	tenantID string, want rbac.Permission, proposedYAML string) bool {
	return requireOrgWriteWithMeta(w, r, d, tenantID, want, proposedScopeMeta(proposedYAML),
		"insufficient permissions for the tenant metadata this write proposes"+
			" — the environment/domain in the body places tenant "+tenantID+
			" outside your scope (#1597)")
}

// proposedScopeMeta reads environment/domain from the content the caller is
// proposing to write, rather than from disk. Same extractor the list plane
// uses, so a body and a stored file are read identically.
func proposedScopeMeta(yamlContent string) ScopeMetaFunc {
	return func(tenantID string) (string, string) {
		var summary TenantSummary
		extractMetadata(&summary, []byte(yamlContent), tenantID)
		return summary.Environment, summary.Domain
	}
}

func requireOrgWriteWithMeta(w http.ResponseWriter, r *http.Request, d *Deps,
	tenantID string, want rbac.Permission, meta ScopeMetaFunc, denyMsg string) bool {
	// A Deps literal without an RBAC manager is a TEST-ONLY state (nil-safe
	// contract, mirroring TenantOrg above): main.go always wires a non-nil
	// manager — even open mode is a non-nil Manager — and the route-level
	// RBAC middleware dereferences the same manager, so a nil here can never
	// be reached by a routed production request. Treating it as "no RBAC
	// layer configured" preserves the pre-P4b behavior of the handlers these
	// wrappers guard, which performed no in-handler permission check.
	if d.RBAC == nil {
		return true
	}
	if OrgAllowed(d.RBAC, d.TenantOrg, rbac.RequestPrincipal(r), tenantID, want, meta) {
		return true
	}
	WriteJSONErrorWithCode(w, r, http.StatusForbidden, CodeForbidden, denyMsg)
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
func tenantsLackingPermission(rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, p *rbac.VerifiedPrincipal, tenantIDs []string, want rbac.Permission, meta ScopeMetaFunc) []string {
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
		if !OrgAllowed(rbacMgr, tenantOrg, p, tid, want, meta) {
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
// _rbac.yaml) → OrgAllowedRead returns true for every tenant, so
// this helper effectively becomes the identity transform — no
// restrictions, allocation cost only.
//
// Org-aware read filter (ADR-027 / LD-6 P4c): each item's tenant is checked
// through OrgAllowedRead, so a group-member / PR / task entry belonging to a
// tenant outside the caller's org is filtered out once --rbac-org-scope-enforce
// flips — the read plane closes in lockstep with read-by-id (P4c converted this
// from the pre-P4c org-blind Allowed, removing its org_write_guard exemption).
// The would-deny records on axis="org" (read/visibility plane). tenantOrg may be
// nil (test literals) — OrgsForTenant is nil-receiver-safe → unlabeled.
//
// Generic over the slice element type so it covers the four
// near-identical filter wrappers below (members []string,
// PRInfo, async.TaskResult, etc.) without copy-paste drift on
// the loop body.
func filterByRBAC[T any](
	rbacMgr *rbac.Manager,
	tenantOrg *tenantorg.Manager,
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
		if tid == "" || OrgAllowedRead(rbacMgr, tenantOrg, p, tid, perm) {
			out = append(out, item)
		}
	}
	return out
}
