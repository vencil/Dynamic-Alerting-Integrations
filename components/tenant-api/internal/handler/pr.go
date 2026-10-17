package handler

import (
	"net/http"

	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
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
// #3123: a batch PR/MR covers several tenants, and is shown only to a caller
// who may read EVERY one of them (prVisible) — on both the bulk and the
// `?tenant=` path. One whose tenants are unknown (created before a restart),
// like any PR not attributed to a tenant, is shown only to a caller who reads
// every tenant. The route's read check on "*" is no isolation barrier: an
// org-scoped `tenants: ["*"]` rule passes it.
//
// @Summary     List pending PRs/MRs
// @Description Returns pending pull/merge requests created by tenant-api, filtered by caller's tenant RBAC.
// @Description A batch PR/MR (several tenants; `tenant_id` empty, `tenants` lists them) is returned only to callers who may read every tenant it covers.
// @Description A batch PR/MR whose tenants tenant-api did not record (e.g. created before a restart) is returned only to callers who may read every tenant.
// @Tags        prs
// @Produce     json
// @Param       tenant query  string false "Filter by tenant ID"
// @Success     200   {object} PRListResponse
// @Router      /api/v1/prs [get]
func ListPRs(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		p := rbac.RequestPrincipal(r)
		tenantFilter := r.URL.Query().Get("tenant")

		var prs []platform.PRInfo
		if tenantFilter != "" {
			// Tenant-specific query: respect RBAC + org scope (ADR-027 / LD-6
			// P4c). If caller can't read the tenant, return empty (don't 403 —
			// that would leak existence). If they can, surface the PR (or empty
			// if there isn't one). OrgAllowedRead records on axis="org".
			if OrgAllowedRead(d.RBAC, d.TenantOrg, p, tenantFilter, rbac.PermRead) {
				if pr, ok := d.PRTracker.PendingPRForTenant(tenantFilter); ok && prVisible(d.RBAC, d.TenantOrg, p, pr) {
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
			prs = filterAccessiblePRs(d.RBAC, d.TenantOrg, p, all)
		}

		writeJSON(w, http.StatusOK, PRListResponse{
			PendingPRs: prs,
			Count:      len(prs),
		})
	}
}

// filterAccessiblePRs returns the subset of PRs the caller may see
// (prVisible).
func filterAccessiblePRs(rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, p *rbac.VerifiedPrincipal, prs []platform.PRInfo) []platform.PRInfo {
	out := make([]platform.PRInfo, 0, len(prs))
	for _, pr := range prs {
		if prVisible(rbacMgr, tenantOrg, p, pr) {
			out = append(out, pr)
		}
	}
	return out
}

// prVisibleTo is prVisible for a write handler's Deps. A Deps without an RBAC
// manager exists only in tests (see requireOrgWriteWithMeta) and sees the PR.
func prVisibleTo(d *Deps, p *rbac.VerifiedPrincipal, pr platform.PRInfo) bool {
	if d.RBAC == nil {
		return true
	}
	return prVisible(d.RBAC, d.TenantOrg, p, pr)
}

// prVisible is the one visibility decision for a pending PR/MR (#3123):
//   - a batch PR/MR with a recorded tenant list: the caller must be able to
//     read EVERY covered tenant — its title, body and URL describe them all;
//   - a single-tenant PR/MR: the caller must be able to read that tenant;
//   - anything else — a batch PR/MR whose tenants are unknown, or a PR not
//     attributed to any tenant — only a caller who reads every tenant
//     (readsEveryTenant). These used to pass through filterByRBAC's
//     empty-tenant case to every reader, and a batch branch used to parse to
//     the tenant "batch", readable by any rule matching it.
func prVisible(rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, p *rbac.VerifiedPrincipal, pr platform.PRInfo) bool {
	if len(pr.Tenants) > 0 {
		for _, id := range pr.Tenants {
			if !OrgAllowedRead(rbacMgr, tenantOrg, p, id, rbac.PermRead) {
				return false
			}
		}
		return true
	}
	if pr.TenantID == "" || platform.IsBatchBranch(pr.HeadRef) {
		return readsEveryTenant(rbacMgr, p)
	}
	return OrgAllowedRead(rbacMgr, tenantOrg, p, pr.TenantID, rbac.PermRead)
}
