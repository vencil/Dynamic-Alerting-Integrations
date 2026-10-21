package handler

// Shared fixtures of the batch and PUT handler tests. They lived beside the
// Go-side domain-policy tests, which #2903 (ADR-036 §4) removed with the
// judging they tested; routing and the domain policy are the route
// generator's (generator_gate_test.go, generator_real_test.go).

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
	"github.com/vencil/tenant-api/internal/rbac"
)

// routingPolicyTree is a conf.d root with a finance policy that forbids
// slack, and routing profiles that do and do not render a slack receiver.
// tenant-api no longer reads the policy (#2903); the tree is a realistic
// conf.d for the batch tests that write over it.
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

// runBatch posts ops through BatchTenants over a git-backed copy of files.
func runBatch(t *testing.T, configDir string, d *Deps, ops string) BatchResponse {
	t.Helper()
	w := postTenantBatch(t, d, ops)
	// #2900: a batch with a refused op is rejected whole — 422 with the same
	// per-op results body; anything else is 200.
	if w.Code != http.StatusOK && w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 200 or 422; body: %s", w.Code, w.Body.String())
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if (w.Code == http.StatusUnprocessableEntity) != (resp.Status == batchStatusRejected) {
		t.Fatalf("status %d with body status %q: 422 and \"rejected\" go together; body: %s", w.Code, resp.Status, w.Body.String())
	}
	return resp
}

// postTenantBatch is runBatch without the status assertion.
func postTenantBatch(t *testing.T, d *Deps, ops string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(`{"operations":`+ops+`}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	d.RBAC.Middleware(rbac.PermRead, nil)(BatchTenants(d)).ServeHTTP(w, req)
	return w
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
	addBareOrigin(t, configDir)
	return configDir
}

// addBareOrigin points dir's "origin" at a fresh, empty bare repository and
// returns its path, so a PR-mode write's push succeeds: a failed push fails
// the write (#1724). Nothing is pushed to it here — the base fetch finds no
// origin/<base> and the writer branches from the local base.
func addBareOrigin(t *testing.T, dir string) string {
	t.Helper()
	bare := t.TempDir()
	for _, args := range [][]string{{"init", "--bare", "-q", bare}, {"-C", dir, "remote", "add", "origin", bare}} {
		if out, err := exec.Command("git", args...).CombinedOutput(); err != nil {
			t.Skipf("git %v: %v\n%s", args, err, out)
		}
	}
	return bare
}

// prBranchRef is where a test reads a PR branch after the write: the
// writer drops the local branch once it is pushed (#641), and fails the
// write when it is not (#1724), so the copy left in the repo is the
// remote-tracking ref the push updated.
func prBranchRef(branch string) string { return "refs/remotes/origin/" + branch }

func gitHead(t *testing.T, dir string) string {
	t.Helper()
	out, err := exec.Command("git", "-C", dir, "rev-parse", "HEAD").CombinedOutput()
	if err != nil {
		t.Fatalf("git rev-parse: %v\n%s", err, out)
	}
	return strings.TrimSpace(string(out))
}

func batchTree() map[string]string {
	files := routingPolicyTree()
	files["t-ok.yaml"] = "tenants:\n  t-ok:\n    cpu_usage_percent: '85'\n    _routing_profile: domain-ok\n"
	files["t-dirty.yaml"] = "tenants:\n  t-dirty:\n    cpu_usage_percent: '85'\n    _routing_profile: team-chat\n"
	return files
}

// offTree: t-off is on disk with the profile team-chat, routing disabled.
func offTree(disk string) map[string]string {
	files := routingPolicyTree()
	files["_domain_policy.yaml"] = "domain_policies:\n  finance:\n    tenants: [t-off]\n" +
		"    constraints:\n      forbidden_receiver_types: [slack]\n"
	files["t-off.yaml"] = "tenants:\n  t-off:\n    cpu_usage_percent: '85'\n" + disk
	return files
}

const offDisabledChat = "    _routing_profile: team-chat\n    _routing: disable\n"

// runOffBatch runs ops against offTree(disk) in the given write mode and
// returns the per-op results and t-off.yaml as it would land: the file on
// disk (direct) or on the PR branch (PR mode; "" when no PR was opened).
func runOffBatch(t *testing.T, mode WriteMode, disk, ops string) ([]BatchResult, string) {
	t.Helper()
	configDir := seedGitTree(t, offTree(disk))
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t), WriteMode: mode}
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
	out, err := exec.Command("git", "-C", configDir, "show", prBranchRef(head)+":t-off.yaml").CombinedOutput()
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

// putStale PUTs body for tenantID through the RBAC middleware with f's deps.
func putStale(t *testing.T, f *staleFixture, tenantID, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+tenantID, "id", tenantID, bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(f.deps), f.deps.RBAC, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
	return w
}

// tenantBranches lists every tenant-api branch, local and on origin.
func (f *staleFixture) tenantBranches(t *testing.T) string {
	t.Helper()
	var out []string
	for _, dir := range []string{f.dir, f.bare} {
		b, err := exec.Command("git", "-C", dir, "branch", "--format=%(refname:short)").Output()
		if err != nil {
			t.Fatalf("git branch in %s: %v", dir, err)
		}
		for _, line := range strings.Split(string(b), "\n") {
			if strings.HasPrefix(line, "tenant-api/") {
				out = append(out, line)
			}
		}
	}
	return strings.Join(out, ",")
}

// writeTestRBACYAML grants the "ops" IdP group write on every tenant (RBAC
// open mode is read-only, so a write test needs an explicit rule).
const writeTestRBACYAML = `groups:
  - name: ops
    tenants: ["*"]
    permissions: [read, write]
`

// writeTestIdentity stamps a write-capable identity onto the request
// (matched by writeTestRBACYAML) so the RBAC middleware sets a verified
// principal and a non-empty author email for the git commit.
func writeTestIdentity(req *http.Request) {
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "ops")
}

// putReceiverDoc PUTs body for tenant "rs-t" into a fresh git tree; pr
// selects PR write-back. Returns the status, the response body and every
// branch's copy of the tenant file (empty when nothing was written anywhere).
func putReceiverDoc(t *testing.T, body string, pr bool) (int, string, string) {
	t.Helper()
	const tenant = "rs-t"
	dir := seedGitTree(t, map[string]string{"_defaults.yaml": "defaults:\n  cpu_usage_percent: 80\n"})
	rb := adminRBAC(t)
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: rb, WriteMode: WriteModeDirect}
	if pr {
		d.WriteMode = WriteModePR
		d.PRClient = &mockPlatformClient{}
		d.PRTracker = &mockPlatformTracker{}
	}
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+tenant, "id", tenant, bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(d), rb, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	var written strings.Builder
	refs, err := exec.Command("git", "-C", dir, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes").Output()
	if err != nil {
		t.Fatalf("list branches: %v", err)
	}
	for _, ref := range strings.Fields(string(refs)) {
		if out, err := exec.Command("git", "-C", dir, "show", ref+":"+tenant+".yaml").Output(); err == nil {
			written.WriteString(ref + ": " + string(out))
		}
	}
	return w.Code, w.Body.String(), written.String()
}
