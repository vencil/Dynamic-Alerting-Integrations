package rbac

import (
	"encoding/json"
	"fmt"
	"net/http"

	chimw "github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/errcode"
)

// Middleware returns an HTTP middleware that enforces RBAC for the given
// permission on the caller ResolveIdentity attached to the request (ADR-027
// §2). It reads no header.
//
// On success, the email and groups are available via RequestEmail/RequestGroups.
// On failure, responds 401 (missing identity) or 403 (insufficient permission).
//
// tenantIDFn extracts the tenant ID from the request (may be nil — see below).
//
// tenantIDFn == nil checks `want` on the platform wildcard "*" — passed only by
// a rule whose tenants list holds the literal "*". That is the coarse route
// gate for platform-scope resources with no per-tenant ownership (saved
// views); it is org-blind, so the handler bars an org-scoped "*" rule
// (Manager.PlatformNonOrgScoped, #3160). A collection route whose handler
// filters per tenant mounts MiddlewareAnyTenant instead (#2520).
func (m *Manager) Middleware(want Permission, tenantIDFn func(*http.Request) string) func(http.Handler) http.Handler {
	return m.gate(func(w http.ResponseWriter, r *http.Request, bPrincipal *VerifiedPrincipal) bool {
		return m.authorizeTenant(w, r, bPrincipal, want, tenantIDFn)
	})
}

// MiddlewareAnyTenant is the route gate for collection endpoints whose handler
// narrows the response per tenant (#2520): the caller passes iff some rule
// could grant `want` on at least one tenant (GrantsSomeTenant — decided from
// the principal and _rbac.yaml alone, see there for why it is not "an existing
// tenant"). Everything else — identity resolution, the 401, the context the
// handler reads — is Middleware's.
//
// ⛔ Mount it only on a route whose handler decides visibility per tenant
// itself. This gate admits a caller holding ["db-a"]; a handler that returns
// platform-wide data unfiltered would hand it everything. The read-route
// manifest in cmd/server/routes_test.go names each route's filter.
func (m *Manager) MiddlewareAnyTenant(want Permission) func(http.Handler) http.Handler {
	return m.gate(func(w http.ResponseWriter, r *http.Request, bPrincipal *VerifiedPrincipal) bool {
		if m.GrantsSomeTenant(bPrincipal, want) {
			return true
		}
		m.writeForbiddenAnyTenant(w, r, want)
		return false
	})
}

// MiddlewareAuthenticated is the route gate for endpoints that answer about the
// caller itself and nothing else (GET /api/v1/me — the OIDC userinfo / GitHub
// GET /user shape): it requires an identity (401 without one, exactly as
// Middleware) and grants nothing to decide. No rule has to match: a caller
// with zero grants — no rule names it, an org-scoped rule it lacks the claim
// for, or an _rbac.yaml with no rules — passes and learns only that it holds
// no grant. The empty-rules denial is about access to tenant data, which this
// gate never guards.
//
// ⛔ Mount it only on a route whose response is derived from the caller's own
// principal (its matched rules) and names nothing else; any route that reads
// tenant or platform data takes Middleware or MiddlewareAnyTenant.
func (m *Manager) MiddlewareAuthenticated() func(http.Handler) http.Handler {
	return m.gate(func(http.ResponseWriter, *http.Request, *VerifiedPrincipal) bool { return true })
}

// gate is the shared body of Middleware, MiddlewareAnyTenant and
// MiddlewareAuthenticated: take the principal from the context (401 without
// one), let authorize decide (it writes its own denial), then attach the identity to the request context
// for the handler.
func (m *Manager) gate(authorize func(http.ResponseWriter, *http.Request, *VerifiedPrincipal) bool) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// The caller ResolveIdentity (or the dev bypass) attached on the
			// UDS plane (ADR-027 §2). No principal → 401; this gate never
			// reads a header itself.
			bPrincipal := RequestPrincipal(r)
			if bPrincipal == nil {
				writeError(w, r, http.StatusUnauthorized, errMissingIdentity.Error())
				return
			}

			if !authorize(w, r, bPrincipal) {
				return
			}

			// Attach email/groups for the RequestEmail/RequestGroups consumers;
			// the principal itself is already on the context.
			r = r.WithContext(withIdentity(r.Context(), bPrincipal.Email, bPrincipal.Groups))
			next.ServeHTTP(w, r)
		})
	}
}

// authorizeTenant is Middleware's decision: `want` on the tenant tenantIDFn
// names, or on the platform wildcard "*" when tenantIDFn is nil. On a denial it
// writes the 403 and returns false.
func (m *Manager) authorizeTenant(w http.ResponseWriter, r *http.Request, bPrincipal *VerifiedPrincipal,
	want Permission, tenantIDFn func(*http.Request) string) bool {
	// Platform-scope routes (tenantIDFn == nil) check want on wildcard "*"
	tenantID := "*"
	if tenantIDFn != nil {
		tenantID = tenantIDFn(r)
	}

	// Authorization runs off the hop-B principal through the single
	// evaluation core (ADR-027 / LD-6 P3): the principal's groups —
	// and, for rules carrying a match: block, its named claims —
	// feed the one shared ruleMatches predicate.
	//
	// ADR-027 / LD-6 P4c: per-tenant READ routes additionally gate on the
	// org axis. A read-by-id of another org's tenant is a read-only IDOR,
	// so when this is a read on a concrete tenant (want == PermRead AND the
	// resolved tenant is not the platform wildcard "*"), resolve the
	// tenant's org list and route through AllowedInOrgRead (records a
	// scope denial on axis="org", the same series as the list plane). The wildcard "*" (platform-scope routes,
	// tenantIDFn==nil) stays org-blind by design — invariant I6: a READ, or
	// any check that judges no tenant, has no tenant org list for the org
	// axis to compare against. I6 covers ONLY that: it does not make an
	// org-scoped "*" rule a platform-wide grant. An org-scoped "*" rule
	// passes this route gate, so every platform-wide write — and every read
	// of a platform resource that belongs to no tenant (saved views) — bars
	// on a rule WITHOUT org-scope in its handler (Manager.PlatformNonOrgScoped:
	// saved views read/write; PlatformAdminNonOrgScoped: the federation
	// whitelist PUT, account backfill and /audit — #3160).
	// WRITE routes (PermWrite/PermAdmin middleware) stay
	// org-blind here too; their org gate lives handler-side in
	// RequireOrgWrite, one plane per site. The guard keys on the RESOLVED
	// tenantID != "*" (not tenantIDFn != nil): TenantIDFromPath returns
	// chi.URLParam, so a crafted id="*" must remain org-blind.
	var authorized bool
	if want == PermRead && tenantID != "*" {
		var orgs []string
		if m.orgResolver != nil {
			orgs = m.orgResolver(tenantID)
		}
		authorized = m.AllowedInOrgRead(bPrincipal, tenantID, want, orgs)
	} else {
		authorized = m.Allowed(bPrincipal, tenantID, want)
	}
	if !authorized {
		m.writeForbidden(w, r, tenantID, want)
		return false
	}
	return true
}

// errorEnvelope is a package-local copy of the wire shape of
// handler.ErrorResponse, restricted to the fields this middleware emits
// (error / code / request_id / help / action). The rbac package sits in the
// domain layer and must NOT import the HTTP handler layer (depguard
// domain-no-handler ratchet, .golangci.yml), so the shape is duplicated; the
// codes are not — they are internal/errcode's (#3202). The access_test.go
// assertions pin the two shapes in sync. `error` and `code` are always set (the OpenAPI schema
// declares them required); request_id comes from the chi RequestID
// middleware, which runs router-wide before any RBAC middleware
// (cmd/server/routes.go), and is omitted when absent (bare test harnesses).
type errorEnvelope struct {
	Error     string       `json:"error"`
	Code      errcode.Code `json:"code,omitempty"`
	RequestID string       `json:"request_id,omitempty"`
	Help      string       `json:"help,omitempty"`
	Action    string       `json:"action,omitempty"`
}

// writeEnvelope stamps the request ID and renders the envelope. Mirrors
// handler.WriteErrorEnvelope (r may be nil in tests → request_id omitted).
func writeEnvelope(w http.ResponseWriter, r *http.Request, status int, env errorEnvelope) {
	if env.RequestID == "" && r != nil {
		env.RequestID = chimw.GetReqID(r.Context())
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(env)
}

// writeError writes a JSON error response in the unified envelope shape.
// The message content is unchanged from the pre-envelope bare
// {"error": msg} map — the migration only ADDS code + request_id.
func writeError(w http.ResponseWriter, r *http.Request, status int, msg string) {
	var code errcode.Code
	switch status {
	case http.StatusUnauthorized:
		code = errcode.Unauthorized
	case http.StatusForbidden:
		code = errcode.Forbidden
	default:
		// TRIPWIRE (fail-loud, dev-rules #5): `code` is a REQUIRED field of
		// the OpenAPI ErrorResponse schema, and the contract fuzz CANNOT
		// observe middleware responses (wildcard-RBAC fixture) — an empty
		// code here would ship silently. A caller introducing a new status
		// must add a code mapping above (keep it mirroring
		// handler.codeFromStatus) AND extend the envelope pins in
		// internal/handler/access_test.go. The panic is contained: the
		// router-wide chi Recoverer (cmd/server/routes.go) turns it into a
		// 500 instead of a schema-violating 4xx.
		panic(fmt.Sprintf("rbac.writeError: no error-code mapping for status %d (code is required by the OpenAPI error schema)", status))
	}
	writeEnvelope(w, r, status, errorEnvelope{Error: msg, Code: code})
}

// writeForbiddenAnyTenant is MiddlewareAnyTenant's 403: no rule grants the
// caller `want` on any tenant. Same envelope, code and help link as
// writeForbidden; the message names no tenant, because none was asked for.
func (m *Manager) writeForbiddenAnyTenant(w http.ResponseWriter, r *http.Request, want Permission) {
	writeEnvelope(w, r, http.StatusForbidden, errorEnvelope{
		Error: "insufficient permissions: no tenant grants " + string(want),
		Code:  errcode.Forbidden,
		Help:  "https://github.com/vencil/vibe-k8s-lab/blob/main/docs/governance-security.md",
		Action: "Check your _rbac.yaml group rules. Ensure your IdP group has '" + string(want) +
			"' permission for at least one tenant.",
	})
}

// writeForbidden writes a 403 response with a help link and suggested action.
// v2.5.0: Enhanced error message with guidance for RBAC troubleshooting.
// Unified-envelope migration: help/action/message preserved verbatim;
// code + request_id added.
//
// The environments[]/domains[] half of the Action appears only on a plane the
// metadata axis can deny (write/admin). writeForbidden also serves PermRead
// denials (the middleware's read-by-id and list gates), where the metadata axis
// is not bound — naming a knob that cannot change the outcome is worse than
// naming no knob at all.
func (m *Manager) writeForbidden(w http.ResponseWriter, r *http.Request, tenantID string, want Permission) {
	action := "Check your _rbac.yaml group rules. Ensure your IdP group has '" + string(want) +
		"' permission for tenant '" + tenantID + "'"
	if want != PermRead {
		action += ", and that environments[]/domains[] constraints match the tenant metadata"
	}
	writeEnvelope(w, r, http.StatusForbidden, errorEnvelope{
		Error:  "insufficient permissions for tenant " + tenantID,
		Code:   errcode.Forbidden,
		Help:   "https://github.com/vencil/vibe-k8s-lab/blob/main/docs/governance-security.md",
		Action: action + ".",
	})
}
