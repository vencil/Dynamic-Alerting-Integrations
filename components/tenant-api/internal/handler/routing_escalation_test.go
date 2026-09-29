package handler

// routing_escalation_test.go — #2325 wiring of `require_critical_escalation`
// into the write paths the parity matrix does not reach: the matrix drives
// PUT and the direct batch refusal only, so the PR-mode batch refusal and
// the advisories on every success path (direct batch op, PR-mode batch,
// PR-mode PUT, and the PR-mode no-changes PUT and batch) are pinned here.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
)

// escalationTree: a policy requiring critical escalation for t-leak and
// t-miss; profile esc-leak is compliant with one leak (routes[0], team=app
// to slack, ahead of the pagerduty route), esc-miss reaches no pagerduty.
func escalationTree() map[string]string {
	slack := "{type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}"
	return map[string]string{
		"_defaults.yaml": "defaults:\n  cpu_usage_percent: 80\n",
		"_domain_policy.yaml": "domain_policies:\n  escalation:\n    tenants: [t-leak, t-miss]\n" +
			"    constraints:\n      require_critical_escalation: true\n",
		"_routing_profiles.yaml": "routing_profiles:\n" +
			"  esc-leak:\n    receiver: " + slack + "\n" +
			"    routes:\n    - match: {team: app}\n      receiver: " + slack + "\n" +
			"    - match: {severity: critical}\n      receiver: {type: pagerduty, service_key: k}\n" +
			"  esc-miss:\n    receiver: " + slack + "\n",
		"t-leak.yaml": "tenants:\n  t-leak:\n    cpu_usage_percent: '85'\n",
		"t-miss.yaml": "tenants:\n  t-miss:\n    cpu_usage_percent: '85'\n",
	}
}

// escalationAdvisory is the leak advisory esc-leak yields for t-leak.
const escalationAdvisory = "tenant=t-leak: domain policy 'escalation': routes[0] (team=app)"

func hasAdvisory(warnings []string) bool {
	for _, w := range warnings {
		if strings.HasPrefix(w, escalationAdvisory) {
			return true
		}
	}
	return false
}

func escalationPRClient() *mockPlatformClient {
	return &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, head string, labels []string) (*platform.PRInfo, error) {
			return &platform.PRInfo{Number: 11, WebURL: "https://example/pr/11", State: "open", Title: title, HeadRef: head}, nil
		},
	}
}

// Direct batch (executeBatchOps): the leak advisory rides on the successful
// op's own warnings.
func TestBatchTenants_EscalationAdvisory_Direct(t *testing.T) {
	configDir := seedGitTree(t, escalationTree())
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
		Policy: policy.NewManager(configDir), WriteMode: WriteModeDirect}
	resp := runBatch(t, configDir, d, `[{"tenant_id":"t-leak","patch":{"_routing_profile":"esc-leak"}}]`)
	r := resultsByTenant(resp)["t-leak"]
	if r.Status != "ok" || !hasAdvisory(r.Warnings) {
		t.Errorf("t-leak = %+v, want ok with the %q advisory", r, escalationAdvisory)
	}
}

// PR-mode batch (batchTenantsPRMode): a non-compliant op is refused and left
// out; the leak advisory of an op taken into the PR is in the batch-level
// warnings.
func TestBatchTenants_Escalation_PRMode(t *testing.T) {
	configDir := seedGitTree(t, escalationTree())
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
		Policy: policy.NewManager(configDir), WriteMode: WriteModePR, PRClient: escalationPRClient(),
		PRTracker: &mockPlatformTracker{}}
	resp := runBatch(t, configDir, d, `[
		{"tenant_id":"t-miss","patch":{"_routing_profile":"esc-miss"}},
		{"tenant_id":"t-leak","patch":{"_routing_profile":"esc-leak"}}]`)
	if resp.Status != "pending_review" {
		t.Fatalf("status = %q, want pending_review: %+v", resp.Status, resp)
	}
	got := resultsByTenant(resp)
	if r := got["t-miss"]; r.Status != "error" || !strings.HasPrefix(r.Message, "policy violation: ") ||
		!strings.Contains(r.Message, "requires critical escalation") {
		t.Errorf("t-miss = %+v, want refused for require_critical_escalation", r)
	}
	if r := got["t-leak"]; r.Status != "included" {
		t.Errorf("t-leak = %+v, want included in the PR", r)
	}
	if !hasAdvisory(resp.Warnings) {
		t.Errorf("batch warnings %v, want the %q advisory", resp.Warnings, escalationAdvisory)
	}
}

// PR-mode PUT (putTenantPRMode): the leak advisory is in the pending_review
// response's warnings.
func TestPutTenant_EscalationAdvisory_PRMode(t *testing.T) {
	configDir := seedGitTree(t, escalationTree())
	rbacMgr := newRBACManager(t, policyTestRBACYAML)
	h := PutTenant(&Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: rbacMgr,
		Policy: policy.NewManager(configDir), WriteMode: WriteModePR, PRClient: escalationPRClient(),
		PRTracker: &mockPlatformTracker{}})
	body := "tenants:\n  t-leak:\n    cpu_usage_percent: '85'\n    _routing_profile: esc-leak\n"
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/t-leak", "id", "t-leak", bytes.NewBufferString(body))
	policyTestIdentity(req)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var resp PutTenantResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Status != "pending_review" || !hasAdvisory(resp.Warnings) {
		t.Errorf("response = %+v, want pending_review with the %q advisory", resp, escalationAdvisory)
	}
}

// ErrNoChanges success paths (PR-mode PUT `no_changes`, PR-mode batch
// `completed`): the write changes nothing, yet the routing on disk is judged,
// so the leak advisory still rides on the response's warnings.
const escalationLeakTenant = "tenants:\n  t-leak:\n    cpu_usage_percent: '85'\n    _routing_profile: esc-leak\n"

func escalationTreeLeakOnDisk() map[string]string {
	tree := escalationTree()
	tree["t-leak.yaml"] = escalationLeakTenant
	return tree
}

func TestPutTenant_EscalationAdvisory_PRModeNoChanges(t *testing.T) {
	configDir := seedGitTree(t, escalationTreeLeakOnDisk())
	rbacMgr := newRBACManager(t, policyTestRBACYAML)
	h := PutTenant(&Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: rbacMgr,
		Policy: policy.NewManager(configDir), WriteMode: WriteModePR, PRClient: escalationPRClient(),
		PRTracker: &mockPlatformTracker{}})
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/t-leak", "id", "t-leak", bytes.NewBufferString(escalationLeakTenant))
	policyTestIdentity(req)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var resp PutTenantResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Status != "no_changes" || !hasAdvisory(resp.Warnings) {
		t.Errorf("response = %+v, want no_changes with the %q advisory", resp, escalationAdvisory)
	}
}

func TestBatchTenants_EscalationAdvisory_PRModeNoChanges(t *testing.T) {
	configDir := seedGitTree(t, escalationTreeLeakOnDisk())
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
		Policy: policy.NewManager(configDir), WriteMode: WriteModePR, PRClient: escalationPRClient(),
		PRTracker: &mockPlatformTracker{}}
	resp := runBatch(t, configDir, d, `[{"tenant_id":"t-leak","patch":{"_routing_profile":"esc-leak"}}]`)
	if resp.Status != "completed" || !hasAdvisory(resp.Warnings) {
		t.Errorf("response = %+v, want completed with the %q advisory", resp, escalationAdvisory)
	}
}

// A `!!null x` require_critical_escalation (#2325) is None to PyYAML: the
// generator runs the policy with that constraint off and still refuses a
// forbidden slack receiver, so PUT does too — the file is not refused whole
// (yaml.v3 alone cannot decode `!!null x`, which left every constraint off).
func TestPutTenant_TaggedNullEscalationStillEnforcesForbidden(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  cpu_usage_percent: 80\n",
		"_domain_policy.yaml": "domain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n" +
			"      require_critical_escalation: !!null x\n      forbidden_receiver_types: [slack]\n",
	}
	body := "tenants:\n  t1:\n    cpu_usage_percent: '85'\n    _routing:\n" +
		"      receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}\n"
	code, resp, _ := putRoutingTenant(t, files, "t1", body)
	if code != http.StatusForbidden || !strings.Contains(resp, `"constraint":"forbidden_receiver_types"`) ||
		strings.Contains(resp, "require_critical_escalation") {
		t.Errorf("status = %d, body %s; want 403 for forbidden_receiver_types only", code, resp)
	}
}
