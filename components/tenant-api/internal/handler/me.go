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
	// OrgClaimKeys are the org-scope claim keys of the rules THIS caller
	// matches (ADR-027 / LD-6 P7) — a caller-relative fact ("which of my
	// claims act as an org axis for me"), NOT "does the platform run an org
	// axis". Intersected with the principal's present claims by construction:
	// the redacted reverse report strips claim keys as customer-recognizable
	// identifiers (they name the identity axes a deployment runs), so /me
	// must not reveal an org-scope key name the caller does not already
	// carry a value for. Sorted, de-duplicated; omitempty keeps the zero-org
	// (and pre-P7) response body byte-identical.
	OrgClaimKeys []string `json:"org_claim_keys,omitempty"`
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
		p := rbac.RequestPrincipal(r)
		if p != nil {
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

		// Collect all accessible tenants and build permissions map off the
		// rules the principal matches (ADR-027 / LD-6 P3): rbac.RulesMatching
		// runs the SAME ruleMatches predicate authz uses, so /me lists
		// match-block rule hits (key = rule name) exactly like legacy
		// name-matched rules, and no rule-matching semantics live outside the
		// rbac package. Rules sharing a name contribute the UNION of their
		// permissions/tenants — for a normal config (unique names, no match
		// blocks) the output is byte-identical to the old per-group lookup;
		// for the degenerate duplicate-name config the old code showed only
		// the FIRST rule while authz already granted the union, so /me now
		// tracks authz more closely.
		//
		// p may be nil only on the legacy no-middleware fallback above, where
		// email is also empty → the 401 has already returned; RulesMatching
		// is nil-safe regardless (anonymous matches no rule).
		accessibleTenants := make(map[string]bool)
		permsByRule := make(map[string]map[string]bool)
		orgClaimKeys := make(map[string]bool)
		for _, rule := range d.RBAC.RulesMatching(p) {
			set, ok := permsByRule[rule.Name]
			if !ok {
				set = make(map[string]bool)
				permsByRule[rule.Name] = set
			}
			for _, perm := range rule.Permissions {
				set[string(perm)] = true
			}
			for _, tenantPattern := range rule.Tenants {
				accessibleTenants[tenantPattern] = true
			}
			// P7: collect the org-scope keys of matched rules, but only when
			// the caller carries a value for the key (`claims` is the local
			// principal-claims map, nil-safe) — the ∩ p.Claims guard above
			// the OrgClaimKeys field doc. A matched org-scoped rule whose
			// claim the caller lacks stays invisible here.
			if rule.OrgScope != "" && claims[rule.OrgScope] != "" {
				orgClaimKeys[rule.OrgScope] = true
			}
		}
		for name, set := range permsByRule {
			// A matched rule with no permissions keeps a nil slice, preserving
			// the pre-P3 JSON rendering (`"<name>": null`).
			var perms []string
			for perm := range set {
				perms = append(perms, perm)
			}
			sort.Strings(perms)
			resp.Permissions[name] = perms
		}

		// Convert map to sorted slice for consistent output
		for tenant := range accessibleTenants {
			resp.AccessibleTenants = append(resp.AccessibleTenants, tenant)
		}
		sort.Strings(resp.AccessibleTenants)

		// Zero collected keys keep the nil slice → omitempty drops the field.
		for key := range orgClaimKeys {
			resp.OrgClaimKeys = append(resp.OrgClaimKeys, key)
		}
		sort.Strings(resp.OrgClaimKeys)

		// v2.5.0: Accessible environments and domains for UI filtering hints
		resp.AccessibleEnvironments = d.RBAC.AccessibleEnvironmentsFor(p)
		resp.AccessibleDomains = d.RBAC.AccessibleDomainsFor(p)
		sort.Strings(resp.AccessibleEnvironments)
		sort.Strings(resp.AccessibleDomains)

		// Sort groups for consistent output
		sort.Strings(resp.Groups)

		writeJSON(w, http.StatusOK, resp)
	}
}
