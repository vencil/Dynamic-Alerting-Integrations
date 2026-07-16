package handler

// Coverage for the PUT/batch domain-policy gate (ROI refactor R3, E1).
//
// Highest-risk gap this file closes: extractPatchKeys is what feeds the
// domain-policy check on PUT — if the per-key extraction or the nested-map
// flattening (flattenMap / flattenMapDepth, previously 0% covered) breaks, a
// policy-violating write is SILENTLY WAVED THROUGH (the policy manager never
// sees the key it should match). The positive assertions here therefore pin:
//   1. a NESTED `_routing.receiver.type` in the PUT body is flattened to the
//      exact dot-key the policy manager matches, and a forbidden value is
//      BLOCKED (403, nothing written to disk);
//   2. the same nested shape with a non-forbidden value passes the gate and
//      actually commits;
//   3. the batch execution path (executeBatchOps) rejects a violating op
//      per-tenant while a sibling compliant op still lands.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
)

// policyTestRBACYAML grants the "ops" IdP group write on every tenant — the
// write-capable harness for the policy-gate tests below (RBAC open mode is
// read-only, so a write test needs an explicit rule).
const policyTestRBACYAML = `groups:
  - name: ops
    tenants: ["*"]
    permissions: [read, write]
`

// policyTestIdentity stamps a write-capable identity onto the request
// (matched by policyTestRBACYAML) so the RBAC middleware sets a verified
// principal and a non-empty author email for the git commit.
func policyTestIdentity(req *http.Request) {
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "ops")
}

// financePolicyForTest returns a policy manager where tenantID belongs to a
// "finance" domain that forbids the slack receiver type.
func financePolicyForTest(tenantID string) *policy.Manager {
	return policy.NewForTest(&policy.DomainPolicyConfig{
		DomainPolicies: map[string]policy.DomainPolicy{
			"finance": {
				Description: "test finance domain",
				Tenants:     []string{tenantID},
				Constraints: policy.Constraints{
					ForbiddenReceiverTypes: []string{"slack"},
				},
			},
		},
	})
}

// --- extractPatchKeys / flattenMap pure-function tables ---

func TestExtractPatchKeys_Table(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name     string
		body     string
		tenantID string
		want     map[string]string
	}{
		{
			name:     "flat string values",
			body:     "tenants:\n  fin-db:\n    _silent_mode: \"warning\"\n    mysql_cpu: \"70\"\n",
			tenantID: "fin-db",
			want:     map[string]string{"_silent_mode": "warning", "mysql_cpu": "70"},
		},
		{
			name:     "non-string scalars stringified",
			body:     "tenants:\n  fin-db:\n    _timeout_ms: 500\n    _enabled: true\n",
			tenantID: "fin-db",
			want:     map[string]string{"_timeout_ms": "500", "_enabled": "true"},
		},
		{
			name: "nested routing map flattened to dot keys",
			body: "tenants:\n  fin-db:\n    _routing:\n      receiver:\n        type: slack\n        url: \"https://example.com/hook\"\n",
			tenantID: "fin-db",
			want: map[string]string{
				"_routing.receiver.type": "slack",
				"_routing.receiver.url":  "https://example.com/hook",
			},
		},
		{
			name:     "nested non-string scalar stringified at dot key",
			body:     "tenants:\n  fin-db:\n    _routing:\n      group_wait_s: 30\n",
			tenantID: "fin-db",
			want:     map[string]string{"_routing.group_wait_s": "30"},
		},
		{
			name:     "tenant absent from body → empty",
			body:     "tenants:\n  other-db:\n    _silent_mode: \"warning\"\n",
			tenantID: "fin-db",
			want:     map[string]string{},
		},
		{
			name:     "unparseable body → empty (never panics)",
			body:     "{{not yaml",
			tenantID: "fin-db",
			want:     map[string]string{},
		},
		{
			name:     "no tenants block → empty",
			body:     "defaults:\n  mysql_cpu: 80\n",
			tenantID: "fin-db",
			want:     map[string]string{},
		},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := extractPatchKeys([]byte(tc.body), tc.tenantID)
			if !reflect.DeepEqual(got, tc.want) {
				t.Errorf("extractPatchKeys() = %#v, want %#v", got, tc.want)
			}
		})
	}
}

func TestFlattenMap_EmptyMap(t *testing.T) {
	t.Parallel()
	out := make(map[string]string)
	flattenMap("_routing", map[string]interface{}{}, out)
	if len(out) != 0 {
		t.Errorf("flattenMap(empty) added keys: %#v", out)
	}
}

// TestFlattenMap_DepthCapStopsRecursion pins the stack-overflow guard: a
// maliciously deep nested payload must terminate with a marker value instead
// of recursing forever. (Real policy keys are ≤3 levels; anything past the cap
// only needs to be safe, not meaningful.)
func TestFlattenMap_DepthCapStopsRecursion(t *testing.T) {
	t.Parallel()
	// Build a 120-level nested map: {"k": {"k": {... "leaf"}}}.
	nested := map[string]interface{}{"k": "leaf"}
	for i := 0; i < 120; i++ {
		nested = map[string]interface{}{"k": nested}
	}
	out := make(map[string]string)
	flattenMap("_routing", nested, out)
	if len(out) != 1 {
		t.Fatalf("flattenMap(deep) produced %d keys, want exactly 1 marker: %#v", len(out), out)
	}
	for k, v := range out {
		if !strings.Contains(v, "nested too deep") {
			t.Errorf("deep-nesting value = %q (key %q), want the '<nested too deep>' marker", v, k)
		}
	}
}

// --- PUT domain-policy gate (tenant_put.go policy-violation branch) ---

func TestPutTenant_NestedRoutingPolicyViolation_Blocked(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	gw := newTestWriter(configDir)
	const tenant = "fin-db"

	h := PutTenant(&Deps{
		Writer:    gw,
		ConfigDir: configDir,
		Policy:    financePolicyForTest(tenant),
		WriteMode: WriteModeDirect,
	})
	// NESTED routing form — only reaches the policy manager if
	// extractPatchKeys+flattenMap produce the `_routing.receiver.type` dot key.
	body := bytes.NewBufferString("tenants:\n  " + tenant + ":\n    _routing:\n      receiver:\n        type: slack\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+tenant, "id", tenant, body)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusForbidden {
		t.Fatalf("PutTenant() status = %d, want 403 (policy must block nested forbidden receiver); body: %s",
			w.Code, w.Body.String())
	}
	var resp map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp["code"] != CodePolicyViolation {
		t.Errorf("code = %v, want %q", resp["code"], CodePolicyViolation)
	}
	if !strings.Contains(fmt.Sprintf("%v", resp["violations"]), "forbidden") {
		t.Errorf("violations should name the forbidden constraint, got: %v", resp["violations"])
	}
	// The gate fires BEFORE any write: nothing may land on disk.
	if _, err := os.Stat(filepath.Join(configDir, tenant+".yaml")); !os.IsNotExist(err) {
		t.Errorf("policy-violating PUT left a tenant file on disk (err=%v) — silent waive-through", err)
	}
}

func TestPutTenant_NestedRoutingPolicyCompliant_Commits(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, policyTestRBACYAML)
	const tenant = "fin-db"

	h := PutTenant(&Deps{
		Writer:    gw,
		ConfigDir: configDir,
		RBAC:      rbacMgr,
		Policy:    financePolicyForTest(tenant),
		WriteMode: WriteModeDirect,
	})
	// Same nested shape, NON-forbidden value → the policy gate must pass it
	// through and the write must actually commit (the twin proving the 403
	// above is the policy match, not a broken write path). Routed through the
	// RBAC middleware so the request carries a verified identity — the direct
	// commit path needs a non-empty author email for `git commit`.
	body := bytes.NewBufferString("tenants:\n  " + tenant + ":\n    _routing:\n      receiver:\n        type: email\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+tenant, "id", tenant, body)
	policyTestIdentity(req)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("PutTenant() status = %d, want 200 for a policy-compliant nested routing write; body: %s",
			w.Code, w.Body.String())
	}
	got, err := os.ReadFile(filepath.Join(configDir, tenant+".yaml"))
	if err != nil {
		t.Fatalf("compliant write did not land on disk: %v", err)
	}
	if !strings.Contains(string(got), "email") {
		t.Errorf("written file missing the routing value:\n%s", got)
	}
}

// --- batch execution path (executeBatchOps policy-violation branch) ---

func TestBatchTenants_PolicyViolationPerTenant(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, policyTestRBACYAML)
	const finTenant = "fin-db"

	h := BatchTenants(&Deps{
		Writer:    gw,
		ConfigDir: configDir,
		RBAC:      rbacMgr,
		Policy:    financePolicyForTest(finTenant),
		WriteMode: WriteModeDirect,
	})
	reqBody, _ := json.Marshal(BatchRequest{
		Operations: []BatchOperation{
			// Flat batch form of the routing key — the second format
			// policy.CheckWrite matches (`_routing_receiver_type`).
			{TenantID: finTenant, Patch: map[string]string{"_routing_receiver_type": "slack"}},
			// Compliant sibling op for a tenant outside the domain policy.
			{TenantID: "gen-db", Patch: map[string]string{"_silent_mode": "warning"}},
		},
	})
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBuffer(reqBody))
	req.Header.Set("Content-Type", "application/json")
	// Route through the RBAC middleware so executeBatchOps sees a verified
	// write-capable principal (an anonymous caller is denied per-op before
	// the policy check) and the git author email is non-empty.
	policyTestIdentity(req)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, nil).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("BatchTenants() status = %d, body: %s", w.Code, w.Body.String())
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(resp.Results) != 2 {
		t.Fatalf("expected 2 results, got %d: %+v", len(resp.Results), resp.Results)
	}
	byTenant := map[string]BatchResult{}
	for _, r := range resp.Results {
		byTenant[r.TenantID] = r
	}
	fin := byTenant[finTenant]
	if fin.Status != "error" || !strings.Contains(fin.Message, "domain policy violation") {
		t.Errorf("violating op result = %+v, want error with 'domain policy violation'", fin)
	}
	if gen := byTenant["gen-db"]; gen.Status != "ok" {
		t.Errorf("compliant sibling op result = %+v, want ok (policy must block per-tenant, not the batch)", gen)
	}
	// The violating tenant's file must NOT exist; the compliant one must.
	if _, err := os.Stat(filepath.Join(configDir, finTenant+".yaml")); !os.IsNotExist(err) {
		t.Errorf("policy-violating batch op left %s.yaml on disk (err=%v)", finTenant, err)
	}
	if _, err := os.Stat(filepath.Join(configDir, "gen-db.yaml")); err != nil {
		t.Errorf("compliant batch op did not write gen-db.yaml: %v", err)
	}
}
