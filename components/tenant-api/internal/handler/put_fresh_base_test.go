package handler

// #2486 item 1: a PR-mode PUT is judged on the FRESH base too. The policy
// pre-check reads the pod's local tree and the policy watcher's copy, which
// are synced only at startup; WritePR cuts its branch from the freshest
// origin/<base>. Without a second check there, a profile, `_routing_defaults`
// or policy list tightened on origin is bypassed. Now WritePRChecked runs
// freshBasePutPolicyCheck on the checked-out base before writing: 403
// POLICY_VIOLATION, nothing written, no PR, no branch left.
//
// Item 2 (PR half): the base's `_domain_policy.yml` is read there too.

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

	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
)

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

// assertPutFreshBaseRefused: 403 POLICY_VIOLATION naming the tenant (no op
// index) on the latest base, with violations or the load failure; no PR, no
// branch, origin's main unmoved, the local file unchanged.
func assertPutFreshBaseRefused(t *testing.T, f *staleFixture, w *httptest.ResponseRecorder, tenant, originMainBefore, localBefore string, loadErr bool) {
	t.Helper()
	var env map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &env)
	msg, _ := env["error"].(string)
	_, hasOp := env["operation"]
	if w.Code != http.StatusForbidden || env["code"] != CodePolicyViolation || env["tenant_id"] != tenant || hasOp ||
		!strings.Contains(msg, "latest base branch") || !strings.Contains(msg, "tenant "+tenant) ||
		!strings.Contains(msg, "Nothing was written and no PR/MR was opened") {
		t.Errorf("status %d, body %s; want 403 %s for tenant %s on the latest base", w.Code, w.Body.String(), CodePolicyViolation, tenant)
	}
	vs, _ := env["violations"].([]any)
	if loadErr {
		if !strings.Contains(msg, "cannot be loaded") || strings.Contains(msg, f.dir) {
			t.Errorf("want the unloadable-policy message without a server path: %s", msg)
		}
	} else if len(vs) == 0 {
		t.Errorf("no policy violations listed: %s", w.Body.String())
	}
	if f.prOpened {
		t.Error("a PR was opened")
	}
	if b := f.tenantBranches(t); b != "" {
		t.Errorf("branch left behind: %s", b)
	}
	if got := gitRev(t, f.bare, "main"); got != originMainBefore {
		t.Errorf("origin main moved: %s → %s", originMainBefore, got)
	}
	if got := mustRead(t, filepath.Join(f.dir, "t-off.yaml")); got != localBefore {
		t.Errorf("the local t-off.yaml changed:\n%s", got)
	}
}

// The body every case PUTs: t-off on the compliant profile, a threshold
// changed so the write is a real change.
const putOKProfileBody = "tenants:\n  t-off:\n    cpu_usage_percent: '90'\n    _routing_profile: domain-ok\n"

const slackRoutingDefaults = "defaults:\n  cpu_usage_percent: 80\n" +
	"_routing_defaults:\n  receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/d'}\n"

func TestPutTenant_PRMode_PolicyJudgedOnFreshBase(t *testing.T) {
	profilesSlackOK := strings.Replace(routingPolicyTree()["_routing_profiles.yaml"],
		"  domain-ok:\n    receiver: {type: pagerduty, service_key: ok-key}\n",
		"  domain-ok:\n    receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/ok'}\n", 1)
	cases := []struct {
		name, localPolicy, body string
		origin                  map[string]string
		loadErr                 bool
	}{
		{name: "profile tightened on origin", body: putOKProfileBody,
			origin: map[string]string{"_routing_profiles.yaml": profilesSlackOK}},
		{name: "_routing_defaults tightened on origin",
			body:   "tenants:\n  t-off:\n    cpu_usage_percent: '90'\n",
			origin: map[string]string{"_defaults.yaml": slackRoutingDefaults}},
		{name: "tenant added to the policy on origin", localPolicy: policyListing("t-other"),
			body:   "tenants:\n  t-off:\n    cpu_usage_percent: '90'\n    _routing_profile: team-chat\n",
			origin: map[string]string{"_domain_policy.yaml": policyListing("t-off, t-other")}},
		{name: "policy file broken on origin", body: putOKProfileBody, loadErr: true,
			origin: map[string]string{"_domain_policy.yaml": "domain_policies: [unclosed\n"}},
		// Item 2: only a `.yml` on origin lists the tenant.
		{name: "tenant listed by a _domain_policy.yml added on origin", localPolicy: policyListing("t-other"),
			body:   "tenants:\n  t-off:\n    cpu_usage_percent: '90'\n    _routing_profile: team-chat\n",
			origin: map[string]string{"_domain_policy.yml": policyListing("t-off")}},
		{name: "_domain_policy.yml broken on origin", body: putOKProfileBody, loadErr: true,
			origin: map[string]string{"_domain_policy.yml": "domain_policies: [unclosed\n"}},
		// #2659: YAML that parses but whose domain_policies is null — the
		// shape da-guard reports as domain_policy_unusable — is refused too.
		{name: "domain_policies null on origin", body: putOKProfileBody, loadErr: true,
			origin: map[string]string{"_domain_policy.yaml": "domain_policies: null\n"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			local := staleTree("    _routing_profile: domain-ok\n")
			if tc.localPolicy != "" {
				local["_domain_policy.yaml"] = tc.localPolicy
			}
			f := newStaleFixture(t, local, tc.origin)
			before := gitRev(t, f.bare, "main")
			localBefore := mustRead(t, filepath.Join(f.dir, "t-off.yaml"))
			w := putStale(t, f, "t-off", tc.body)
			assertPutFreshBaseRefused(t, f, w, "t-off", before, localBefore, tc.loadErr)
		})
	}
}

// Local and origin agree: the result is the pre-fix one — a compliant body
// opens a PR; a violating body is refused by the pre-check (no op index, the
// plain "domain policy violation" message), before any branch is cut.
func TestPutTenant_PRMode_InSyncUnchanged(t *testing.T) {
	t.Run("compliant", func(t *testing.T) {
		f := newStaleFixture(t, staleTree("    _routing_profile: domain-ok\n"), nil)
		w := putStale(t, f, "t-off", putOKProfileBody)
		var resp PutTenantResponse
		_ = json.Unmarshal(w.Body.Bytes(), &resp)
		if w.Code != http.StatusOK || resp.Status != "pending_review" || !f.prOpened {
			t.Fatalf("status %d, body %s, PR %v; want 200 pending_review and a PR", w.Code, w.Body.String(), f.prOpened)
		}
	})
	t.Run("violating", func(t *testing.T) {
		f := newStaleFixture(t, staleTree("    _routing_profile: domain-ok\n"), nil)
		w := putStale(t, f, "t-off", "tenants:\n  t-off:\n    _routing_profile: team-chat\n")
		var env map[string]any
		_ = json.Unmarshal(w.Body.Bytes(), &env)
		if w.Code != http.StatusForbidden || env["code"] != CodePolicyViolation || env["error"] != "domain policy violation" {
			t.Fatalf("status %d, body %s; want the pre-check's 403", w.Code, w.Body.String())
		}
		if f.prOpened || f.tenantBranches(t) != "" {
			t.Errorf("PR %v, branches %q", f.prOpened, f.tenantBranches(t))
		}
	})
	// The base relaxes the policy: the local pre-check still refuses
	// (pre-existing behaviour, as for batch).
	t.Run("policy relaxed on origin", func(t *testing.T) {
		f := newStaleFixture(t, staleTree("    _routing_profile: domain-ok\n"),
			map[string]string{"_domain_policy.yaml": policyListing("t-other")})
		w := putStale(t, f, "t-off", "tenants:\n  t-off:\n    _routing_profile: team-chat\n")
		if w.Code != http.StatusForbidden || f.prOpened {
			t.Fatalf("status %d (PR %v), want the local pre-check's 403; body %s", w.Code, f.prOpened, w.Body.String())
		}
	})
}

// #2486 review B1, then hub #2486 Q7-2: a `_domain_policy.yml` broken when
// the pod starts has no last good content, so the policy is unavailable and
// direct mode refuses the writes the policy judges — 503 POLICY_UNAVAILABLE,
// PUT and a routing batch op alike, nothing written — instead of judging
// them on the sound `.yaml` alone (the route generator drops the broken
// file and its `--strict` exits 1). A batch op the policy does not judge
// goes through.
func TestDirectMode_BrokenYmlAtStartupRefusesPolicyWrites(t *testing.T) {
	files := staleTree("    _routing_profile: domain-ok\n")
	files["_domain_policy.yml"] = "domain_policies: [unclosed\n"
	t.Run("put", func(t *testing.T) {
		code, resp, configDir := putRoutingTenant(t, files, "t-off", "tenants:\n  t-off:\n    _routing_profile: domain-ok\n")
		if code != http.StatusServiceUnavailable || !strings.Contains(resp, CodePolicyUnavailable) {
			t.Fatalf("status %d, body %s; want 503 POLICY_UNAVAILABLE", code, resp)
		}
		if got, err := os.ReadFile(filepath.Join(configDir, "t-off.yaml")); err != nil || string(got) != files["t-off.yaml"] {
			t.Errorf("refused PUT changed t-off.yaml: %q, %v", got, err)
		}
	})
	t.Run("batch", func(t *testing.T) {
		configDir := seedGitTree(t, files)
		d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
			Policy: policy.NewManager(configDir), WriteMode: WriteModeDirect}
		w := postTenantBatch(t, d, `[{"tenant_id":"t-off","patch":{"_routing_profile":"team-chat"}}]`)
		if w.Code != http.StatusServiceUnavailable || !strings.Contains(w.Body.String(), CodePolicyUnavailable) {
			t.Fatalf("status %d, body %s; want 503 POLICY_UNAVAILABLE", w.Code, w.Body.String())
		}
		// A patch the policy does not judge is not refused.
		resp := runBatch(t, configDir, d, `[{"tenant_id":"t-off","patch":{"cpu_usage_percent":"90"}}]`)
		if len(resp.Results) != 1 || resp.Results[0].Status != "ok" {
			t.Errorf("results = %+v, want the non-routing op written", resp.Results)
		}
	})
	t.Run("escape hatch", func(t *testing.T) {
		configDir := seedGitTree(t, files)
		mgr := policy.NewManager(configDir)
		mgr.SetOpenOnUnavailable(true)
		d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
			Policy: mgr, WriteMode: WriteModeDirect}
		resp := runBatch(t, configDir, d, `[{"tenant_id":"t-off","patch":{"_routing_profile":"team-chat"}}]`)
		if len(resp.Results) != 1 || !strings.Contains(resp.Results[0].Message, "domain policy violation") {
			t.Errorf("results = %+v, want the op judged by the .yaml policy (let through the gate)", resp.Results)
		}
		if mgr.OpenPasses() == 0 {
			t.Error("OpenPasses = 0, want the write let through counted")
		}
	})
	// N2 (hub #2486 PR-7c round 2): tenant_api_policy_unavailable_open_total
	// counts writes — one per op the policy judges — never the batch request
	// on top: two routing ops and one the policy does not judge count 2 (the
	// batch-level gate used to count once more).
	t.Run("escape hatch counts each write once", func(t *testing.T) {
		configDir := seedGitTree(t, files)
		mgr := policy.NewManager(configDir)
		mgr.SetOpenOnUnavailable(true)
		d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
			Policy: mgr, WriteMode: WriteModeDirect}
		resp := runBatch(t, configDir, d, `[{"tenant_id":"t-off","patch":{"_routing_profile":"domain-ok"}},`+
			`{"tenant_id":"t-other","patch":{"_routing_profile":"domain-ok"}},`+
			`{"tenant_id":"t-off","patch":{"cpu_usage_percent":"90"}}]`)
		if len(resp.Results) != 3 {
			t.Fatalf("results = %+v, want 3", resp.Results)
		}
		if got := mgr.OpenPasses(); got != 2 {
			t.Errorf("OpenPasses = %d, want 2 (one per routing op, none for the batch)", got)
		}
	})
}

// Item 2, batch half: the in-lock batch check reads the base's
// `_domain_policy.yml` too.
func TestBatchTenants_PRMode_DomainPolicyYmlOnFreshBase(t *testing.T) {
	local := staleTree("    _routing_profile: domain-ok\n")
	local["_domain_policy.yaml"] = policyListing("t-other")
	f := newStaleFixture(t, local, map[string]string{"_domain_policy.yml": policyListing("t-off")})
	before := gitRev(t, f.bare, "main")
	assertFreshBaseRefused(t, f, postTenantBatch(t, f.deps,
		`[{"tenant_id":"t-off","patch":{"_routing_profile":"team-chat"}}]`), 0, "t-off", before)
}
