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
	"slices"
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

// leakAdvisory is the full leak advisory esc-leak yields for tenant, word
// for word. Every test here compares the whole warnings list against it
// (count and order included): a contains check cannot see a duplicate.
func leakAdvisory(tenant string) string {
	return "tenant=" + tenant + ": domain policy 'escalation': routes[0] (team=app) receiver type 'slack'" +
		" catches alerts with severity=critical, team=app before any receiver of type pagerduty does, so they never reach one"
}

// wantWarnings fails unless got is exactly want, in order.
func wantWarnings(t *testing.T, got []string, want ...string) {
	t.Helper()
	if !slices.Equal(got, want) {
		t.Errorf("warnings (%d) =\n  %q\nwant (%d)\n  %q", len(got), got, len(want), want)
	}
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
	if r.Status != "ok" {
		t.Errorf("t-leak = %+v, want ok", r)
	}
	wantWarnings(t, r.Warnings, leakAdvisory("t-leak"))
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
	wantWarnings(t, resp.Warnings, leakAdvisory("t-leak"))
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
	if resp.Status != "pending_review" {
		t.Errorf("response = %+v, want pending_review", resp)
	}
	wantWarnings(t, resp.Warnings, leakAdvisory("t-leak"))
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
	if resp.Status != "no_changes" {
		t.Errorf("response = %+v, want no_changes", resp)
	}
	wantWarnings(t, resp.Warnings, leakAdvisory("t-leak"))
}

func TestBatchTenants_EscalationAdvisory_PRModeNoChanges(t *testing.T) {
	configDir := seedGitTree(t, escalationTreeLeakOnDisk())
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
		Policy: policy.NewManager(configDir), WriteMode: WriteModePR, PRClient: escalationPRClient(),
		PRTracker: &mockPlatformTracker{}}
	resp := runBatch(t, configDir, d, `[{"tenant_id":"t-leak","patch":{"_routing_profile":"esc-leak"}}]`)
	if resp.Status != "completed" {
		t.Errorf("status = %q, want completed: %+v", resp.Status, resp)
	}
	wantWarnings(t, resp.Warnings, leakAdvisory("t-leak"))
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

// A `require_critical_escalation: !!null {}` in a platform file that is NOT
// the domain policy (#2325) is not read the PyYAML way: only the policy file
// is. The profiles / defaults file stays usable, the tenant keeps its slack
// receiver, and PUT is still refused for both constraints — not accepted
// because the file carrying the receiver was dropped.
func TestPutTenant_TaggedNullEscalationOutsidePolicyFile_StillRefused(t *testing.T) {
	t.Parallel()
	slack := "{type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}"
	domain := "domain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n" +
		"      require_critical_escalation: true\n      forbidden_receiver_types: [slack]\n"
	viaProfile := "tenants:\n  t1:\n    cpu_usage_percent: '85'\n    _routing_profile: p1\n"
	for name, c := range map[string]struct {
		files map[string]string
		body  string
	}{
		"profiles file, top-level key": {map[string]string{
			"_defaults.yaml":         "defaults:\n  cpu_usage_percent: 80\n",
			"_routing_profiles.yaml": "routing_profiles:\n  p1:\n    receiver: " + slack + "\nmeta: {require_critical_escalation: !!null {}}\n",
		}, viaProfile},
		"profiles file, inside a profile": {map[string]string{
			"_defaults.yaml":         "defaults:\n  cpu_usage_percent: 80\n",
			"_routing_profiles.yaml": "routing_profiles:\n  p1:\n    receiver: " + slack + "\n    require_critical_escalation: !!null {}\n",
		}, viaProfile},
		"defaults file carrying _routing_defaults": {map[string]string{
			"_defaults.yaml": "defaults:\n  cpu_usage_percent: 80\n_routing_defaults:\n  receiver: " + slack +
				"\nmeta: {require_critical_escalation: !!null {}}\n",
		}, "tenants:\n  t1:\n    cpu_usage_percent: '85'\n    _routing:\n      group_wait: 30s\n"},
	} {
		c.files["_domain_policy.yaml"] = domain
		code, resp, _ := putRoutingTenant(t, c.files, "t1", c.body)
		if code != http.StatusForbidden || !strings.Contains(resp, `"constraint":"forbidden_receiver_types"`) ||
			!strings.Contains(resp, `"constraint":"require_critical_escalation"`) {
			t.Errorf("%s: status = %d, body %s; want 403 for forbidden_receiver_types and require_critical_escalation", name, code, resp)
		}
	}
}

// PR-mode batch with several included ops for one tenant (#2440 review): an
// earlier op is judged on an intermediate routing that WritePRBatch stacks
// the later ops over, so only the LAST included op's advisories describe
// what the PR writes. t-leak's first op (esc-leak) leaks, its second (esc-ok,
// critical straight to pagerduty) does not: no t-leak advisory is returned.
// t-a and t-b keep theirs, in the order the tenants first appear among the
// included ops (t-a before t-b, though t-a's leaking op comes after t-b's).
func TestBatchTenants_EscalationAdvisory_PRModeLastOpPerTenant(t *testing.T) {
	tree := escalationTree()
	tree["_domain_policy.yaml"] = "domain_policies:\n  escalation:\n    tenants: [t-leak, t-miss, t-a, t-b]\n" +
		"    constraints:\n      require_critical_escalation: true\n"
	tree["_routing_profiles.yaml"] += "  esc-ok:\n    receiver: {type: pagerduty, service_key: k}\n"
	tree["t-a.yaml"] = "tenants:\n  t-a:\n    cpu_usage_percent: '85'\n"
	tree["t-b.yaml"] = "tenants:\n  t-b:\n    cpu_usage_percent: '85'\n"
	configDir := seedGitTree(t, tree)
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
		Policy: policy.NewManager(configDir), WriteMode: WriteModePR, PRClient: escalationPRClient(),
		PRTracker: &mockPlatformTracker{}}
	resp := runBatch(t, configDir, d, `[
		{"tenant_id":"t-leak","patch":{"_routing_profile":"esc-leak"}},
		{"tenant_id":"t-a","patch":{"_routing_profile":"esc-ok"}},
		{"tenant_id":"t-b","patch":{"_routing_profile":"esc-leak"}},
		{"tenant_id":"t-a","patch":{"_routing_profile":"esc-leak"}},
		{"tenant_id":"t-leak","patch":{"_routing_profile":"esc-ok"}}]`)
	if resp.Status != "pending_review" {
		t.Fatalf("status = %q, want pending_review: %+v", resp.Status, resp)
	}
	for _, r := range resp.Results {
		if r.Status != "included" {
			t.Fatalf("result %+v, want every op included", r)
		}
	}
	wantWarnings(t, resp.Warnings, leakAdvisory("t-a"), leakAdvisory("t-b"))
}

// A later op of the same tenant that does not touch routing leaves the
// routing an earlier op produced in the PR as is, so it must not clear that
// op's advisory: the PR still ships esc-leak (#2440 review).
func TestBatchTenants_EscalationAdvisory_PRModeNonRoutingOpKeepsAdvisory(t *testing.T) {
	configDir := seedGitTree(t, escalationTree())
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
		Policy: policy.NewManager(configDir), WriteMode: WriteModePR, PRClient: escalationPRClient(),
		PRTracker: &mockPlatformTracker{}}
	resp := runBatch(t, configDir, d, `[
		{"tenant_id":"t-leak","patch":{"_routing_profile":"esc-leak"}},
		{"tenant_id":"t-leak","patch":{"cpu_usage_percent":"90"}}]`)
	if resp.Status != "pending_review" {
		t.Fatalf("status = %q, want pending_review: %+v", resp.Status, resp)
	}
	wantWarnings(t, resp.Warnings, leakAdvisory("t-leak"))
}

// PR-mode batch, several ops per tenant (#2440 review): each tenant's
// advisories appear once, whatever the mix of routing and non-routing ops,
// in the order of the tenants' first routing op taken into the PR. A
// non-routing op before a routing one must not register the tenant (it did,
// and each such op listed the tenant's advisories once more). A refused op
// is left out and changes nothing; a later routing op replaces an earlier
// one's advisories, down to none.
func TestBatchTenants_EscalationAdvisory_PRModeOncePerTenant(t *testing.T) {
	const (
		leak    = `{"tenant_id":"t-leak","patch":{"_routing_profile":"esc-leak"}}`
		miss    = `{"tenant_id":"t-leak","patch":{"_routing_profile":"esc-miss"}}`
		thr     = `{"tenant_id":"t-leak","patch":{"cpu_usage_percent":"90"}}`
		thr2    = `{"tenant_id":"t-leak","patch":{"cpu_usage_percent":"91"}}`
		thrNoop = `{"tenant_id":"t-leak","patch":{"cpu_usage_percent":"85"}}`
		other   = `{"tenant_id":"t-other","patch":{"_routing_profile":"esc-leak"}}`
	)
	ops := func(o ...string) string { return "[" + strings.Join(o, ",") + "]" }
	for _, c := range []struct {
		name       string
		leakOnDisk bool
		ops        string
		status     string
		want       []string
	}{
		{"A leak then threshold", false, ops(leak, thr), "pending_review", []string{leakAdvisory("t-leak")}},
		{"B leak then refused", false, ops(leak, miss), "pending_review", []string{leakAdvisory("t-leak")}},
		{"C refused then leak", false, ops(miss, leak), "pending_review", []string{leakAdvisory("t-leak")}},
		{"D2 no changes twice", true, ops(leak, leak), "completed", []string{leakAdvisory("t-leak")}},
		{"H threshold only, leak on disk", true, ops(thr), "pending_review", nil},
		{"I threshold then leak", false, ops(thr, leak), "pending_review", []string{leakAdvisory("t-leak")}},
		{"J thr, thr, leak", false, ops(thr, thr2, leak), "pending_review", []string{leakAdvisory("t-leak")}},
		{"K leak, thr, routing off", false,
			ops(leak, thr, `{"tenant_id":"t-leak","patch":{"_routing":"false"}}`), "pending_review", nil},
		{"L leak then empty profile", false,
			ops(leak, `{"tenant_id":"t-leak","patch":{"_routing_profile":""}}`), "pending_review", nil},
		{"M interleaved with another tenant", false, ops(thr, other, leak), "pending_review",
			[]string{leakAdvisory("t-other"), leakAdvisory("t-leak")}},
		{"O no-op threshold then no-op leak", true, ops(thrNoop, leak), "pending_review", []string{leakAdvisory("t-leak")}},
		{"P leak twice", false, ops(leak, leak), "pending_review", []string{leakAdvisory("t-leak")}},
	} {
		t.Run(c.name, func(t *testing.T) {
			tree := escalationTree()
			if c.leakOnDisk {
				tree = escalationTreeLeakOnDisk()
			}
			tree["_domain_policy.yaml"] = "domain_policies:\n  escalation:\n    tenants: [t-leak, t-miss, t-other]\n" +
				"    constraints:\n      require_critical_escalation: true\n"
			tree["t-other.yaml"] = "tenants:\n  t-other:\n    cpu_usage_percent: '85'\n"
			configDir := seedGitTree(t, tree)
			d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
				Policy: policy.NewManager(configDir), WriteMode: WriteModePR, PRClient: escalationPRClient(),
				PRTracker: &mockPlatformTracker{}}
			resp := runBatch(t, configDir, d, c.ops)
			if resp.Status != c.status {
				t.Errorf("status = %q, want %q: %+v", resp.Status, c.status, resp)
			}
			wantWarnings(t, resp.Warnings, c.want...)
		})
	}
}
