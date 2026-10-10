package handler

import (
	"net/http"

	"github.com/vencil/tenant-api/internal/rbac"
)

// canSeeCredentials reports whether the caller is shown a tenant's receiver
// credentials as written (#1560 option d). Everyone else reads them masked
// (internal/credmask) on every route that returns the tenant file's content:
// GET /tenants/{id} (raw_yaml, custom_alerts), GET /tenants/{id}/effective
// and POST /tenants/{id}/diff.
//
// ⛔ THE SAME PREDICATE AS THE PUT GATE (RequireOrgWrite: OrgAllowed with
// PermWrite and the tenant's on-disk metadata). So a caller who sees the
// placeholder can never PUT, and a read-modify-write cannot carry it back
// over the real value. Change one, change both.
//
// A Deps without an RBAC manager is the test-only state requireOrgWriteWithMeta
// documents; it is treated the same way here (credentials shown), so the two
// stay in lockstep there too.
func canSeeCredentials(r *http.Request, d *Deps, tenantID string) bool {
	if d.RBAC == nil {
		return true
	}
	return OrgAllowed(d.RBAC, d.TenantOrg, rbac.RequestPrincipal(r), tenantID, rbac.PermWrite, WriteScopeMeta(d.ConfigDir))
}
