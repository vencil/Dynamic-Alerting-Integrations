package handler

// #2280: the domain-policy gate judges a tenant's RESOLVED routing —
// `_routing_defaults` → routing profile → the tenant's `_routing`, every
// receiver (main, overrides, routes) — on PUT and on the batch ops that set
// `_routing_profile` / `_routing`. The cross-language table is asserted by
// routing_policy_parity_test.go; this file pins the handler behaviour around
// it (nothing written on 403, per-op exclusion in both batch paths, the
// unrelated-write asymmetry).

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
)

// routingPolicyTree is a conf.d root with a finance policy that forbids
// slack, and routing profiles that do and do not render a slack receiver.
func routingPolicyTree() map[string]string {
	return map[string]string{
		"_defaults.yaml": "defaults:\n  cpu_usage_percent: 80\n",
		"_domain_policy.yaml": "domain_policies:\n  finance:\n    tenants: [tenant-x, t-sre, t-ok, t-dirty, t-page]\n" +
			"    constraints:\n      forbidden_receiver_types: [slack]\n",
		"_routing_profiles.yaml": "routing_profiles:\n" +
			"  team-chat:\n    receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}\n" +
			"  team-page:\n    receiver: {type: pagerduty, service_key: page-key}\n" +
			"    routes:\n    - match: {severity: critical}\n" +
			"      receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/esc'}\n" +
			"  domain-ok:\n    receiver: {type: pagerduty, service_key: ok-key}\n",
	}
}

const pdReceiver = "      receiver:\n        type: pagerduty\n        service_key: main-key\n"

// putRoutingTenant PUTs body for tenantID into a fresh git-backed copy of
// files, through the RBAC middleware, with the policy loaded from the tree.
func putRoutingTenant(t *testing.T, files map[string]string, tenantID, body string) (int, string, string) {
	t.Helper()
	configDir := setupConfigDir(t, files)
	initGitRepo(t, configDir)
	rbacMgr := newRBACManager(t, policyTestRBACYAML)
	h := PutTenant(&Deps{
		Writer:    newTestWriter(configDir),
		ConfigDir: configDir,
		RBAC:      rbacMgr,
		Policy:    policy.NewManager(configDir),
		WriteMode: WriteModeDirect,
	})
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+tenantID, "id", tenantID, bytes.NewBufferString(body))
	policyTestIdentity(req)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
	return w.Code, w.Body.String(), configDir
}

func TestPutTenant_ResolvedRoutingPolicy(t *testing.T) {
	cases := []struct {
		name, tenant, body string
		want               int
		target             string // the violation's target on 403
	}{
		// Step-0 cases i–iv (#2280): routes / overrides, unknown vs forbidden type.
		// The receiver SHAPE (an unknown type) is not the policy gate's (#2295).
		{"i routes entry, unknown type", "tenant-x",
			pdReceiver + "      routes:\n      - match: {severity: critical}\n        receiver: {type: bogus, url: 'https://x.example/hook'}\n",
			http.StatusOK, ""},
		{"ii routes entry, forbidden type", "tenant-x",
			pdReceiver + "      routes:\n      - match: {severity: critical}\n        receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}\n",
			http.StatusForbidden, "routes[0]"},
		{"iii override, unknown type", "tenant-x",
			pdReceiver + "      overrides:\n      - alertname: HighCPU\n        receiver: {type: bogus, url: 'https://x.example/hook'}\n",
			http.StatusOK, ""},
		{"iv override, forbidden type", "tenant-x",
			pdReceiver + "      overrides:\n      - alertname: HighCPU\n        receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}\n",
			http.StatusForbidden, "overrides[0]"},
		{"profile main receiver forbidden", "t-sre", "", http.StatusForbidden, "receiver"},
		{"profile route forbidden", "t-page", "", http.StatusForbidden, "routes[0]"},
		{"compliant profile", "t-ok", "", http.StatusOK, ""},
	}
	profileOf := map[string]string{"t-sre": "team-chat", "t-page": "team-page", "t-ok": "domain-ok"}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			body := "tenants:\n  " + tc.tenant + ":\n    cpu_usage_percent: '85'\n"
			if p, ok := profileOf[tc.tenant]; ok {
				body += "    _routing_profile: " + p + "\n"
			} else {
				body += "    _routing:\n" + tc.body
			}
			code, resp, configDir := putRoutingTenant(t, routingPolicyTree(), tc.tenant, body)
			if code != tc.want {
				t.Fatalf("status = %d, want %d; body: %s", code, tc.want, resp)
			}
			_, statErr := os.Stat(filepath.Join(configDir, tc.tenant+".yaml"))
			if tc.want != http.StatusForbidden {
				if statErr != nil {
					t.Errorf("allowed PUT did not land on disk: %v", statErr)
				}
				return
			}
			if !os.IsNotExist(statErr) {
				t.Errorf("refused PUT left %s.yaml on disk (err=%v)", tc.tenant, statErr)
			}
			var env struct {
				Code       string             `json:"code"`
				Violations []policy.Violation `json:"violations"`
			}
			if err := json.Unmarshal([]byte(resp), &env); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			if env.Code != CodePolicyViolation || len(env.Violations) != 1 || env.Violations[0].Target != tc.target {
				t.Errorf("response = %+v, want one %s violation on %s", env, CodePolicyViolation, tc.target)
			}
		})
	}
}

// runBatch posts ops through BatchTenants over a git-backed copy of files.
func runBatch(t *testing.T, configDir string, d *Deps, ops string) BatchResponse {
	t.Helper()
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(`{"operations":`+ops+`}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	d.RBAC.Middleware(rbac.PermRead, nil)(BatchTenants(d)).ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return resp
}

func seedGitTree(t *testing.T, files map[string]string) string {
	t.Helper()
	configDir := t.TempDir()
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(configDir, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	for _, args := range [][]string{
		{"init"}, {"config", "user.email", "t@t.com"}, {"config", "user.name", "T"},
		{"add", "."}, {"commit", "-m", "seed"}, {"branch", "-M", "main"},
	} {
		if out, err := exec.Command("git", append([]string{"-C", configDir}, args...)...).CombinedOutput(); err != nil {
			t.Skipf("git %v: %v\n%s", args, err, out)
		}
	}
	return configDir
}

func batchTree() map[string]string {
	files := routingPolicyTree()
	files["t-ok.yaml"] = "tenants:\n  t-ok:\n    cpu_usage_percent: '85'\n    _routing_profile: domain-ok\n"
	// Already violating on disk (a profile with a slack main receiver).
	files["t-dirty.yaml"] = "tenants:\n  t-dirty:\n    cpu_usage_percent: '85'\n    _routing_profile: team-chat\n"
	return files
}

func resultsByTenant(resp BatchResponse) map[string]BatchResult {
	out := map[string]BatchResult{}
	for _, r := range resp.Results {
		out[r.TenantID] = r
	}
	return out
}

// Direct mode (executeBatchOps): the op that points the tenant at a
// violating profile is refused and writes nothing; an op that touches no
// routing key is not refused for the routing already on disk.
func TestBatchTenants_RoutingProfilePolicy_Direct(t *testing.T) {
	configDir := seedGitTree(t, batchTree())
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
		Policy: policy.NewManager(configDir), WriteMode: WriteModeDirect}
	resp := runBatch(t, configDir, d, `[
		{"tenant_id":"t-ok","patch":{"_routing_profile":"team-chat"}},
		{"tenant_id":"t-dirty","patch":{"cpu_usage_percent":"90"}}]`)
	got := resultsByTenant(resp)
	if r := got["t-ok"]; r.Status != "error" || !strings.Contains(r.Message, "domain policy violation") ||
		!strings.Contains(r.Message, "routing profile 'team-chat'") {
		t.Errorf("t-ok = %+v, want refused naming the profile", r)
	}
	if r := got["t-dirty"]; r.Status != "ok" {
		t.Errorf("t-dirty = %+v, want ok (unrelated write is not refused for on-disk routing)", r)
	}
	b, _ := os.ReadFile(filepath.Join(configDir, "t-ok.yaml"))
	if strings.Contains(string(b), "team-chat") {
		t.Errorf("refused op reached disk:\n%s", b)
	}
}

// PR mode (batchTenantsPRMode): the refused op is left out and the rest of
// the batch still becomes one PR.
func TestBatchTenants_RoutingProfilePolicy_PRMode(t *testing.T) {
	configDir := seedGitTree(t, batchTree())
	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, head string, labels []string) (*platform.PRInfo, error) {
			return &platform.PRInfo{Number: 9, WebURL: "https://example/pr/9", State: "open"}, nil
		},
	}
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
		Policy: policy.NewManager(configDir), WriteMode: WriteModePR, PRClient: mockClient,
		PRTracker: &mockPlatformTracker{}}
	resp := runBatch(t, configDir, d, `[
		{"tenant_id":"t-ok","patch":{"_routing_profile":"team-page"}},
		{"tenant_id":"t-dirty","patch":{"cpu_usage_percent":"90"}}]`)
	if resp.Status != "pending_review" {
		t.Fatalf("status = %q, want pending_review: %+v", resp.Status, resp)
	}
	got := resultsByTenant(resp)
	if r := got["t-ok"]; r.Status != "error" || !strings.Contains(r.Message, "routes[0]") {
		t.Errorf("t-ok = %+v, want refused on the profile's routes[0]", r)
	}
	if r := got["t-dirty"]; r.Status != "included" {
		t.Errorf("t-dirty = %+v, want included in the PR", r)
	}
}
