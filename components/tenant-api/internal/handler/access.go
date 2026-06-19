package handler

import (
	"net/http"

	"github.com/go-chi/chi/v5"
)

// AccessResponse is the body of GET /api/v1/tenants/{id}/access.
//
// It is only ever returned with Allow=true. The route's RBAC read
// middleware (rbac.Middleware(PermRead, TenantIDFromPath)) already responds
// 403 to any caller lacking read access to {id}, so reaching the handler
// means the caller is authorized. The body is deliberately minimal — a
// sibling service learns the decision from the HTTP status (200 = allow,
// 403 = deny) and never receives the tenant's config (least privilege).
type AccessResponse struct {
	// Allow is always true in a 200 response (see the type doc).
	Allow bool `json:"allow"`
	// Tenant echoes the authorized tenant ID.
	Tenant string `json:"tenant"`
	// Permission is the permission level that was checked.
	Permission string `json:"permission"`
}

// CheckTenantAccess handles GET /api/v1/tenants/{id}/access.
//
// A lightweight RBAC read-probe, purpose-built so a sibling service (the
// recipe-preview service, #657) can reuse this tenant-isolation decision
// WITHOUT re-implementing _rbac.yaml / HasPermission in a second language
// (which would risk authorization drift = a cross-tenant hole) and WITHOUT
// over-fetching the tenant config (as probing GET /tenants/{id} would). The
// route's rbac.Middleware(PermRead, TenantIDFromPath) makes the whole
// decision: 200 {allow:true} = the caller may read this tenant, 403 = it may
// not. This handler holds zero authorization logic.
//
// @Summary     Check whether the caller may read a tenant (RBAC probe)
// @Description Lightweight authorization probe that reuses the tenant read
// @Description RBAC decision: returns 200 {allow:true} when the caller has
// @Description read permission on {id}, or 403 otherwise. Purpose-built for
// @Description sibling services (e.g. the recipe-preview service, #657) so
// @Description they never re-implement RBAC or over-fetch the tenant config.
// @Tags        tenants
// @Produce     json
// @Param       id  path     string true "Tenant ID"
// @Success     200 {object} AccessResponse
// @Router      /api/v1/tenants/{id}/access [get]
func CheckTenantAccess() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// Reaching here means the middleware already authorized read on {id}.
		writeJSON(w, http.StatusOK, AccessResponse{
			Allow:      true,
			Tenant:     chi.URLParam(r, "id"),
			Permission: "read",
		})
	}
}
