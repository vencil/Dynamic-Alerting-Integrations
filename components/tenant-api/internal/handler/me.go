package handler

import (
	"encoding/json"
	"net/http"
	"sort"

	"github.com/vencil/tenant-api/internal/rbac"
)

// MeResponse is the response body for GET /api/v1/me
// v2.5.0: Added AccessibleEnvironments and AccessibleDomains for UI filtering hints.
type MeResponse struct {
	Email                  string              `json:"email"`
	User                   string              `json:"user"`
	Groups                 []string            `json:"groups"`
	AccessibleTenants      []string            `json:"accessible_tenants"`
	AccessibleEnvironments []string            `json:"accessible_environments,omitempty"` // nil = all
	AccessibleDomains      []string            `json:"accessible_domains,omitempty"`      // nil = all
	Permissions            map[string][]string `json:"permissions"`
}

// Me handles GET /api/v1/me
//
// Returns the current user's identity (from oauth2-proxy headers) and their
// RBAC permissions across all groups.
//
// @Summary     Get current user identity and permissions
// @Description Returns the authenticated user's email, groups, accessible tenants, and permissions.
// @Tags        identity
// @Produce     json
// @Success     200 {object} MeResponse
// @Failure     401 {object} map[string]string
// @Router      /api/v1/me [get]
func (d *Deps) Me() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		email := rbac.RequestEmail(r)
		if email == "" {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnauthorized)
			_ = json.NewEncoder(w).Encode(map[string]string{
				"error": "missing identity: X-Forwarded-Email header required",
			})
			return
		}
		groups := rbac.RequestGroups(r)

		// Extract user from email (the part before '@') for backwards compatibility
		user := email
		for i := 0; i < len(email); i++ {
			if email[i] == '@' {
				user = email[:i]
				break
			}
		}

		// Build the response
		resp := MeResponse{
			Email:       email,
			User:        user,
			Groups:      groups,
			Permissions: make(map[string][]string),
		}

		// Collect all accessible tenants and build permissions map
		accessibleTenants := make(map[string]bool)
		rbacCfg := d.RBAC.Get()

		for _, groupName := range groups {
			// Find the group rule in RBAC config
			var groupRule *rbac.GroupRule
			for i := range rbacCfg.Groups {
				if rbacCfg.Groups[i].Name == groupName {
					groupRule = &rbacCfg.Groups[i]
					break
				}
			}

			if groupRule == nil {
				continue
			}

			// Convert permissions to strings
			var perms []string
			for _, p := range groupRule.Permissions {
				perms = append(perms, string(p))
			}
			sort.Strings(perms)
			resp.Permissions[groupName] = perms

			// Collect accessible tenants
			for _, tenantPattern := range groupRule.Tenants {
				accessibleTenants[tenantPattern] = true
			}
		}

		// Convert map to sorted slice for consistent output
		for tenant := range accessibleTenants {
			resp.AccessibleTenants = append(resp.AccessibleTenants, tenant)
		}
		sort.Strings(resp.AccessibleTenants)

		// v2.5.0: Accessible environments and domains for UI filtering hints
		resp.AccessibleEnvironments = d.RBAC.AccessibleEnvironments(groups)
		resp.AccessibleDomains = d.RBAC.AccessibleDomains(groups)
		sort.Strings(resp.AccessibleEnvironments)
		sort.Strings(resp.AccessibleDomains)

		// Sort groups for consistent output
		sort.Strings(resp.Groups)

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}
}
