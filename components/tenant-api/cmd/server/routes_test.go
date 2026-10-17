package main

// Route-manifest tripwire (ADR-027 / LD-6 P4b §5b).
//
// Every route that can MUTATE state (method ∈ {PUT, POST, DELETE, PATCH})
// must carry an entry in writeRouteManifest naming its org-scope gate
// mechanism — or an explicit, reasoned exemption. The router is built by the
// SAME buildRouter production code main() uses (conditional dependencies
// stubbed so every conditional route registers), then chi.Walk enumerates
// the write-method routes and the test fails in BOTH directions:
//
//   - a write route missing from the manifest (a NEW endpoint was added
//     without deciding its org-gate story), or
//   - a manifest entry no route registers (stale entry — the exemption /
//     mechanism claim is no longer demonstrably attached to anything).
//
// The manifest is deliberately hand-maintained: adding a write endpoint MUST
// be a conscious authorization decision, not an inherited default.

import (
	"context"
	"net/http"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
)

// Gate-mechanism labels. Free-form strings would rot; a small closed set
// keeps the manifest reviewable.
const (
	// gateTopOfHandler — handler.RequireOrgWrite at the top of the handler,
	// before body read / feature branching.
	gateTopOfHandler = "top-of-handler RequireOrgWrite"
	// gateInHandler — handler.OrgAllowed called inline where the tenant ID
	// becomes known (body / query / stored record).
	gateInHandler = "in-handler OrgAllowed"
	// gateHelperFunnel — per-tenant loop funnels through an OrgAllowed-based
	// helper (tenantsLackingPermission / executeBatchOps / runBatchPR).
	gateHelperFunnel = "helper funnel over OrgAllowed"
	// gatePlatformStar — platform-scope gate Allowed(p, "*", …): org-scope
	// deliberately does not apply to platform scope (invariant I6; the
	// org_write_guard tripwire auto-exempts the "*" literal).
	gatePlatformStar = "platform-\"*\" exemption"
	// gateReadOp — the route mutates nothing (read/preview computation via
	// POST); no write to gate.
	gateReadOp = "read-op exemption"
	// gateNonTenantData — the resource is not per-tenant data, so there is no
	// tenant whose org list could scope it; route-level "*" write gate applies.
	gateNonTenantData = "non-tenant-data exemption"
	// gateWriteOpPlatformAdmin — read-op semantics (the POST computes a
	// report and commits nothing) behind the LOCKED platform-admin bar
	// (rbac.PlatformAdminNonOrgScoped) at the top of the handler with a
	// constant 403 — the write-method mirror of gateReadPlatformAdmin.
	// Deliberately NOT gateReadOp: that label claims the route middleware
	// enforces PermRead on {id}; here the middleware only authenticates and
	// the real bar lives in the handler (ADR-027 / LD-6 P7).
	gateWriteOpPlatformAdmin = "read-op semantics; platform-admin bar in handler (non-org-scoped, constant 403)"
)

// writeRouteManifest: method+pattern → org-gate mechanism + why that
// mechanism (or exemption) is the right one. Patterns are chi's registered
// route patterns as reported by chi.Walk.
var writeRouteManifest = map[string]string{
	"PUT /api/v1/tenants/{id}/": gateTopOfHandler +
		" — site #10: covers direct commit AND PR mode before body read / policy detail leak",
	"PUT /api/v1/tenants/{id}/custom-alerts": gateTopOfHandler +
		" — site #11: authz before the PR-mode 501 so write-mode is not probeable",
	"PUT /api/v1/tenants/{id}/federation": gateInHandler +
		" — site #9: PermAdmin on path {id}, same bar as token issuance",
	"POST /api/v1/tenants/batch": gateHelperFunnel +
		" — sites #3/#4: PR-mode pre-validation inline OrgAllowed + executeBatchOps (sync/async, orgs resolved at execution time)",
	"POST /api/v1/tenants/{id}/diff": gateReadOp +
		" — diff preview only; route middleware enforces PermRead on {id}; commits nothing",
	"POST /api/v1/tenants/{id}/validate": gateReadOp +
		" — validation preview only; route middleware enforces PermRead on {id}; commits nothing",
	"PUT /api/v1/groups/{id}/": gateHelperFunnel +
		" — sites #1/#2: tenantsLackingPermission(PermWrite) over every member tenant; route gate is the any-tenant WRITE gate (#2520), groupVisible answers a hidden id with 409",
	"DELETE /api/v1/groups/{id}/": gateHelperFunnel +
		" — sites #1/#2: tenantsLackingPermission(PermWrite) over the stored member list; route gate is the any-tenant WRITE gate (#2520), a hidden group is 404",
	"POST /api/v1/groups/{id}/batch": gateHelperFunnel +
		" — site #5: one op per member through the /tenants/batch pipeline (runBatchPR inline OrgAllowed in PR mode; executeBatchOps at execution time otherwise)",
	"PUT /api/v1/views/{id}/": gateNonTenantData +
		" — saved views hold filter definitions, not tenant config; no per-tenant write decision exists, so the route keeps the platform-\"*\" write gate (#2520)",
	"DELETE /api/v1/views/{id}/": gateNonTenantData +
		" — same as PUT: view definitions are not tenant-scoped data",
	"POST /api/v1/federation/tokens/": gateInHandler +
		" — site #6: PermAdmin on body tenant_id before token issuance (data egress)",
	"DELETE /api/v1/federation/tokens/{id}": gateInHandler +
		" — site #8: PermAdmin on the token record's tenant before revocation",
	"PUT /api/v1/federation/policy/": gatePlatformStar +
		" — platform whitelist is platform-wide config; Allowed(p, \"*\", admin) is the intended org-blind gate",
	"POST /api/v1/federation/accounts/backfill": gatePlatformStar +
		" — fleet-wide AccountID backfill; same platform-admin bar as the whitelist",
	"POST /api/v1/audit/tenants/{id}/access-report/dry-run": gateWriteOpPlatformAdmin +
		" — what-if dry-run over a candidate _rbac.yaml; 403 byte-identical to the GET access-report bar (P7)",
}

// Read-plane gate-mechanism labels (ADR-027 / LD-6 P4c). Reads gate in the
// rbac middleware chokepoint, so — unlike the distributed write gates —
// the manifest records which middleware path a GET route takes.
const (
	// gateReadByIDOrg — per-tenant read (PermRead middleware + TenantIDFromPath):
	// the middleware resolves the tenant's orgs and routes through
	// AllowedInOrgRead, closing the read-by-id IDOR. Flips atomically with
	// list+write under --rbac-org-scope-enforce.
	gateReadByIDOrg = "read-by-id org gate (middleware AllowedInOrgRead)"
	// gateReadAnyTenant — collection read behind MiddlewareAnyTenant (#2520):
	// the route admits any caller who may read SOME tenant
	// (rbac.GrantsSomeTenant), so the handler named in the entry is the ONLY
	// thing between a ["db-a"] caller and other tenants' data. Every entry
	// carrying this label must name that handler-level filter. The gate is
	// org-blind (invariant I6: it judges no tenant); the per-tenant org axis
	// lives in those filters (ScopeAllowed / OrgAllowedRead). Before #2520
	// these routes read on "*" (the old gateReadListOrgBlind label).
	gateReadAnyTenant = "any-tenant read gate (MiddlewareAnyTenant) — handler filters per tenant"
	// gateReadPlatformStar — read on the platform wildcard "*" (Middleware(PermRead,
	// nil)): only a rule listing "*" passes. For data with no per-tenant owner.
	gateReadPlatformStar = "platform-\"*\" read gate (Middleware(PermRead, nil))"
	// gateReadNoAuth — infra endpoint mounted with no RBAC middleware.
	gateReadNoAuth = "no-auth infra endpoint"
	// gateReadAuthenticated — identity only (MiddlewareAuthenticated): 401
	// without an identity, no grant required. Only for a response derived
	// from the caller's own principal that names nothing else (#2520).
	gateReadAuthenticated = "authenticated-only gate (MiddlewareAuthenticated) — caller's own principal, no grant required"
	// gateReadPlatformAdmin — route middleware checks read on "*" (PermRead,
	// nil); the LOCKED authorization bar (rbac.PlatformAdminNonOrgScoped) runs
	// at the top of the handler with a constant 403 (ADR-027 / LD-6 P6).
	// Deliberately tighter than the federation-policy platform-"*" precedent:
	// an org-scoped wildcard admin fails this bar.
	gateReadPlatformAdmin = "platform-admin bar in handler (non-org-scoped; federation-policy precedent, tightened)"
)

// readRouteManifest: GET pattern → its read-plane org-gate story. Same
// bidirectional discipline as writeRouteManifest — a new GET route missing here
// (a read endpoint added without deciding its org-gate story) OR a stale entry
// fails TestReadRoutesMatchOrgGateManifest. The five gateReadByIDOrg entries are
// the read-by-id IDOR surface P4c closed.
var readRouteManifest = map[string]string{
	// per-tenant reads — org-gated by the middleware (P4c)
	"GET /api/v1/tenants/{id}/":           gateReadByIDOrg + " — primary IDOR: full tenant config incl. thresholds",
	"GET /api/v1/tenants/{id}/effective":  gateReadByIDOrg + " — merged effective config + hashes",
	"GET /api/v1/tenants/{id}/metrics":    gateReadByIDOrg + " — metric discovery catalog (Prometheus proxy)",
	"GET /api/v1/tenants/{id}/access":     gateReadByIDOrg + " — recipe-preview PEP probe (#657); 403 = DENY",
	"GET /api/v1/tenants/{id}/federation": gateReadByIDOrg + " — per-tenant federation metric subset",
	// list / wildcard reads — middleware org-blind by design
	"GET /api/v1/me":                 gateReadAuthenticated + " — caller's own identity and rule patterns (RulesMatching); names no tenant beyond the caller's own grants; zero grants → 200 with empty grants",
	"GET /api/v1/tenants":            gateReadAnyTenant + " — filterTenantsByRBAC: ScopeAllowed / ScopeAllowedUnknownMetadata per row (list plane, P4a)",
	"GET /api/v1/tenants/search":     gateReadAnyTenant + " — same filterTenantsByRBAC row filter as the list; config_derivation scoped by configDerivedMeta",
	"GET /api/v1/groups":             gateReadAnyTenant + " — ListGroups filters members + skips inaccessible groups (hasAccessibleMember/filterAccessibleMembers, org-aware P4c)",
	"GET /api/v1/groups/{id}/":       gateReadAnyTenant + " — single group; GetGroup filters members + hides inaccessible groups in-handler (groupVisible/filterAccessibleMembers, org-aware P4c; mirrors ListGroups)",
	"GET /api/v1/views":              gateReadPlatformStar + " — saved views have no owner and no per-tenant filter; stays on \"*\" until an ownership model exists (#2520)",
	"GET /api/v1/views/{id}/":        gateReadPlatformStar + " — single view; same as the list",
	"GET /api/v1/tasks/{id}":         gateReadAnyTenant + " — submitter-only (taskOwnedBy, else 404) + results org-filtered in-handler (filterTaskResults)",
	"GET /api/v1/prs":                gateReadAnyTenant + " — PR list filtered in-handler (prVisible via filterAccessiblePRs / ?tenant=)",
	"GET /api/v1/events":             gateReadAnyTenant + " — SSE stream filtered per subscriber per event (eventVisible)",
	"GET /api/v1/federation/policy/": gateReadAnyTenant + " — platform metric whitelist, names no tenant; tenant admins need it to choose their subset",
	"GET /api/v1/federation/tokens/": gateReadAnyTenant + " — token list; per-tenant admin (OrgAllowed) enforced in-handler",
	// audit surface — platform-admin bar inside the handler (P6)
	"GET /api/v1/audit/tenants/{id}/access-report": gateReadPlatformAdmin + " — reverse access report; constant 403 for every non-admin caller (no enumeration oracle)",
	// no-auth infra
	"GET /health":  gateReadNoAuth,
	"GET /ready":   gateReadNoAuth,
	"GET /metrics": gateReadNoAuth,
}

// routeStubTracker is the minimal platform.Tracker stub that makes the
// conditional /prs registration fire. Handlers never execute under chi.Walk.
type routeStubTracker struct{}

func (routeStubTracker) WatchLoop(<-chan struct{})     {}
func (routeStubTracker) PendingPRs() []platform.PRInfo { return nil }
func (routeStubTracker) PendingPRForTenant(string) (platform.PRInfo, bool) {
	return platform.PRInfo{}, false
}
func (routeStubTracker) HasPendingPR(string) bool   { return false }
func (routeStubTracker) ClaimTenant(string) bool    { return false }
func (routeStubTracker) ReleaseClaim(string)        {}
func (routeStubTracker) RegisterPR(platform.PRInfo) {}
func (routeStubTracker) LastSyncTime() time.Time    { return time.Time{} }
func (routeStubTracker) RefreshNow(context.Context) {}

// buildRouterAllRoutes builds the production router with the CONDITIONAL
// dependencies stubbed non-nil so every route registers. Zero-value stubs are
// fine: chi.Walk never invokes a handler, it only enumerates the routing tree.
func buildRouterAllRoutes(t *testing.T) *chi.Mux {
	t.Helper()
	rbacMgr, err := rbac.NewManager("", nil)
	if err != nil {
		t.Fatalf("rbac.NewManager: %v", err)
	}
	deps := &handler.Deps{
		RBAC:       rbacMgr,
		Federation: &token.Manager{}, // registers /federation/tokens/* + accounts/backfill
		PRTracker:  routeStubTracker{},
	}
	return buildRouter(routerDeps{
		Deps:      deps,
		RBAC:      rbacMgr,
		Events:    func(http.ResponseWriter, *http.Request) {},
		RateLimit: func(next http.Handler) http.Handler { return next },
	})
}

func TestWriteRoutesMatchOrgGateManifest(t *testing.T) {
	t.Parallel()

	r := buildRouterAllRoutes(t)

	writeMethods := map[string]bool{"PUT": true, "POST": true, "DELETE": true, "PATCH": true}
	seen := make(map[string]bool, len(writeRouteManifest))
	var unregistered []string

	walkErr := chi.Walk(r, func(method, route string, _ http.Handler, _ ...func(http.Handler) http.Handler) error {
		if !writeMethods[method] {
			return nil
		}
		key := method + " " + route
		if _, ok := writeRouteManifest[key]; !ok {
			unregistered = append(unregistered, key)
			return nil
		}
		seen[key] = true
		return nil
	})
	if walkErr != nil {
		t.Fatalf("chi.Walk: %v", walkErr)
	}

	sort.Strings(unregistered)
	if len(unregistered) > 0 {
		t.Errorf("write-method route(s) not in writeRouteManifest — every mutating endpoint "+
			"must declare its org-scope gate mechanism (or a reasoned exemption) in "+
			"cmd/server/routes_test.go before it ships (ADR-027 / LD-6 P4b):\n  %s",
			strings.Join(unregistered, "\n  "))
	}

	var stale []string
	for key := range writeRouteManifest {
		if !seen[key] {
			stale = append(stale, key)
		}
	}
	sort.Strings(stale)
	if len(stale) > 0 {
		t.Errorf("stale writeRouteManifest entr%s (no such registered route — remove the entry "+
			"or fix its method/pattern):\n  %s",
			map[bool]string{true: "y", false: "ies"}[len(stale) == 1],
			strings.Join(stale, "\n  "))
	}

}

// TestReadRoutesMatchOrgGateManifest is the read-plane sibling of the write
// manifest (ADR-027 / LD-6 P4c): every GET route must declare, in
// readRouteManifest, whether it is org-gated by the read-by-id middleware path
// (gateReadByIDOrg), behind the any-tenant gate with a named handler filter
// (gateReadAnyTenant), on the identity-only gate (gateReadAuthenticated), or
// on a platform gate (gateReadPlatformStar / gateReadPlatformAdmin /
// gateReadNoAuth).
// A new GET route missing from the manifest — a read endpoint added without a
// conscious org-gate decision, exactly the read-by-id IDOR shape P4c closed — or
// a stale entry, fails the test in both directions.
func TestReadRoutesMatchOrgGateManifest(t *testing.T) {
	t.Parallel()

	r := buildRouterAllRoutes(t)

	seen := make(map[string]bool, len(readRouteManifest))
	var unregistered []string
	walkErr := chi.Walk(r, func(method, route string, _ http.Handler, _ ...func(http.Handler) http.Handler) error {
		if method != "GET" {
			return nil
		}
		key := method + " " + route
		if _, ok := readRouteManifest[key]; !ok {
			unregistered = append(unregistered, key)
			return nil
		}
		seen[key] = true
		return nil
	})
	if walkErr != nil {
		t.Fatalf("chi.Walk: %v", walkErr)
	}

	sort.Strings(unregistered)
	if len(unregistered) > 0 {
		t.Errorf("GET route(s) not in readRouteManifest — every read endpoint must declare its "+
			"org-gate story (read-by-id org gate vs org-blind list/wildcard) in cmd/server/routes_test.go "+
			"before it ships (ADR-027 / LD-6 P4c):\n  %s", strings.Join(unregistered, "\n  "))
	}

	var stale []string
	for key := range readRouteManifest {
		if !seen[key] {
			stale = append(stale, key)
		}
	}
	sort.Strings(stale)
	if len(stale) > 0 {
		t.Errorf("stale readRouteManifest entr%s (no such registered GET route — remove the entry "+
			"or fix its pattern):\n  %s",
			map[bool]string{true: "y", false: "ies"}[len(stale) == 1],
			strings.Join(stale, "\n  "))
	}
}
