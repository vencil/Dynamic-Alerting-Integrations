package handler

import (
	"encoding/json"
	"net/http"

	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
)

// PRListResponse is the response body for GET /api/v1/prs.
type PRListResponse struct {
	PendingPRs []platform.PRInfo `json:"pending_prs"`
	Count      int               `json:"count"`
}

// ListPRs handles GET /api/v1/prs
//
// Returns all pending PRs/MRs tracked by the PR tracker.
// Supports optional ?tenant={id} query parameter to filter by tenant.
// Works with both GitHub PRs and GitLab MRs via platform.Tracker interface.
//
// **v2.8.0 B-6 PR-2 hardening**: filters the returned PR list to
// only include PRs whose `TenantID` is readable by the caller.
// Without this, any PermRead user could enumerate every pending
// PR across the platform — info disclosure on tenant existence,
// who's making changes, and at what cadence. With the filter, the
// list naturally shows only the caller's accessible tenants.
//
// `?tenant=<id>` filter: still serves the requested tenant's PR
// (if any), gated by per-tenant PermRead — returns empty list (200
// with `count: 0`) if the caller can't read that tenant. We do
// NOT return 403 in this case to avoid the "tenant exists" oracle
// — empty list is indistinguishable from "no pending PR for that
// tenant", which is the intended behaviour at the API surface.
//
// @Summary     List pending PRs/MRs
// @Description Returns pending pull/merge requests created by tenant-api, filtered by caller's tenant RBAC.
// @Tags        prs
// @Produce     json
// @Param       tenant query  string false "Filter by tenant ID"
// @Success     200   {object} PRListResponse
// @Router      /api/v1/prs [get]
func (d *Deps) ListPRs() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		idpGroups := rbac.RequestGroups(r)
		tenantFilter := r.URL.Query().Get("tenant")

		var prs []platform.PRInfo
		if tenantFilter != "" {
			// Tenant-specific query: respect RBAC. If caller can't
			// read the tenant, return empty (don't 403 — that
			// would leak existence). If they can, surface the PR
			// (or empty if there isn't one).
			if d.RBAC.HasPermission(idpGroups, tenantFilter, rbac.PermRead) {
				if pr, ok := d.PRTracker.PendingPRForTenant(tenantFilter); ok {
					prs = []platform.PRInfo{pr}
				} else {
					prs = []platform.PRInfo{}
				}
			} else {
				prs = []platform.PRInfo{}
			}
		} else {
			// Bulk query: filter by per-PR tenant authz.
			all := d.PRTracker.PendingPRs()
			prs = filterAccessiblePRs(d.RBAC, idpGroups, all)
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(PRListResponse{
			PendingPRs: prs,
			Count:      len(prs),
		})
	}
}

// filterAccessiblePRs returns the subset of PRs whose TenantID is
// readable by the caller. PRs with empty TenantID are passed
// through (administrative PRs not tied to a single tenant — these
// shouldn't normally exist, but if they do they're surface-area
// the caller already saw via other endpoints).
func filterAccessiblePRs(rbacMgr *rbac.Manager, idpGroups []string, prs []platform.PRInfo) []platform.PRInfo {
	return filterByRBAC(rbacMgr, idpGroups, prs, tenantIDFromPR, rbac.PermRead)
}

// tenantIDFromPR is the per-element extractor for filterByRBAC over
// platform.PRInfo slices.
func tenantIDFromPR(p platform.PRInfo) string { return p.TenantID }
