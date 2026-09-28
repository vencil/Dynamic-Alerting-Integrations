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
			"  domain-ok:\n    receiver: {type: pagerduty, service_key: ok-key}\n" +
			"  inherit-bad:\n    receiver: {type: webhook}\n",
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
		// The receiver SHAPE (an unknown type) is not the policy gate's: it is
		// the #2295 receiver check's, a 400 naming the receiver's type field.
		{"i routes entry, unknown type", "tenant-x",
			pdReceiver + "      routes:\n      - match: {severity: critical}\n        receiver: {type: bogus, url: 'https://x.example/hook'}\n",
			http.StatusBadRequest, "tenants.tenant-x._routing.routes[0].receiver.type"},
		{"ii routes entry, forbidden type", "tenant-x",
			pdReceiver + "      routes:\n      - match: {severity: critical}\n        receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}\n",
			http.StatusForbidden, "routes[0]"},
		{"iii override, unknown type", "tenant-x",
			pdReceiver + "      overrides:\n      - alertname: HighCPU\n        receiver: {type: bogus, url: 'https://x.example/hook'}\n",
			http.StatusBadRequest, "tenants.tenant-x._routing.overrides[0].receiver.type"},
		{"iv override, forbidden type", "tenant-x",
			pdReceiver + "      overrides:\n      - alertname: HighCPU\n        receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}\n",
			http.StatusForbidden, "overrides[0]"},
		{"profile main receiver forbidden", "t-sre", "", http.StatusForbidden, "receiver"},
		{"profile route forbidden", "t-page", "", http.StatusForbidden, "routes[0]"},
		{"compliant profile", "t-ok", "", http.StatusOK, ""},
		// #2295: only receivers the body writes are shape-checked; one the
		// tenant inherits from a profile is da-guard's to judge.
		{"inherited malformed profile receiver", "t-inh", "", http.StatusOK, ""},
	}
	profileOf := map[string]string{"t-sre": "team-chat", "t-page": "team-page", "t-ok": "domain-ok", "t-inh": "inherit-bad"}
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
			if tc.want == http.StatusOK {
				if statErr != nil {
					t.Errorf("allowed PUT did not land on disk: %v", statErr)
				}
				return
			}
			if !os.IsNotExist(statErr) {
				t.Errorf("refused PUT left %s.yaml on disk (err=%v)", tc.tenant, statErr)
			}
			if tc.want == http.StatusBadRequest {
				var bad ErrorResponse
				if err := json.Unmarshal([]byte(resp), &bad); err != nil {
					t.Fatalf("unmarshal: %v", err)
				}
				if bad.Code != CodeInvalidBody || len(bad.Violations) != 1 || bad.Violations[0].Field != tc.target {
					t.Errorf("response = %+v, want one %s violation on %s", bad, CodeInvalidBody, tc.target)
				}
				return
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

// offTree: t-off is on disk with the violating profile team-chat, routing
// disabled — compliant as it stands.
func offTree(disk string) map[string]string {
	files := routingPolicyTree()
	files["_domain_policy.yaml"] = "domain_policies:\n  finance:\n    tenants: [t-off]\n" +
		"    constraints:\n      forbidden_receiver_types: [slack]\n"
	files["t-off.yaml"] = "tenants:\n  t-off:\n    cpu_usage_percent: '85'\n" + disk
	return files
}

const offDisabledChat = "    _routing_profile: team-chat\n    _routing: disable\n"

// The block ON DISK is part of what a batch op is judged on: re-enabling
// routing on a tenant whose file names a violating profile is refused even
// though the patch alone names nothing forbidden. And the other way round,
// pointing a disabled tenant at that profile is allowed (nothing renders).
func TestBatchTenants_RoutingPatchJudgedOverDiskBlock(t *testing.T) {
	cases := []struct {
		name, disk, patch string
		refused           bool
	}{
		{"re-enable over a violating profile on disk", offDisabledChat, `{"_routing":"on"}`, true},
		{"violating profile while disabled on disk", "    _routing_profile: domain-ok\n    _routing: disable\n",
			`{"_routing_profile":"team-chat"}`, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			configDir := seedGitTree(t, offTree(tc.disk))
			before, _ := os.ReadFile(filepath.Join(configDir, "t-off.yaml"))
			d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
				Policy: policy.NewManager(configDir), WriteMode: WriteModeDirect}
			resp := runBatch(t, configDir, d, `[{"tenant_id":"t-off","patch":`+tc.patch+`}]`)
			r := resp.Results[0]
			after, _ := os.ReadFile(filepath.Join(configDir, "t-off.yaml"))
			if tc.refused {
				if r.Status != "error" || !strings.Contains(r.Message, "domain policy violation") {
					t.Fatalf("result = %+v, want refused for domain policy", r)
				}
				if !bytes.Equal(before, after) {
					t.Errorf("refused op changed t-off.yaml:\n%s", after)
				}
				return
			}
			if r.Status != "ok" || bytes.Equal(before, after) {
				t.Errorf("result = %+v (file changed: %v), want ok and written", r, !bytes.Equal(before, after))
			}
		})
	}
}

// Two ops on one tenant in one request, each fine alone: point the disabled
// tenant at the violating profile, then re-enable routing. Stacked they
// render slack. The second op must be refused in BOTH write modes.
const stackedOps = `[
	{"tenant_id":"t-off","patch":{"_routing_profile":"team-chat"}},
	{"tenant_id":"t-off","patch":{"_routing":"on"}}]`

func TestBatchTenants_StackedOpsSameTenant_Direct(t *testing.T) {
	configDir := seedGitTree(t, offTree("    _routing_profile: domain-ok\n    _routing: disable\n"))
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
		Policy: policy.NewManager(configDir), WriteMode: WriteModeDirect}
	resp := runBatch(t, configDir, d, stackedOps)
	if len(resp.Results) != 2 || resp.Results[0].Status != "ok" || resp.Results[1].Status != "error" ||
		!strings.Contains(resp.Results[1].Message, "domain policy violation") {
		t.Fatalf("results = %+v, want the first op ok and the second refused", resp.Results)
	}
	b, _ := os.ReadFile(filepath.Join(configDir, "t-off.yaml"))
	if !strings.Contains(string(b), "team-chat") || !strings.Contains(string(b), "disable") {
		t.Errorf("t-off.yaml should hold op 1 only (team-chat, still disabled):\n%s", b)
	}
}

func TestBatchTenants_StackedOpsSameTenant_PRMode(t *testing.T) {
	configDir := seedGitTree(t, offTree("    _routing_profile: domain-ok\n    _routing: disable\n"))
	var head string
	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, h string, labels []string) (*platform.PRInfo, error) {
			head = h
			return &platform.PRInfo{Number: 9, WebURL: "https://example/pr/9", State: "open"}, nil
		},
	}
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
		Policy: policy.NewManager(configDir), WriteMode: WriteModePR, PRClient: mockClient,
		PRTracker: &mockPlatformTracker{}}
	resp := runBatch(t, configDir, d, stackedOps)
	if len(resp.Results) != 2 || resp.Results[0].Status != "included" || resp.Results[1].Status != "error" ||
		!strings.Contains(resp.Results[1].Message, "policy violation") {
		t.Fatalf("results = %+v, want the first op included and the second refused", resp.Results)
	}
	if head == "" {
		t.Fatalf("no PR opened: %+v", resp)
	}
	out, err := exec.Command("git", "-C", configDir, "show", head+":t-off.yaml").CombinedOutput()
	if err != nil {
		t.Fatalf("git show %s:t-off.yaml: %v\n%s", head, err, out)
	}
	if !strings.Contains(string(out), "team-chat") || !strings.Contains(string(out), "disable") {
		t.Errorf("PR branch must hold op 1 only (team-chat, still disabled):\n%s", out)
	}
}

// runOffBatch runs ops against offTree(disk) in the given write mode and
// returns the per-op results and t-off.yaml as it would land: the file on
// disk (direct) or on the PR branch (PR mode; "" when no PR was opened).
func runOffBatch(t *testing.T, mode WriteMode, disk, ops string) ([]BatchResult, string) {
	t.Helper()
	configDir := seedGitTree(t, offTree(disk))
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
		Policy: policy.NewManager(configDir), WriteMode: mode}
	var head string
	if mode == WriteModePR {
		d.PRClient = &mockPlatformClient{
			providerName: "github",
			createPRFunc: func(title, body, h string, labels []string) (*platform.PRInfo, error) {
				head = h
				return &platform.PRInfo{Number: 9, WebURL: "https://example/pr/9", State: "open"}, nil
			},
		}
		d.PRTracker = &mockPlatformTracker{}
	}
	resp := runBatch(t, configDir, d, ops)
	if mode != WriteModePR {
		b, _ := os.ReadFile(filepath.Join(configDir, "t-off.yaml"))
		return resp.Results, string(b)
	}
	if head == "" {
		return resp.Results, ""
	}
	out, err := exec.Command("git", "-C", configDir, "show", head+":t-off.yaml").CombinedOutput()
	if err != nil {
		t.Fatalf("git show %s:t-off.yaml: %v\n%s", head, err, out)
	}
	return resp.Results, string(out)
}

func statuses(results []BatchResult) string {
	out := make([]string, len(results))
	for i, r := range results {
		out[i] = r.Status
	}
	return strings.Join(out, ",")
}

// A REFUSED op is not stacked under the ops after it: nothing of it is
// written, so judging a later op as if it were would judge a state that
// never lands. Two shapes, both write modes (PR mode stacks in memory,
// direct mode reads the file back).
func TestBatchTenants_RefusedOpIsNotStacked(t *testing.T) {
	ok := "included"
	for _, mode := range []WriteMode{WriteModePR, WriteModeDirect} {
		if mode == WriteModeDirect {
			ok = "ok"
		}
		t.Run(string(mode)+"/bypass: refused op carried _routing: disable", func(t *testing.T) {
			// op1 is refused (its flat receiver-type key is forbidden) but
			// also sets `_routing: disable`. Stacked, it would make op2's
			// violating profile look disabled — while what lands is op2's
			// profile with routing ENABLED.
			results, file := runOffBatch(t, mode, "    _routing_profile: domain-ok\n", `[
				{"tenant_id":"t-off","patch":{"cpu_usage_percent":"90"}},
				{"tenant_id":"t-off","patch":{"_routing":"disable","_routing_receiver_type":"slack"}},
				{"tenant_id":"t-off","patch":{"_routing_profile":"team-chat"}}]`)
			if got := statuses(results); got != ok+",error,error" {
				t.Fatalf("statuses = %s, want %s,error,error: %+v", got, ok, results)
			}
			if strings.Contains(file, "team-chat") || strings.Contains(file, "disable") || !strings.Contains(file, "domain-ok") {
				t.Errorf("t-off.yaml must keep domain-ok, enabled:\n%s", file)
			}
		})
		t.Run(string(mode)+"/variant B: op3 is judged over op1 only", func(t *testing.T) {
			// Disk: compliant profile, disabled. op1 (violating profile,
			// still disabled) is fine; op2 re-enables → refused; op3 swaps
			// to another violating profile — fine over op1 (still
			// disabled), refused only if the refused op2 were stacked.
			results, file := runOffBatch(t, mode, "    _routing_profile: domain-ok\n    _routing: disable\n", `[
				{"tenant_id":"t-off","patch":{"_routing_profile":"team-chat"}},
				{"tenant_id":"t-off","patch":{"_routing":"on"}},
				{"tenant_id":"t-off","patch":{"_routing_profile":"team-page"}}]`)
			if got := statuses(results); got != ok+",error,"+ok {
				t.Fatalf("statuses = %s, want %s,error,%s: %+v", got, ok, ok, results)
			}
			if !strings.Contains(file, "team-page") || !strings.Contains(file, "disable") {
				t.Errorf("t-off.yaml must be team-page, still disabled:\n%s", file)
			}
		})
	}
}
