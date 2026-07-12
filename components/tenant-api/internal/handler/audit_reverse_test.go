package handler

// GET /api/v1/audit/tenants/{id}/access-report handler tests (ADR-027 / LD-6
// P6, spec §5.4/§5.5):
//
//   - opt-in gate double assertion: the DEFAULT response must carry NEITHER
//     tenant.orgs NOR any passing_org_values; ?include=org_values must carry
//     both (round-2 axis-1 must-fix).
//   - redacted projection at the HTTP surface: the serialized body contains
//     none of the three identifier classes (rule/group names, claim keys and
//     values, org values) nor the verbatim tenant pattern.
//   - LOCKED bar (owner decision §0.1): every non-(non-org-scoped platform
//     admin) caller gets a CONSTANT 403 — status and body byte-identical
//     across existing / nonexistent / malformed tenant ids (round-1 C8: no
//     tenant-enumeration oracle) — and an org-scoped wildcard admin is such a
//     caller (the org-blind seam the bar exists to close).
//   - check-order grounding: for an authorized caller the id/param 400s DO
//     fire (proving the constant 403 above really is the bar, not a shared
//     error path).
//   - dev-bypass runtime wiring: the report's completeness gap reflects the
//     LIVE ADR-022 gauge (this package's devBypassActive atomic), not a
//     compile-time constant (round-1 C7).
//
// The report's CONTENT semantics (enumeration, dual-mode org gates, witness
// satisfiability, redact idempotence, byte-exact goldens) are pinned at the
// rbac layer (reverse_smoke_test.go / reverse_dogfood_test.go); these tests
// pin only what the HTTP surface adds.

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

const (
	auditOrgClaimHeader = "X-Audit-Org"
	auditTenantLabeled  = "db-audit-labeled" // labeled in the tenantorg fixture
	auditTenantGhost    = "db-audit-ghost"   // never onboarded
	auditOrgCovered     = "ORG-COVERED"      // ∈ tenant orgs ∩ rule claim pin
	auditOrgUnpinned    = "ORG-UNPINNED"     // tenant org NOT in any claim pin
)

// auditReverseRBACYAML: a non-org-scoped platform admin (passes the bar), an
// org-scoped wildcard admin (the seam: passes bare Allowed(p,"*",admin) but
// must fail the bar), an org-scoped match-block rule whose claims pin the org
// value domain (exercises passing_org_values + all three identifier classes
// in one grant), and a platform-wide reader (passes the route middleware,
// fails the bar). Synthetic ids per dev-rule #2.
const auditReverseRBACYAML = `groups:
  - name: audit-platform-admins
    tenants: ["*"]
    permissions: [admin]
  - name: audit-org-admins
    tenants: ["*"]
    permissions: [admin]
    org-scope: org
  - name: audit-org-ops
    match:
      groups: [audit-ops]
      claims:
        org: [` + auditOrgCovered + `, ORG-ELSEWHERE]
    tenants: ["db-audit-*"]
    permissions: [read, write]
    org-scope: org
  - name: audit-readers
    tenants: ["*"]
    permissions: [read]
`

// newAuditReverseFixture wires the handler Deps the way main.go does: RBAC via
// the production constructor (claim header declared, matching the org-scoped
// rules) and the tenantorg manager labeling exactly one tenant.
func newAuditReverseFixture(t *testing.T) *Deps {
	t.Helper()
	mgr := newRBACManagerWithClaims(t, auditReverseRBACYAML,
		map[string]string{"org": auditOrgClaimHeader})
	torg := tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{
		auditTenantLabeled: {auditOrgCovered, auditOrgUnpinned},
	}})
	return &Deps{RBAC: mgr, TenantOrg: torg}
}

// serveAuditReverse runs one request through the SAME middleware shape the
// route mounts (rbac.Middleware(PermRead, nil) — authenticated only; the bar
// is in the handler). query is appended verbatim ("" for none); orgClaim==""
// omits the org claim header.
func serveAuditReverse(t *testing.T, d *Deps, id, query, groups, orgClaim string) *httptest.ResponseRecorder {
	t.Helper()
	target := "/api/v1/audit/tenants/" + id + "/access-report" + query
	req := newRequestWithChiParam("GET", target, "id", id, nil)
	req.Header.Set("X-Forwarded-Email", "auditor@example.com")
	req.Header.Set("X-Forwarded-Groups", groups)
	if orgClaim != "" {
		req.Header.Set(auditOrgClaimHeader, orgClaim)
	}
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(GetTenantAccessReport(d), d.RBAC, rbac.PermRead, nil).ServeHTTP(w, req)
	return w
}

// TestAuditReverse_OptInOrgValuesGate is the spec §5.4 double assertion: org
// identifiers appear ONLY under ?include=org_values.
func TestAuditReverse_OptInOrgValuesGate(t *testing.T) {
	t.Parallel()
	d := newAuditReverseFixture(t)

	// Default view: no org values anywhere in the serialized body.
	w := serveAuditReverse(t, d, auditTenantLabeled, "", "audit-platform-admins", "")
	if w.Code != http.StatusOK {
		t.Fatalf("default view status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	body := w.Body.String()
	if strings.Contains(body, `"passing_org_values"`) {
		t.Errorf("default view leaks passing_org_values: %s", body)
	}
	if strings.Contains(body, `"orgs":`) {
		t.Errorf("default view leaks tenant.orgs: %s", body)
	}
	if !strings.Contains(body, `"org_status":"labeled"`) {
		t.Errorf("default view lost the org_status enum: %s", body)
	}
	if !strings.Contains(body, `"schema_version":1`) {
		t.Errorf("default view missing schema_version: %s", body)
	}

	// Opt-in: both expansions present, with the enumeration-derived
	// intersection (only the covered org passes the claim-pinned rule).
	w = serveAuditReverse(t, d, auditTenantLabeled, "?include=org_values", "audit-platform-admins", "")
	if w.Code != http.StatusOK {
		t.Fatalf("opt-in view status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	body = w.Body.String()
	if !strings.Contains(body, `"passing_org_values":["`+auditOrgCovered+`"]`) {
		t.Errorf("opt-in view missing the enumerated passing org values: %s", body)
	}
	if !strings.Contains(body, `"orgs":["`+auditOrgCovered+`","`+auditOrgUnpinned+`"]`) {
		t.Errorf("opt-in view missing sorted tenant.orgs: %s", body)
	}
}

// TestAuditReverse_RedactedView pins the redacted projection at the HTTP
// surface: none of the three identifier classes — nor the verbatim tenant
// pattern — appears in the serialized body, even when the caller ALSO asks
// for org values (redaction wins over the opt-in). Skeleton fields survive.
func TestAuditReverse_RedactedView(t *testing.T) {
	t.Parallel()
	d := newAuditReverseFixture(t)

	w := serveAuditReverse(t, d, auditTenantLabeled, "?view=redacted&include=org_values",
		"audit-platform-admins", "")
	if w.Code != http.StatusOK {
		t.Fatalf("redacted view status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	body := w.Body.String()

	// Class 1 — rule / group identifiers.
	for _, tok := range []string{"audit-platform-admins", "audit-org-admins", "audit-org-ops", "audit-readers", `"audit-ops"`} {
		if strings.Contains(body, tok) {
			t.Errorf("redacted body leaks rule/group identifier %q: %s", tok, body)
		}
	}
	// Class 2 — claim keys and values ("org" as a JSON key can only be the
	// claim key: claim_key is dropped and claims_all_of rebuilt as a count).
	if strings.Contains(body, `"org":`) || strings.Contains(body, `"claim_key"`) {
		t.Errorf("redacted body leaks claim identifiers: %s", body)
	}
	// Class 3 — org values (fixture prefix covers tenant orgs AND pins).
	if strings.Contains(body, "ORG-") {
		t.Errorf("redacted body leaks org values: %s", body)
	}
	// Verbatim tenant pattern downgraded to its kind.
	if strings.Contains(body, "db-audit-*") {
		t.Errorf("redacted body leaks the verbatim tenant pattern: %s", body)
	}
	for _, tok := range []string{`"pattern_kind"`, `"groups_count"`, `"claims_count"`, `"unsatisfiable"`, `"schema_version":1`} {
		if !strings.Contains(body, tok) {
			t.Errorf("redacted body missing skeleton field %s: %s", tok, body)
		}
	}
}

// TestAuditReverse_BarConstant403 pins the enumeration-oracle defense: a
// caller failing the bar gets 403 with a byte-identical body whether the id
// exists, was never onboarded, or is malformed — the bar runs BEFORE
// ValidateTenantID, so even the malformed id answers 403, never 400.
func TestAuditReverse_BarConstant403(t *testing.T) {
	t.Parallel()
	d := newAuditReverseFixture(t)

	ids := []string{auditTenantLabeled, auditTenantGhost, "bad..id"}
	bodies := make([]string, len(ids))
	for i, id := range ids {
		w := serveAuditReverse(t, d, id, "", "audit-readers", "")
		if w.Code != http.StatusForbidden {
			t.Fatalf("id %q: status = %d, want constant 403 (bar before id validation); body=%s",
				id, w.Code, w.Body.String())
		}
		bodies[i] = w.Body.String()
	}
	for i := 1; i < len(bodies); i++ {
		if bodies[i] != bodies[0] {
			t.Errorf("403 body for id %q differs from id %q — enumeration oracle:\n%s\nvs\n%s",
				ids[i], ids[0], bodies[i], bodies[0])
		}
	}
	if !strings.Contains(bodies[0], CodeForbidden) {
		t.Errorf("bar 403 missing machine-readable code: %s", bodies[0])
	}
}

// TestAuditReverse_OrgScopedWildcardAdmin403 pins the LOCKED bar semantics at
// the HTTP surface: an ORG-SCOPED wildcard admin — who passes the bare
// org-blind Allowed(p,"*",admin) and therefore the route middleware — still
// gets the SAME constant 403 as any other non-qualifying caller.
func TestAuditReverse_OrgScopedWildcardAdmin403(t *testing.T) {
	t.Parallel()
	d := newAuditReverseFixture(t)

	w := serveAuditReverse(t, d, auditTenantLabeled, "", "audit-org-admins", auditOrgCovered)
	if w.Code != http.StatusForbidden {
		t.Fatalf("org-scoped wildcard admin status = %d, want 403 (tightened bar); body=%s",
			w.Code, w.Body.String())
	}
	ref := serveAuditReverse(t, d, auditTenantLabeled, "", "audit-readers", "")
	if w.Body.String() != ref.Body.String() {
		t.Errorf("org-scoped admin 403 body differs from the generic bar 403 (constant-shape violation):\n%s\nvs\n%s",
			w.Body.String(), ref.Body.String())
	}
}

// TestAuditReverse_AuthorizedCaller400s grounds the check order from the other
// side: FOR a caller passing the bar, the id and query-param validations do
// fire as 400s (so the constant 403 above is genuinely the bar, not a shared
// error path swallowing everything).
func TestAuditReverse_AuthorizedCaller400s(t *testing.T) {
	t.Parallel()
	d := newAuditReverseFixture(t)

	cases := []struct {
		name  string
		id    string
		query string
	}{
		{"malformed id", "bad..id", ""},
		{"unknown view value", auditTenantLabeled, "?view=redcated"},
		{"unknown include value", auditTenantLabeled, "?include=everything"},
	}
	for _, tc := range cases {
		w := serveAuditReverse(t, d, tc.id, tc.query, "audit-platform-admins", "")
		if w.Code != http.StatusBadRequest {
			t.Errorf("%s: status = %d, want 400; body=%s", tc.name, w.Code, w.Body.String())
		}
	}

	// A never-onboarded tenant is NOT a 404 for an authorized auditor — the
	// report answers with org_status=not_onboarded (offboarded-tenant audits).
	w := serveAuditReverse(t, d, auditTenantGhost, "", "audit-platform-admins", "")
	if w.Code != http.StatusOK {
		t.Fatalf("ghost tenant status = %d, want 200 (no 404 masking); body=%s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"org_status":"not_onboarded"`) {
		t.Errorf("ghost tenant report missing not_onboarded status: %s", w.Body.String())
	}
}

// TestAuditReverse_DevBypassRuntimeStatus pins the runtime wiring of the
// completeness gap (round-1 C7): the handler injects the LIVE devBypassActive
// gauge per request. Deliberately NOT parallel — it toggles the package-global
// ADR-022 gauge and restores it before any paused parallel test resumes.
func TestAuditReverse_DevBypassRuntimeStatus(t *testing.T) {
	d := newAuditReverseFixture(t)

	SetDevBypassActive(true)
	defer SetDevBypassActive(false)
	w := serveAuditReverse(t, d, auditTenantLabeled, "", "audit-platform-admins", "")
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"status":"active"`) {
		t.Errorf("dev-bypass ON not reflected in completeness: %s", w.Body.String())
	}

	SetDevBypassActive(false)
	w = serveAuditReverse(t, d, auditTenantLabeled, "", "audit-platform-admins", "")
	if strings.Contains(w.Body.String(), `"status":"active"`) {
		t.Errorf("dev-bypass OFF still reported active (stale, not runtime-fresh): %s", w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"status":"inactive"`) {
		t.Errorf("dev-bypass OFF missing inactive status: %s", w.Body.String())
	}
}
