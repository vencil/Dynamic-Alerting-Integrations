package handler

// ADR-027 / LD-6 P4c — read/visibility-plane org-scope harness.
//
// P4c closes the read-only IDOR/enumeration-oracle: a caller who knows another
// org's tenant id could GET /api/v1/tenants/{id} (and /diff, /validate,
// /effective, /metrics, /access, /federation) and read its config — the
// read-by-id middleware gate (rbac.Middleware(PermRead, TenantIDFromPath) →
// AllowedInOrgRead) now org-scopes every per-tenant read.
//
// All 7 read routes mount the IDENTICAL middleware, so the gate's behavior is
// route-independent: this file pins the middleware decision with a sentinel
// (was the request let through?) across the caller × labeling × mode matrix,
// plus two real-handler e2e groundings — GetTenant (the primary IDOR) and
// CheckTenantAccess (the recipe-preview PEP contract, #657). The route-manifest
// test (cmd/server/routes_test.go) pins that each of the 7 routes actually uses
// this middleware.
//
// Fixture mirrors org_write_enforce_test.go but wires SetOrgResolver (the
// production main.go wiring) so the middleware read gate resolves the tenant's
// orgs, and installs a per-test ScopeWouldDenyMetrics so the counter axis
// (org, NOT org_write) is assertable in isolation.

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

const (
	orgReadClaimHeader     = "X-Auth-Request-Org"
	orgReadGroup           = "org-read-users"
	orgReadTenantIn        = "tenant-read-in"        // labeled with the member org
	orgReadTenantOut       = "tenant-read-out"       // labeled with a different org
	orgReadTenantUnlabeled = "tenant-read-unlabeled" // absent from _tenant_orgs.yaml
	orgReadMemberOrg       = "ORG-ALPHA"
	orgReadOutsiderOrg     = "ORG-BETA"
)

// orgReadRBACYAML: one org-scoped rule (tenants ["*"], full perms, org-scope).
const orgReadRBACYAML = `groups:
  - name: ` + orgReadGroup + `
    tenants: ["*"]
    permissions: [read, write, admin]
    org-scope: org
`

// orgReadPlainRBACYAML: the byte-identical control — a read grant with NO
// org-scope, so the org axis degenerates to (true,true) and enforce mode must
// not change any read outcome.
const orgReadPlainRBACYAML = `groups:
  - name: ` + orgReadGroup + `
    tenants: ["*"]
    permissions: [read]
`

// newOrgReadManager builds the enforce/shadow org-scoped RBAC manager via the
// production constructor, wires the tenant→orgs resolver (main.go wiring) and an
// isolated would-deny recorder, and labels tenant-in / tenant-out (unlabeled
// deliberately absent).
func newOrgReadManager(t *testing.T, yaml string, enforce bool) (*rbac.Manager, *tenantorg.Manager, *ScopeWouldDenyMetrics) {
	t.Helper()
	mgr := newRBACManagerWithClaims(t, yaml, map[string]string{"org": orgReadClaimHeader})
	torg := tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{
		orgReadTenantIn:  {orgReadMemberOrg},
		orgReadTenantOut: {orgReadOutsiderOrg},
	}})
	mgr.SetOrgResolver(func(tid string) []string { orgs, _ := torg.OrgsForTenant(tid); return orgs })
	rec := &ScopeWouldDenyMetrics{}
	mgr.SetScopeAuditor(rec)
	if enforce {
		mgr.EnableOrgScopeEnforce()
	}
	return mgr, torg, rec
}

// orgReadIdentity stamps the org-scoped caller identity. callerOrg=="" omits the
// org claim header (the unwired-claim case).
func orgReadIdentity(req *http.Request, callerOrg string) *http.Request {
	req.Header.Set("X-Forwarded-Email", "org-read-caller@example.com")
	req.Header.Set("X-Forwarded-Groups", orgReadGroup)
	if callerOrg != "" {
		req.Header.Set(orgReadClaimHeader, callerOrg)
	}
	return req
}

// TestOrgReadGate_MiddlewareMatrix pins the read-by-id middleware decision. The
// sentinel writes 200 only if the middleware let the request through — so
// reached==false proves the gate blocked the read BEFORE any handler could
// serve the tenant config (the IDOR closure).
func TestOrgReadGate_MiddlewareMatrix(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name        string
		yaml        string
		enforce     bool
		tenant      string
		callerOrg   string
		wantStatus  int
		wantReached bool
	}{
		// org-scoped rule ------------------------------------------------------
		{"labeled_member_enforce_allow", orgReadRBACYAML, true, orgReadTenantIn, orgReadMemberOrg, http.StatusOK, true},
		{"labeled_member_shadow_allow", orgReadRBACYAML, false, orgReadTenantIn, orgReadMemberOrg, http.StatusOK, true},
		// labeled cross-org: denied in BOTH modes ((false,false)) — labeling,
		// not the enforce flip, is what closes the IDOR for a labeled tenant.
		{"labeled_crossorg_enforce_deny", orgReadRBACYAML, true, orgReadTenantOut, orgReadMemberOrg, http.StatusForbidden, false},
		{"labeled_crossorg_shadow_deny", orgReadRBACYAML, false, orgReadTenantOut, orgReadMemberOrg, http.StatusForbidden, false},
		// unlabeled tenant: shadow leniency vs enforce fail-closed.
		{"unlabeled_shadow_allow", orgReadRBACYAML, false, orgReadTenantUnlabeled, orgReadMemberOrg, http.StatusOK, true},
		{"unlabeled_enforce_deny", orgReadRBACYAML, true, orgReadTenantUnlabeled, orgReadMemberOrg, http.StatusForbidden, false},
		// caller carries no org claim on a labeled tenant → (false,false) both modes.
		{"labeled_noclaim_enforce_deny", orgReadRBACYAML, true, orgReadTenantIn, "", http.StatusForbidden, false},
		// no org-scoped rule (byte-identical control): org axis (true,true) —
		// enforce must not change ANY read outcome.
		{"noorgrule_crossorg_enforce_allow", orgReadPlainRBACYAML, true, orgReadTenantOut, orgReadOutsiderOrg, http.StatusOK, true},
		{"noorgrule_unlabeled_enforce_allow", orgReadPlainRBACYAML, true, orgReadTenantUnlabeled, orgReadMemberOrg, http.StatusOK, true},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			mgr, _, _ := newOrgReadManager(t, tc.yaml, tc.enforce)
			reached := false
			inner := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				reached = true
				w.WriteHeader(http.StatusOK)
			})
			wrapped := wrapWithRBACMiddleware(inner, mgr, rbac.PermRead, TenantIDFromPath)
			req := newRequestWithChiParam("GET", "/api/v1/tenants/"+tc.tenant+"/", "id", tc.tenant, nil)
			req = orgReadIdentity(req, tc.callerOrg)
			w := httptest.NewRecorder()
			wrapped.ServeHTTP(w, req)
			if w.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d; body=%s", w.Code, tc.wantStatus, w.Body.String())
			}
			if reached != tc.wantReached {
				t.Fatalf("handler reached = %v, want %v (a blocked read must never reach the handler)", reached, tc.wantReached)
			}
		})
	}
}

// TestOrgReadGate_WouldDenyRecordsOrgAxis: an unlabeled read under SHADOW is
// allowed but records its would-deny on axis="org" (read/visibility plane) —
// NOT org_write, NOT metadata — so the enforce-flip soak criterion's existing
// increase({axis="org"})==0 clause auto-covers read-by-id.
func TestOrgReadGate_WouldDenyRecordsOrgAxis(t *testing.T) {
	t.Parallel()
	mgr, _, rec := newOrgReadManager(t, orgReadRBACYAML, false) // shadow
	inner := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	wrapped := wrapWithRBACMiddleware(inner, mgr, rbac.PermRead, TenantIDFromPath)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/"+orgReadTenantUnlabeled+"/", "id", orgReadTenantUnlabeled, nil)
	req = orgReadIdentity(req, orgReadMemberOrg)
	w := httptest.NewRecorder()
	wrapped.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("shadow unlabeled read status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	snap := rec.Snapshot()
	if snap["org"] != 1 {
		t.Errorf("axis=org would-deny = %d, want 1 (read/visibility plane records on org)", snap["org"])
	}
	if snap["org_write"] != 0 {
		t.Errorf("axis=org_write = %d, want 0 (a read must never pollute the write-plane soak counter)", snap["org_write"])
	}
	if snap["metadata"] != 0 {
		t.Errorf("axis=metadata = %d, want 0", snap["metadata"])
	}
}

// TestOrgReadGate_LabeledMismatchNotCounted: a labeled cross-org read is
// (false,false) — denied in shadow too — so it is a shadow-FALSE decision and
// recordScopeShadowGap must NOT count it (only unlabeled leniency counts, or the
// soak counter would never reach zero).
func TestOrgReadGate_LabeledMismatchNotCounted(t *testing.T) {
	t.Parallel()
	mgr, _, rec := newOrgReadManager(t, orgReadRBACYAML, false) // shadow
	inner := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	wrapped := wrapWithRBACMiddleware(inner, mgr, rbac.PermRead, TenantIDFromPath)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/"+orgReadTenantOut+"/", "id", orgReadTenantOut, nil)
	req = orgReadIdentity(req, orgReadMemberOrg) // member org != tenant-out's org
	w := httptest.NewRecorder()
	wrapped.ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Fatalf("labeled cross-org shadow read status = %d, want 403; body=%s", w.Code, w.Body.String())
	}
	if snap := rec.Snapshot(); snap["org"] != 0 {
		t.Errorf("axis=org would-deny = %d, want 0 (a labeled cross-org denial is shadow-false and must not be counted)", snap["org"])
	}
}

// TestOrgReadGate_GetTenantIDORClosed grounds the primary IDOR: an outsider who
// knows the id of a labeled tenant gets 403 and NEVER receives the tenant config
// (raw YAML / thresholds), while the member gets 200 with the config.
func TestOrgReadGate_GetTenantIDORClosed(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		orgReadTenantIn + ".yaml": "tenants:\n  " + orgReadTenantIn + ":\n    cpu: \"80\"\n",
	})
	run := func(t *testing.T, callerOrg string, enforce bool) *httptest.ResponseRecorder {
		t.Helper()
		mgr, _, _ := newOrgReadManager(t, orgReadRBACYAML, enforce)
		d := &Deps{ConfigDir: dir}
		wrapped := wrapWithRBACMiddleware(GetTenant(d), mgr, rbac.PermRead, TenantIDFromPath)
		req := newRequestWithChiParam("GET", "/api/v1/tenants/"+orgReadTenantIn+"/", "id", orgReadTenantIn, nil)
		req = orgReadIdentity(req, callerOrg)
		w := httptest.NewRecorder()
		wrapped.ServeHTTP(w, req)
		return w
	}

	// Outsider is denied in BOTH modes for a LABELED tenant, and the response
	// body must not leak the config.
	for _, enforce := range []bool{false, true} {
		w := run(t, orgReadOutsiderOrg, enforce)
		if w.Code != http.StatusForbidden {
			t.Fatalf("outsider GetTenant (enforce=%v) status = %d, want 403; body=%s", enforce, w.Code, w.Body.String())
		}
		if strings.Contains(w.Body.String(), "cpu") {
			t.Errorf("outsider GetTenant (enforce=%v) leaked tenant config: %s", enforce, w.Body.String())
		}
	}
	// Member sees the config.
	w := run(t, orgReadMemberOrg, true)
	if w.Code != http.StatusOK {
		t.Fatalf("member GetTenant status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "cpu") {
		t.Errorf("member GetTenant did not return the tenant config: %s", w.Body.String())
	}
}

// TestOrgReadGate_AccessProbeContract grounds the recipe-preview PEP contract
// (#657): GET /tenants/{id}/access is org-aware via the same middleware, so a
// cross-org would-fire preview is denied (403) — the recipe-preview PEP maps any
// non-200 to DENY — while a same-org caller gets 200 {allow:true}. /access holds
// zero authz logic; the gate is entirely the middleware.
func TestOrgReadGate_AccessProbeContract(t *testing.T) {
	t.Parallel()
	run := func(t *testing.T, tenant, callerOrg string) *httptest.ResponseRecorder {
		t.Helper()
		mgr, _, _ := newOrgReadManager(t, orgReadRBACYAML, true) // enforce
		wrapped := wrapWithRBACMiddleware(CheckTenantAccess(), mgr, rbac.PermRead, TenantIDFromPath)
		req := newRequestWithChiParam("GET", "/api/v1/tenants/"+tenant+"/access", "id", tenant, nil)
		req = orgReadIdentity(req, callerOrg)
		w := httptest.NewRecorder()
		wrapped.ServeHTTP(w, req)
		return w
	}

	// Same-org: 200 allow (recipe-preview proceeds).
	if w := run(t, orgReadTenantIn, orgReadMemberOrg); w.Code != http.StatusOK {
		t.Fatalf("same-org /access status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	// Cross-org: 403 (recipe-preview treats as DENY — never 404, /access is
	// existence-blind so 403 leaks nothing).
	if w := run(t, orgReadTenantIn, orgReadOutsiderOrg); w.Code != http.StatusForbidden {
		t.Fatalf("cross-org /access status = %d, want 403; body=%s", w.Code, w.Body.String())
	}
}

// TestOrgReadGate_CollectionFiltersOrgAware pins the Q2 conversion: the read
// collection filters (filterAccessibleMembers / hasAccessibleMember, and by
// extension filterByRBAC / ListPRs / task results) route through OrgAllowedRead,
// so under enforce a cross-org tenant is filtered out of a group's member list
// and a group with only cross-org members is invisible — closing the
// group-member/PR/task cross-org reference oracle in lockstep with read-by-id.
func TestOrgReadGate_CollectionFiltersOrgAware(t *testing.T) {
	t.Parallel()
	mgr, torg, _ := newOrgReadManager(t, orgReadRBACYAML, true) // enforce
	member := &rbac.VerifiedPrincipal{
		Groups: []string{orgReadGroup},
		Claims: map[string]string{"org": orgReadMemberOrg},
	}

	// A group referencing both an in-org and a cross-org tenant: the member
	// caller sees only the in-org member; the cross-org reference is filtered.
	members := []string{orgReadTenantIn, orgReadTenantOut}
	got := filterAccessibleMembers(mgr, torg, member, members)
	if len(got) != 1 || got[0] != orgReadTenantIn {
		t.Errorf("filterAccessibleMembers = %v, want [%s] (cross-org member must be filtered under enforce)", got, orgReadTenantIn)
	}

	// A group whose ONLY member is cross-org is invisible (no accessible member).
	if hasAccessibleMember(mgr, torg, member, []string{orgReadTenantOut}) {
		t.Errorf("hasAccessibleMember(cross-org only) = true, want false under enforce")
	}
	// A group with an in-org member stays visible.
	if !hasAccessibleMember(mgr, torg, member, []string{orgReadTenantOut, orgReadTenantIn}) {
		t.Errorf("hasAccessibleMember(has in-org member) = false, want true")
	}
}
