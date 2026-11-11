package handler

// ADR-027 §7 — read/visibility-plane org-scope harness.
//
// P4c closes the read-only IDOR/enumeration-oracle: a caller who knows another
// org's tenant id could GET /api/v1/tenants/{id} (and /diff, /validate,
// /effective, /metrics, /recipe-preview, /federation) and read its config — the
// read-by-id middleware gate (rbac.Middleware(PermRead, TenantIDFromPath) →
// AllowedInOrgRead) now org-scopes every per-tenant read.
//
// All 7 read routes mount the IDENTICAL middleware, so the gate's behavior is
// route-independent: this file pins the middleware decision with a sentinel
// (was the request let through?) across the caller × labeling matrix,
// plus two real-handler e2e groundings — GetTenant (the primary IDOR) and
// CheckTenantAccess (the recipe-preview PEP contract, #657). The route-manifest
// test (cmd/server/routes_test.go) pins that each of the 7 routes actually uses
// this middleware.
//
// Fixture mirrors org_write_enforce_test.go but wires SetOrgResolver (the
// production main.go wiring) so the middleware read gate resolves the tenant's
// orgs, and installs a per-test ScopeDeniedMetrics so the counter axis
// (org, NOT org_write) is assertable in isolation.

import (
	"bytes"
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
// org-scope, so the org axis places no restriction and must not change any
// read outcome.
const orgReadPlainRBACYAML = `groups:
  - name: ` + orgReadGroup + `
    tenants: ["*"]
    permissions: [read]
`

// newOrgReadManager builds the org-scoped RBAC manager via the production
// constructor, wires the tenant→orgs resolver (main.go wiring) and an isolated
// scope-denied recorder, and labels tenant-in / tenant-out (unlabeled
// deliberately absent).
func newOrgReadManager(t *testing.T, yaml string) (*rbac.Manager, *tenantorg.Manager, *ScopeDeniedMetrics) {
	t.Helper()
	mgr := newRBACManagerWithClaims(t, yaml, map[string]string{"org": orgReadClaimHeader})
	torg := tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{
		orgReadTenantIn:  {orgReadMemberOrg},
		orgReadTenantOut: {orgReadOutsiderOrg},
	}})
	mgr.SetOrgResolver(func(tid string) []string { orgs, _ := torg.OrgsForTenant(tid); return orgs })
	rec := &ScopeDeniedMetrics{}
	mgr.SetScopeAuditor(rec)
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
		tenant      string
		callerOrg   string
		wantStatus  int
		wantReached bool
	}{
		// org-scoped rule ------------------------------------------------------
		{"labeled_member_allow", orgReadRBACYAML, orgReadTenantIn, orgReadMemberOrg, http.StatusOK, true},
		// labeled cross-org: denied.
		{"labeled_crossorg_deny", orgReadRBACYAML, orgReadTenantOut, orgReadMemberOrg, http.StatusForbidden, false},
		// unlabeled tenant: an org-scoped rule grants nothing on it (ADR-027 §7).
		{"unlabeled_deny", orgReadRBACYAML, orgReadTenantUnlabeled, orgReadMemberOrg, http.StatusForbidden, false},
		// caller carries no org claim on a labeled tenant → denied.
		{"labeled_noclaim_deny", orgReadRBACYAML, orgReadTenantIn, "", http.StatusForbidden, false},
		// no org-scoped rule (control): the org axis restricts nothing.
		{"noorgrule_crossorg_allow", orgReadPlainRBACYAML, orgReadTenantOut, orgReadOutsiderOrg, http.StatusOK, true},
		{"noorgrule_unlabeled_allow", orgReadPlainRBACYAML, orgReadTenantUnlabeled, orgReadMemberOrg, http.StatusOK, true},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			mgr, _, _ := newOrgReadManager(t, tc.yaml)
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

// TestOrgReadGate_ScopeDeniedRecordsOrgAxis: an unlabeled read is denied and
// records its denial on axis="org" (read/visibility plane) — NOT org_write,
// NOT metadata — the same series as the list plane.
func TestOrgReadGate_ScopeDeniedRecordsOrgAxis(t *testing.T) {
	t.Parallel()
	mgr, _, rec := newOrgReadManager(t, orgReadRBACYAML)
	inner := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	wrapped := wrapWithRBACMiddleware(inner, mgr, rbac.PermRead, TenantIDFromPath)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/"+orgReadTenantUnlabeled+"/", "id", orgReadTenantUnlabeled, nil)
	req = orgReadIdentity(req, orgReadMemberOrg)
	w := httptest.NewRecorder()
	wrapped.ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Fatalf("unlabeled read status = %d, want 403; body=%s", w.Code, w.Body.String())
	}
	snap := rec.Snapshot()
	if snap["org"] != 1 {
		t.Errorf("axis=org scope denials = %d, want 1 (read/visibility plane records on org)", snap["org"])
	}
	if snap["org_write"] != 0 {
		t.Errorf("axis=org_write = %d, want 0 (a read must never count on the write-plane series)", snap["org_write"])
	}
	if snap["metadata"] != 0 {
		t.Errorf("axis=metadata = %d, want 0", snap["metadata"])
	}
}

// TestOrgReadGate_LabeledMismatchCountedOnce: a labeled cross-org read is a
// decision the org axis denied — the rule matched the caller and the tenant
// and grants read, and only its org-scope failed — so it is counted, once,
// on axis="org" (ADR-027 §7: every scope denial counts, not only unlabeled
// ones).
func TestOrgReadGate_LabeledMismatchCountedOnce(t *testing.T) {
	t.Parallel()
	mgr, _, rec := newOrgReadManager(t, orgReadRBACYAML)
	inner := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	wrapped := wrapWithRBACMiddleware(inner, mgr, rbac.PermRead, TenantIDFromPath)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/"+orgReadTenantOut+"/", "id", orgReadTenantOut, nil)
	req = orgReadIdentity(req, orgReadMemberOrg) // member org != tenant-out's org
	w := httptest.NewRecorder()
	wrapped.ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Fatalf("labeled cross-org read status = %d, want 403; body=%s", w.Code, w.Body.String())
	}
	if snap := rec.Snapshot(); snap["org"] != 1 || snap["org_write"] != 0 || snap["metadata"] != 0 {
		t.Errorf("scope denials = %v, want exactly org=1 (one decision, denied by the org axis)", snap)
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
	run := func(t *testing.T, callerOrg string) *httptest.ResponseRecorder {
		t.Helper()
		mgr, _, _ := newOrgReadManager(t, orgReadRBACYAML)
		d := &Deps{ConfigDir: dir}
		wrapped := wrapWithRBACMiddleware(GetTenant(d), mgr, rbac.PermRead, TenantIDFromPath)
		req := newRequestWithChiParam("GET", "/api/v1/tenants/"+orgReadTenantIn+"/", "id", orgReadTenantIn, nil)
		req = orgReadIdentity(req, callerOrg)
		w := httptest.NewRecorder()
		wrapped.ServeHTTP(w, req)
		return w
	}

	// Outsider is denied for a LABELED tenant, and the response body must not
	// leak the config.
	if w := run(t, orgReadOutsiderOrg); w.Code != http.StatusForbidden {
		t.Fatalf("outsider GetTenant status = %d, want 403; body=%s", w.Code, w.Body.String())
	} else if strings.Contains(w.Body.String(), "cpu") {
		t.Errorf("outsider GetTenant leaked tenant config: %s", w.Body.String())
	}
	// Member sees the config.
	w := run(t, orgReadMemberOrg)
	if w.Code != http.StatusOK {
		t.Fatalf("member GetTenant status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "cpu") {
		t.Errorf("member GetTenant did not return the tenant config: %s", w.Body.String())
	}
}

// TestOrgReadGate_RecipePreview: POST /tenants/{id}/recipe-preview is
// org-aware via the same read middleware (ADR-027 §5 — tenant-api decides
// before it asks the recipe-preview container, which reads no identity), so a
// cross-org preview is refused 403 and the container is never asked, while a
// same-org caller gets the verdict.
func TestOrgReadGate_RecipePreview(t *testing.T) {
	t.Parallel()
	run := func(t *testing.T, tenant, callerOrg string) (*httptest.ResponseRecorder, *fakePreviewer) {
		t.Helper()
		mgr, _, _ := newOrgReadManager(t, orgReadRBACYAML)
		fp := &fakePreviewer{verdict: []byte(previewVerdict)}
		wrapped := wrapWithRBACMiddleware(PreviewRecipe(&Deps{RecipePreview: fp}), mgr, rbac.PermRead, TenantIDFromPath)
		req := newRequestWithChiParam("POST", "/api/v1/tenants/"+tenant+"/recipe-preview", "id", tenant,
			bytes.NewBufferString(previewBody))
		req = orgReadIdentity(req, callerOrg)
		w := httptest.NewRecorder()
		wrapped.ServeHTTP(w, req)
		return w, fp
	}

	if w, fp := run(t, orgReadTenantIn, orgReadMemberOrg); w.Code != http.StatusOK || fp.called != 1 {
		t.Fatalf("same-org preview status = %d (asked %d); body=%s", w.Code, fp.called, w.Body.String())
	}
	if w, fp := run(t, orgReadTenantIn, orgReadOutsiderOrg); w.Code != http.StatusForbidden || fp.called != 0 {
		t.Fatalf("cross-org preview status = %d (asked %d), want 403 unasked; body=%s", w.Code, fp.called, w.Body.String())
	}
}

// TestOrgReadGate_CollectionFiltersOrgAware pins the Q2 conversion: the read
// collection filters (filterAccessibleMembers / hasAccessibleMember, and by
// extension filterByRBAC / ListPRs / task results) route through OrgAllowedRead,
// so a cross-org tenant is filtered out of a group's member list
// and a group with only cross-org members is invisible — closing the
// group-member/PR/task cross-org reference oracle in lockstep with read-by-id.
func TestOrgReadGate_CollectionFiltersOrgAware(t *testing.T) {
	t.Parallel()
	mgr, torg, _ := newOrgReadManager(t, orgReadRBACYAML)
	member := &rbac.VerifiedPrincipal{
		Groups: []string{orgReadGroup},
		Claims: map[string]string{"org": orgReadMemberOrg},
	}

	// A group referencing both an in-org and a cross-org tenant: the member
	// caller sees only the in-org member; the cross-org reference is filtered.
	members := []string{orgReadTenantIn, orgReadTenantOut}
	got := filterAccessibleMembers(mgr, torg, member, members)
	if len(got) != 1 || got[0] != orgReadTenantIn {
		t.Errorf("filterAccessibleMembers = %v, want [%s] (cross-org member must be filtered)", got, orgReadTenantIn)
	}

	// A group whose ONLY member is cross-org is invisible (no accessible member).
	if hasAccessibleMember(mgr, torg, member, []string{orgReadTenantOut}) {
		t.Errorf("hasAccessibleMember(cross-org only) = true, want false")
	}
	// A group with an in-org member stays visible.
	if !hasAccessibleMember(mgr, torg, member, []string{orgReadTenantOut, orgReadTenantIn}) {
		t.Errorf("hasAccessibleMember(has in-org member) = false, want true")
	}
}
