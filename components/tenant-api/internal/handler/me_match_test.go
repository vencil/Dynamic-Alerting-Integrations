package handler

// ADR-027 / LD-6 P3 — /api/v1/me exposure of match-block rule hits.
//
// /me's permissions map is built from rbac.RulesMatching (the same
// ruleMatches predicate authz uses), so:
//   (a) a match rule the principal's claims satisfy is listed under its rule
//       name, and its tenants join accessible_tenants;
//   (b) without the claim the rule is absent (missing claim fail-closed —
//       /me agrees with authz);
//   (c) rules sharing a name contribute the UNION of permissions/tenants
//       (degenerate-config pin: the old per-group lookup showed only the
//       FIRST same-named rule while authz already granted the union — /me
//       now tracks authz).

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sort"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

const meMatchRBACYaml = `
groups:
  - name: team-readers
    tenants: ["*"]
    permissions: [read]
  - name: org-alpha-operators
    match:
      groups: [operators]
      claims:
        org: [org-alpha]
    tenants: ["alpha-*"]
    permissions: [read, write]
`

func serveMe(t *testing.T, rbacMgr *rbac.Manager, groups, orgHeader string) MeResponse {
	t.Helper()
	handler := Me(&Deps{RBAC: rbacMgr})
	req := httptest.NewRequest("GET", "/api/v1/me", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", groups)
	if orgHeader != "" {
		req.Header.Set("X-Auth-Request-Org", orgHeader)
	}
	w := httptest.NewRecorder()
	wrapped := rbacMgr.Middleware(rbac.PermRead, nil)(handler)
	wrapped.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var resp MeResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal response: %v", err)
	}
	return resp
}

// (a)+(b): a matched match-rule appears under its rule name; without the
// claim it is absent.
func TestMe_MatchRuleExposed(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManagerWithClaims(t, meMatchRBACYaml,
		map[string]string{"org": "X-Auth-Request-Org"})

	resp := serveMe(t, rbacMgr, "team-readers, operators", "org-alpha")
	if perms, ok := resp.Permissions["org-alpha-operators"]; !ok || !reflect.DeepEqual(perms, []string{"read", "write"}) {
		t.Errorf("Permissions[org-alpha-operators] = %v (present=%v), want [read write]", perms, ok)
	}
	if perms, ok := resp.Permissions["team-readers"]; !ok || !reflect.DeepEqual(perms, []string{"read"}) {
		t.Errorf("Permissions[team-readers] = %v (present=%v), want [read]", perms, ok)
	}
	wantTenants := []string{"*", "alpha-*"}
	sort.Strings(wantTenants)
	if !reflect.DeepEqual(resp.AccessibleTenants, wantTenants) {
		t.Errorf("AccessibleTenants = %v, want %v", resp.AccessibleTenants, wantTenants)
	}

	// Same groups, no claim header → the match rule is absent (fail-closed),
	// the legacy rule remains.
	resp = serveMe(t, rbacMgr, "team-readers, operators", "")
	if _, ok := resp.Permissions["org-alpha-operators"]; ok {
		t.Errorf("Permissions lists org-alpha-operators without the claim; /me must agree with authz (fail-closed). got %v", resp.Permissions)
	}
	if _, ok := resp.Permissions["team-readers"]; !ok {
		t.Errorf("Permissions lost team-readers: %v", resp.Permissions)
	}
	if !reflect.DeepEqual(resp.AccessibleTenants, []string{"*"}) {
		t.Errorf("AccessibleTenants = %v, want [*]", resp.AccessibleTenants)
	}
}

// (c): duplicate-name rules union their permissions and tenants in /me —
// matching what authz has always granted for this degenerate config.
func TestMe_DuplicateRuleNamesUnion(t *testing.T) {
	t.Parallel()
	rbacMgr := newRBACManager(t, `
groups:
  - name: dup
    tenants: ["*"]
    permissions: [read]
  - name: dup
    tenants: ["extra-*"]
    permissions: [write]
`)

	resp := serveMe(t, rbacMgr, "dup", "")
	if perms, ok := resp.Permissions["dup"]; !ok || !reflect.DeepEqual(perms, []string{"read", "write"}) {
		t.Errorf("Permissions[dup] = %v (present=%v), want the UNION [read write]", perms, ok)
	}
	wantTenants := []string{"*", "extra-*"}
	sort.Strings(wantTenants)
	if !reflect.DeepEqual(resp.AccessibleTenants, wantTenants) {
		t.Errorf("AccessibleTenants = %v, want %v", resp.AccessibleTenants, wantTenants)
	}
}
