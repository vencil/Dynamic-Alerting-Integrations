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
	// helper (tenantsLackingPermission / executeBatchOps / executeGroupBatchOps).
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
		" — sites #1/#2: tenantsLackingPermission(PermWrite) over every member tenant",
	"DELETE /api/v1/groups/{id}/": gateHelperFunnel +
		" — sites #1/#2: tenantsLackingPermission(PermWrite) over the stored member list",
	"POST /api/v1/groups/{id}/batch": gateHelperFunnel +
		" — site #5: executeGroupBatchOps checks OrgAllowed per member at execution time",
	"PUT /api/v1/views/{id}/": gateNonTenantData +
		" — saved views hold filter definitions, not tenant config; no per-tenant write decision exists",
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

func TestWriteRoutesMatchOrgGateManifest(t *testing.T) {
	t.Parallel()

	rbacMgr, err := rbac.NewManager("", nil)
	if err != nil {
		t.Fatalf("rbac.NewManager: %v", err)
	}

	// Stub the CONDITIONAL dependencies non-nil so buildRouter registers
	// every route. Zero-value stubs are fine: chi.Walk never invokes a
	// handler, it only enumerates the routing tree.
	deps := &handler.Deps{
		RBAC:       rbacMgr,
		Federation: &token.Manager{}, // registers /federation/tokens/* + accounts/backfill
		PRTracker:  routeStubTracker{},
	}

	r := buildRouter(routerDeps{
		Deps:      deps,
		RBAC:      rbacMgr,
		Events:    func(http.ResponseWriter, *http.Request) {},
		RateLimit: func(next http.Handler) http.Handler { return next },
	})

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
