package handler

// ADR-027 / LD-6 P4b §5c — enforce-mode 403 harness for the write-plane
// org-scope gates in the handler package (sites #1-#5, #10, #11; the
// federation sites #6-#9 live in federation/org_write_enforce_test.go).
//
// Fixture shape (every test):
//   - ONE org-scoped RBAC rule (`org-scope: org`, tenants ["*"], full perms)
//     loaded through rbac.NewManager — the PRODUCTION path, so the fixture
//     passes validateConfig (org claim key declared via claim headers) and
//     the middleware resolves the caller's X-Auth-Request-Org header into
//     the principal claim the rule keys off.
//   - EnableOrgScopeEnforce() — the axis under test is fail-closed.
//   - A REAL tenantorg manager labeling orgEnfTenantIn with the caller's org
//     and orgEnfTenantOut with a different org.
//   - A git-backed gitops.Writer with an onWrite spy: a denied request must
//     not merely answer 403 / per-item error — the spy pins that NO commit
//     happened (the forgot-to-return safety net).
//
// Caller rows: the "member" caller carries the org that the target tenant is
// labeled with; the "outsider" carries a different org. Outsider → 403 (or
// per-item error) + zero writes; member → non-403.
//
// "*"-semantics pins (invariant I6) live at the bottom: enforce mode must
// NOT change platform-scope ("*") gates or non-tenant-data writes — the org
// axis narrows per-tenant write grants only.

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
	"github.com/vencil/tenant-api/internal/views"
)

const (
	orgEnfClaimHeader = "X-Auth-Request-Org"
	orgEnfGroup       = "org-enf-writers"
	orgEnfTenantIn    = "tenant-org-in"  // labeled with the member caller's org
	orgEnfTenantOut   = "tenant-org-out" // labeled with a different org
	orgEnfMemberOrg   = "ORG-ALPHA"
	orgEnfOutsiderOrg = "ORG-BETA"
)

const orgEnfRBACYAML = `groups:
  - name: ` + orgEnfGroup + `
    tenants: ["*"]
    permissions: [read, write, admin]
    org-scope: org
`

// orgEnfFixture bundles the enforce-mode org-scope harness pieces.
type orgEnfFixture struct {
	configDir string
	writer    *gitops.Writer
	rbacMgr   *rbac.Manager
	tenantOrg *tenantorg.Manager
	writes    atomic.Int32
}

// newOrgEnfFixture builds the harness. files seeds configDir (may be nil);
// the dir is git-initialized (branch main) so both direct commits and
// PR-mode branch writes succeed for the ALLOWED rows.
func newOrgEnfFixture(t *testing.T, files map[string]string) *orgEnfFixture {
	t.Helper()
	configDir := setupConfigDir(t, files)
	initGitRepo(t, configDir)
	// initGitRepo leaves the repo on the init default branch; PR-mode writes
	// check out base "main" — normalize (same as initGitConfigDir).
	cmd := exec.Command("git", "-C", configDir, "branch", "-M", "main")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git branch -M main: %v\n%s", err, out)
	}

	f := &orgEnfFixture{configDir: configDir}
	f.writer = newTestWriter(configDir)
	f.writer.SetOnWrite(func(string) { f.writes.Add(1) })

	f.rbacMgr = newRBACManagerWithClaims(t, orgEnfRBACYAML,
		map[string]string{"org": orgEnfClaimHeader})
	f.rbacMgr.EnableOrgScopeEnforce()

	f.tenantOrg = tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{
		orgEnfTenantIn:  {orgEnfMemberOrg},
		orgEnfTenantOut: {orgEnfOutsiderOrg},
	}})
	return f
}

func (f *orgEnfFixture) deps() *Deps {
	return &Deps{
		ConfigDir: f.configDir,
		Writer:    f.writer,
		RBAC:      f.rbacMgr,
		TenantOrg: f.tenantOrg,
		Groups:    groups.NewManager(f.configDir),
		Views:     views.NewManager(f.configDir),
		WriteMode: WriteModeDirect,
	}
}

// orgEnfIdentity stamps the org-scoped caller identity onto a request.
// callerOrg selects the row: orgEnfMemberOrg / orgEnfOutsiderOrg.
func orgEnfIdentity(req *http.Request, callerOrg string) *http.Request {
	req.Header.Set("X-Forwarded-Email", "org-caller@example.com")
	req.Header.Set("X-Forwarded-Groups", orgEnfGroup)
	req.Header.Set(orgEnfClaimHeader, callerOrg)
	return req
}

// ── Site #10: PutTenant (direct write mode) ────────────────────────────────

func TestOrgWriteEnforce_PutTenant_Direct(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name      string
		callerOrg string
		wantDeny  bool
	}{
		{"outsider_denied_403_no_write", orgEnfOutsiderOrg, true},
		{"member_allowed", orgEnfMemberOrg, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			f := newOrgEnfFixture(t, nil)
			body := bytes.NewBufferString("tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n")
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+orgEnfTenantIn, "id", orgEnfTenantIn, body)
			req = orgEnfIdentity(req, tc.callerOrg)
			w := httptest.NewRecorder()
			wrapWithRBACMiddleware(PutTenant(f.deps()), f.rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

			if tc.wantDeny {
				if w.Code != http.StatusForbidden {
					t.Fatalf("outsider: status = %d, want 403; body=%s", w.Code, w.Body.String())
				}
				if n := f.writes.Load(); n != 0 {
					t.Errorf("outsider: writer committed %d time(s), want 0 (denied request must not write)", n)
				}
			} else {
				if w.Code != http.StatusOK {
					t.Fatalf("member: status = %d, want 200; body=%s", w.Code, w.Body.String())
				}
				if n := f.writes.Load(); n != 1 {
					t.Errorf("member: writer commits = %d, want 1", n)
				}
			}
		})
	}
}

// PR mode: the top-of-handler gate must fire BEFORE any PR machinery — a
// denied caller must not even claim the tenant (no side effect, no probe).
func TestOrgWriteEnforce_PutTenant_PRModeDeniedBeforeClaim(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, nil)
	tracker := &mockPlatformTracker{}
	client := &mockPlatformClient{}
	d := f.deps()
	d.WriteMode = WriteModePR
	d.PRClient = client
	d.PRTracker = tracker

	body := bytes.NewBufferString("tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+orgEnfTenantIn, "id", orgEnfTenantIn, body)
	req = orgEnfIdentity(req, orgEnfOutsiderOrg)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(d), f.rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
	}
	if len(tracker.claimed) != 0 || len(tracker.prs) != 0 {
		t.Errorf("denied PR-mode request touched the tracker: claimed=%v prs=%v", tracker.claimed, tracker.prs)
	}
	if n := f.writes.Load(); n != 0 {
		t.Errorf("denied PR-mode request committed %d time(s), want 0", n)
	}
}

// ── Site #11: PutTenantCustomAlerts ────────────────────────────────────────

func TestOrgWriteEnforce_PutCustomAlerts(t *testing.T) {
	t.Parallel()

	do := func(t *testing.T, f *orgEnfFixture, d *Deps, callerOrg string) *httptest.ResponseRecorder {
		t.Helper()
		body := bytes.NewBufferString(`{}`)
		req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+orgEnfTenantIn+"/custom-alerts", "id", orgEnfTenantIn, body)
		req = orgEnfIdentity(req, callerOrg)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(PutTenantCustomAlerts(d), f.rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
		return w
	}

	t.Run("outsider_denied_403", func(t *testing.T) {
		t.Parallel()
		f := newOrgEnfFixture(t, nil)
		w := do(t, f, f.deps(), orgEnfOutsiderOrg)
		if w.Code != http.StatusForbidden {
			t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
		}
		if n := f.writes.Load(); n != 0 {
			t.Errorf("denied request committed %d time(s), want 0", n)
		}
	})

	// Authorization must come BEFORE feature availability: in PR mode an
	// outsider gets 403, NOT the 501 — write-mode is not probeable by an
	// unauthorized caller.
	t.Run("outsider_pr_mode_403_not_501", func(t *testing.T) {
		t.Parallel()
		f := newOrgEnfFixture(t, nil)
		d := f.deps()
		d.WriteMode = WriteModePR
		w := do(t, f, d, orgEnfOutsiderOrg)
		if w.Code != http.StatusForbidden {
			t.Fatalf("status = %d, want 403 (authz precedes the PR-mode 501); body=%s", w.Code, w.Body.String())
		}
	})

	// Member passes the gate; the empty body then fails validation (400) —
	// deterministically NOT 403, proving the org gate admitted the caller.
	t.Run("member_passes_gate", func(t *testing.T) {
		t.Parallel()
		f := newOrgEnfFixture(t, nil)
		w := do(t, f, f.deps(), orgEnfMemberOrg)
		if w.Code == http.StatusForbidden {
			t.Fatalf("member denied: status = 403; body=%s", w.Body.String())
		}
		if w.Code != http.StatusBadRequest {
			t.Fatalf("status = %d, want 400 (validation, past the org gate); body=%s", w.Code, w.Body.String())
		}
	})
}

// ── Sites #3/#4: BatchTenants (sync / async / PR-mode pre-validation) ─────

// orgEnfBatchBody: one op on the member-org tenant, one on the other-org
// tenant — a single caller (member org) must see a per-item split.
func orgEnfBatchBody(t *testing.T) *bytes.Buffer {
	t.Helper()
	reqBody, err := json.Marshal(BatchRequest{Operations: []BatchOperation{
		{TenantID: orgEnfTenantIn, Patch: map[string]string{"_silent_mode": "warning"}},
		{TenantID: orgEnfTenantOut, Patch: map[string]string{"_silent_mode": "warning"}},
	}})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return bytes.NewBuffer(reqBody)
}

func orgEnfCheckBatchResults(t *testing.T, results []BatchResult) {
	t.Helper()
	if len(results) != 2 {
		t.Fatalf("results = %d, want 2: %+v", len(results), results)
	}
	byTenant := map[string]BatchResult{}
	for _, r := range results {
		byTenant[r.TenantID] = r
	}
	if got := byTenant[orgEnfTenantIn]; got.Status != "ok" {
		t.Errorf("member-org tenant result = %+v, want ok", got)
	}
	out := byTenant[orgEnfTenantOut]
	if out.Status != "error" || !strings.Contains(out.Message, "insufficient permissions") {
		t.Errorf("other-org tenant result = %+v, want the unchanged per-item permissions error", out)
	}
}

func TestOrgWriteEnforce_BatchTenants_Sync(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, nil)
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", orgEnfBatchBody(t))
	req.Header.Set("Content-Type", "application/json")
	req = orgEnfIdentity(req, orgEnfMemberOrg)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(BatchTenants(f.deps()), f.rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	orgEnfCheckBatchResults(t, resp.Results)
	if n := f.writes.Load(); n != 1 {
		t.Errorf("writer commits = %d, want exactly 1 (the member-org op only)", n)
	}
}

func TestOrgWriteEnforce_BatchTenants_Async(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, nil)
	taskMgr := async.NewManager(2)
	defer taskMgr.Close()
	d := f.deps()
	d.Tasks = taskMgr

	req := httptest.NewRequest("POST", "/api/v1/tenants/batch?async=true", orgEnfBatchBody(t))
	req.Header.Set("Content-Type", "application/json")
	req = orgEnfIdentity(req, orgEnfMemberOrg)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(BatchTenants(d), f.rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want 202; body=%s", w.Code, w.Body.String())
	}
	var resp map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	taskID, _ := resp["task_id"].(string)
	if taskID == "" {
		t.Fatal("task_id empty in async response")
	}

	final := pollUntilTerminal(t, taskMgr, taskID, 5*time.Second)
	results := make([]BatchResult, 0, len(final.Results))
	for _, r := range final.Results {
		results = append(results, BatchResult{TenantID: r.TenantID, Status: r.Status, Message: r.Message})
	}
	orgEnfCheckBatchResults(t, results)
	if n := f.writes.Load(); n != 1 {
		t.Errorf("writer commits = %d, want exactly 1 (the member-org op only)", n)
	}
}

// Site #4 (spec §6 T7, handler half): the async batch path must resolve each
// tenant's org list at EXECUTION time, not at submit time. A single-worker
// pool held by a blocker lets us hot-reload _tenant_orgs.yaml AFTER the HTTP
// request returned 202 but BEFORE the queued batch task runs. The tenant is
// labeled with a DIFFERENT org at submit (member would be denied) and
// relabeled to the member's org before execution — the op must end AUTHORIZED,
// which is only possible if the closure reads the live tenantorg manager in
// the loop. The stale-orgs mutant (pre-resolving orgs into the closure before
// Submit) captures the submit-time mapping and stays denied → this test fails
// it. The rbac-half sibling (TestAllowedInOrg_TenantorgSnapshotSwap) proves
// AllowedInOrg itself holds no snapshot state.
func TestOrgWriteEnforce_BatchTenants_Async_ResolvesOrgsAtExecutionTime(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, nil)
	// Single worker + a blocker task pin execution until we release it. The
	// batch task queues behind the blocker (workCh is FIFO), so the reload
	// below is guaranteed to land before the batch closure runs.
	taskMgr := async.NewManager(1)
	defer taskMgr.Close()
	release := make(chan struct{})
	taskMgr.Submit("blocker", func(ctx context.Context) ([]async.TaskResult, error) {
		<-release
		return nil, nil
	})

	// Submit-time mapping: target tenant is in a DIFFERENT org → member denied.
	f.tenantOrg.Override(&tenantorg.Config{TenantOrgs: map[string][]string{
		orgEnfTenantIn: {orgEnfOutsiderOrg},
	}})

	body, err := json.Marshal(BatchRequest{Operations: []BatchOperation{
		{TenantID: orgEnfTenantIn, Patch: map[string]string{"_silent_mode": "warning"}},
	}})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	d := f.deps()
	d.Tasks = taskMgr
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch?async=true", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req = orgEnfIdentity(req, orgEnfMemberOrg)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(BatchTenants(d), f.rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)
	if w.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want 202; body=%s", w.Code, w.Body.String())
	}
	var resp map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	taskID, _ := resp["task_id"].(string)
	if taskID == "" {
		t.Fatal("task_id empty in async response")
	}

	// Hot-reload AFTER submit, BEFORE execution: relabel to the member's org,
	// then release the worker so the batch task runs against the NEW mapping.
	f.tenantOrg.Override(&tenantorg.Config{TenantOrgs: map[string][]string{
		orgEnfTenantIn: {orgEnfMemberOrg},
	}})
	close(release)

	final := pollUntilTerminal(t, taskMgr, taskID, 5*time.Second)
	if len(final.Results) != 1 || final.Results[0].Status == "error" {
		t.Fatalf("execution-time resolution failed: op must be authorized after the reload, got %+v", final.Results)
	}
	if n := f.writes.Load(); n != 1 {
		t.Errorf("writer commits = %d, want exactly 1 (op authorized at execution time)", n)
	}
}

// PR-mode pre-validation (site #3): the inline OrgAllowed check must split
// the ops BEFORE any branch is written, and the resulting PR must contain
// only the authorized tenant.
func TestOrgWriteEnforce_BatchTenants_PRModePrevalidation(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, nil)
	var prTenants string
	client := &mockPlatformClient{
		createPRFunc: func(title, body, headBranch string, labels []string) (*platform.PRInfo, error) {
			prTenants = body
			return &platform.PRInfo{Number: 5, WebURL: "https://gh/5", State: "open", HeadRef: headBranch}, nil
		},
	}
	d := f.deps()
	d.WriteMode = WriteModePR
	d.PRClient = client
	d.PRTracker = &mockPlatformTracker{}

	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", orgEnfBatchBody(t))
	req.Header.Set("Content-Type", "application/json")
	req = orgEnfIdentity(req, orgEnfMemberOrg)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(BatchTenants(d), f.rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	byTenant := map[string]BatchResult{}
	for _, r := range resp.Results {
		byTenant[r.TenantID] = r
	}
	if got := byTenant[orgEnfTenantIn]; got.Status != "included" {
		t.Errorf("member-org tenant result = %+v, want included", got)
	}
	out := byTenant[orgEnfTenantOut]
	if out.Status != "error" || !strings.Contains(out.Message, "insufficient permissions") {
		t.Errorf("other-org tenant result = %+v, want the unchanged per-item permissions error", out)
	}
	if strings.Contains(prTenants, orgEnfTenantOut) {
		t.Errorf("PR body lists the DENIED tenant — pre-validation leaked it into the PR: %q", prTenants)
	}
	if !strings.Contains(prTenants, orgEnfTenantIn) {
		t.Errorf("PR body missing the authorized tenant: %q", prTenants)
	}
}

// ── Sites #1/#2: PutGroup / DeleteGroup member funnel ──────────────────────

func TestOrgWriteEnforce_PutGroup(t *testing.T) {
	t.Parallel()
	t.Run("outsider_member_denied_403_no_write", func(t *testing.T) {
		t.Parallel()
		f := newOrgEnfFixture(t, nil)
		body := bytes.NewBufferString(`{"label":"G","members":["` + orgEnfTenantOut + `"]}`)
		req := newRequestWithChiParam("PUT", "/api/v1/groups/g1", "id", "g1", body)
		req = orgEnfIdentity(req, orgEnfMemberOrg)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(PutGroup(f.deps()), f.rbacMgr, rbac.PermWrite, nil).ServeHTTP(w, req)

		if w.Code != http.StatusForbidden {
			t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
		}
		if !strings.Contains(w.Body.String(), orgEnfTenantOut) {
			t.Errorf("403 body should list the forbidden member id: %s", w.Body.String())
		}
		if n := f.writes.Load(); n != 0 {
			t.Errorf("denied request committed %d time(s), want 0", n)
		}
	})
	t.Run("member_tenants_allowed", func(t *testing.T) {
		t.Parallel()
		f := newOrgEnfFixture(t, nil)
		body := bytes.NewBufferString(`{"label":"G","members":["` + orgEnfTenantIn + `"]}`)
		req := newRequestWithChiParam("PUT", "/api/v1/groups/g1", "id", "g1", body)
		req = orgEnfIdentity(req, orgEnfMemberOrg)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(PutGroup(f.deps()), f.rbacMgr, rbac.PermWrite, nil).ServeHTTP(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
		}
		if n := f.writes.Load(); n != 1 {
			t.Errorf("writer commits = %d, want 1", n)
		}
	})
}

const orgEnfGroupsYAML = `groups:
  g-out:
    label: OtherOrg
    members: [` + orgEnfTenantOut + `]
  g-in:
    label: MemberOrg
    members: [` + orgEnfTenantIn + `]
`

func TestOrgWriteEnforce_DeleteGroup(t *testing.T) {
	t.Parallel()
	t.Run("outsider_member_denied_403_no_write", func(t *testing.T) {
		t.Parallel()
		f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": orgEnfGroupsYAML})
		req := newRequestWithChiParam("DELETE", "/api/v1/groups/g-out", "id", "g-out", nil)
		req = orgEnfIdentity(req, orgEnfMemberOrg)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(DeleteGroup(f.deps()), f.rbacMgr, rbac.PermWrite, nil).ServeHTTP(w, req)

		if w.Code != http.StatusForbidden {
			t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
		}
		if n := f.writes.Load(); n != 0 {
			t.Errorf("denied request committed %d time(s), want 0", n)
		}
	})
	t.Run("member_group_allowed", func(t *testing.T) {
		t.Parallel()
		f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": orgEnfGroupsYAML})
		req := newRequestWithChiParam("DELETE", "/api/v1/groups/g-in", "id", "g-in", nil)
		req = orgEnfIdentity(req, orgEnfMemberOrg)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(DeleteGroup(f.deps()), f.rbacMgr, rbac.PermWrite, nil).ServeHTTP(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
		}
		if n := f.writes.Load(); n != 1 {
			t.Errorf("writer commits = %d, want 1", n)
		}
	})
}

// ── Site #5: GroupBatch member loop ────────────────────────────────────────

const orgEnfMixedGroupYAML = `groups:
  g-mixed:
    label: Mixed
    members: [` + orgEnfTenantIn + `, ` + orgEnfTenantOut + `]
`

func TestOrgWriteEnforce_GroupBatch_Sync(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": orgEnfMixedGroupYAML})
	body := bytes.NewBufferString(`{"patch":{"_silent_mode":"warning"}}`)
	req := newRequestWithChiParam("POST", "/api/v1/groups/g-mixed/batch", "id", "g-mixed", body)
	req = orgEnfIdentity(req, orgEnfMemberOrg)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(GroupBatch(f.deps()), f.rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	var resp GroupBatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	orgEnfCheckBatchResults(t, resp.Results)
	if n := f.writes.Load(); n != 1 {
		t.Errorf("writer commits = %d, want exactly 1 (the member-org member only)", n)
	}
}

// ── Invariant I6: enforce must NOT change "*"-scope / non-tenant surfaces ──

// The tenant LIST route gate is the platform "*" read check — org-blind by
// design. Under enforce, an org-scoped-only caller still reaches the list
// (200); per-tenant visibility inside is ScopeAllowed's org filter.
func TestOrgWriteEnforce_StarSemantics_ListEndpoint(t *testing.T) {
	t.Parallel()
	tenantYAML := "tenants:\n  %s:\n    _silent_mode: \"critical\"\n"
	f := newOrgEnfFixture(t, map[string]string{
		orgEnfTenantIn + ".yaml":  strings.ReplaceAll(tenantYAML, "%s", orgEnfTenantIn),
		orgEnfTenantOut + ".yaml": strings.ReplaceAll(tenantYAML, "%s", orgEnfTenantOut),
	})
	req := httptest.NewRequest("GET", "/api/v1/tenants", nil)
	req = orgEnfIdentity(req, orgEnfMemberOrg)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(ListTenants(f.deps()), f.rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("org-scoped caller must pass the \"*\" list route gate under enforce: status = %d; body=%s",
			w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), orgEnfTenantIn) {
		t.Errorf("member-org tenant missing from list: %s", w.Body.String())
	}
	if strings.Contains(w.Body.String(), orgEnfTenantOut) {
		t.Errorf("other-org tenant visible in enforce mode list: %s", w.Body.String())
	}
}

// Site #5 async: GroupBatch's async path funnels through the same
// executeGroupBatchOps as the sync path (group_batch.go:99 vs :120), so the
// per-member org gate must hold after the HTTP request has returned. Mirrors
// the sync GroupBatch pin plus the async submit/poll cycle.
func TestOrgWriteEnforce_GroupBatch_Async(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": orgEnfMixedGroupYAML})
	taskMgr := async.NewManager(2)
	defer taskMgr.Close()
	d := f.deps()
	d.Tasks = taskMgr

	body := bytes.NewBufferString(`{"patch":{"_silent_mode":"warning"}}`)
	req := newRequestWithChiParam("POST", "/api/v1/groups/g-mixed/batch?async=true", "id", "g-mixed", body)
	req = orgEnfIdentity(req, orgEnfMemberOrg)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(GroupBatch(d), f.rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	if w.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want 202; body=%s", w.Code, w.Body.String())
	}
	var resp map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	taskID, _ := resp["task_id"].(string)
	if taskID == "" {
		t.Fatal("task_id empty in async response")
	}

	final := pollUntilTerminal(t, taskMgr, taskID, 5*time.Second)
	results := make([]BatchResult, 0, len(final.Results))
	for _, r := range final.Results {
		results = append(results, BatchResult{TenantID: r.TenantID, Status: r.Status, Message: r.Message})
	}
	orgEnfCheckBatchResults(t, results)
	if n := f.writes.Load(); n != 1 {
		t.Errorf("writer commits = %d, want exactly 1 (the member-org member only)", n)
	}
}

// Saved views are NOT tenant data — no per-tenant write decision exists, so
// the org axis must not affect a view write even in enforce mode (the route
// manifest carries the same exemption).
func TestOrgWriteEnforce_StarSemantics_ViewsWriteUnchanged(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, nil)
	body := bytes.NewBufferString(`{"label":"My view","filters":{"environment":"production"}}`)
	req := newRequestWithChiParam("PUT", "/api/v1/views/v1", "id", "v1", body)
	// Even the OUTSIDER org caller may write a view: nothing tenant-scoped.
	req = orgEnfIdentity(req, orgEnfOutsiderOrg)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutView(f.deps()), f.rbacMgr, rbac.PermWrite, nil).ServeHTTP(w, req)

	if w.Code == http.StatusForbidden {
		t.Fatalf("view write denied under enforce — org axis leaked into a non-tenant surface: %s", w.Body.String())
	}
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
}
