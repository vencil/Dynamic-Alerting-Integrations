package handler

import (
	"net/http"
	"sort"

	"github.com/vencil/tenant-api/internal/rbac"
)

// MeResponse is the response body for GET /api/v1/me
// v2.5.0: Added AccessibleEnvironments and AccessibleDomains for UI filtering hints.
// ADR-027 / LD-6 P2: Added Claims (named verified claims off the request principal).
type MeResponse struct {
	Email                  string              `json:"email"`
	User                   string              `json:"user"`
	Groups                 []string            `json:"groups"`
	AccessibleTenants      []string            `json:"accessible_tenants"`
	AccessibleEnvironments []string            `json:"accessible_environments,omitempty"` // nil = all
	AccessibleDomains      []string            `json:"accessible_domains,omitempty"`      // nil = all
	Permissions            map[string][]string `json:"permissions"`
	// Claims are the named verified claims carried by the request principal
	// (ADR-027 / LD-6 P2; declared via --identity-claim-headers). omitempty:
	// with no claim axes declared (nil map) the key is absent, keeping the
	// zero-config response body byte-identical to pre-P2. Go serialises map
	// keys sorted, so the rendering is deterministic.
	Claims map[string]string `json:"claims,omitempty"`
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
func Me(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// ADR-027 / LD-6 P2: principal-first. The VerifiedPrincipal the RBAC
		// middleware attached is the identity SSOT (email / groups / claims);
		// the legacy context accessors remain only as a fallback for requests
		// that never passed through Middleware (handler-direct tests or a
		// misconfigured chain), where the empty email yields the same 401 as
		// before. This also fixes the drift where groups were read from
		// RequestGroups while the principal was already available.
		var (
			email  string
			groups []string
			claims map[string]string
		)
		if p := rbac.RequestPrincipal(r); p != nil {
			email = p.Email
			groups = p.Groups
			claims = p.Claims
		} else {
			email = rbac.RequestEmail(r)
			groups = rbac.RequestGroups(r)
		}
		if email == "" {
			writeJSON(w, http.StatusUnauthorized, map[string]string{
				"error": "missing identity: X-Forwarded-Email header required",
			})
			return
		}
		// TRK-228: schemathesis caught nil-vs-array drift. Normalise so JSON
		// encodes [] not null — the spec declares these fields as `array`.
		if groups == nil {
			groups = []string{}
		}

		// Extract user from email (the part before '@') for backwards compatibility
		user := email
		for i := 0; i < len(email); i++ {
			if email[i] == '@' {
				user = email[:i]
				break
			}
		}

		// Build the response. AccessibleTenants explicitly starts as a
		// non-nil empty slice so users with no group memberships still see
		// `"accessible_tenants": []` rather than `null` (TRK-228).
		resp := MeResponse{
			Email:             email,
			User:              user,
			Groups:            groups,
			AccessibleTenants: []string{},
			Permissions:       make(map[string][]string),
			Claims:            claims,
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

		writeJSON(w, http.StatusOK, resp)
	}
}
