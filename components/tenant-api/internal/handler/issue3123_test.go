package handler

// #3123: list / stream endpoints gated on the route's `"*"` read check.
//
// A caller whose rule is `tenants: ["*"]` plus an org-scope passes that gate:
// rbac.Allowed holds the org and metadata axes at shadow, so `"*"` is not an
// isolation barrier and each handler must filter per tenant. Every test here
// uses that caller (orgEnfFixture / newOrgReadManager: org-scope ORG-ALPHA)
// and asserts that nothing belonging to an ORG-BETA tenant reaches it.

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/statestore"
	"github.com/vencil/tenant-api/internal/ws"
)

// ── item 1: GET /events ────────────────────────────────────────────────────

// i3123Events builds the subscriber filter EventStream gives an SSE request
// from identity (through the route's real RBAC middleware), and returns the
// events of sent that it admits — exactly what Broadcast would queue for that
// subscriber. No hub is built here: constructing one writes ws's process-global
// activeHub (#1279); the hub's own delivery through a filter is pinned in
// internal/ws (hub_filter_test.go) and the route wiring in cmd/server.
func i3123Events(t *testing.T, d *Deps, identity func(*http.Request) *http.Request, sent []ws.Event) []ws.Event {
	t.Helper()
	var allow ws.Filter
	req := identity(httptest.NewRequest("GET", "/api/v1/events", nil))
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(func(_ http.ResponseWriter, r *http.Request) {
		allow = eventFilterFor(d)(r)
	}, d.RBAC, rbac.PermRead, nil).ServeHTTP(w, req)
	if allow == nil {
		t.Fatalf("the route refused the stream: %d %s", w.Code, w.Body.String())
	}
	var out []ws.Event
	for _, evt := range sent {
		if allow(evt) {
			out = append(out, evt)
		}
	}
	return out
}

// #3123 item 1: every write was pushed to every subscriber, with its tenant id
// (and a state-store entity such as federation/<tenant>) in the event.
func TestIssue3123_Events_OtherOrgTenantNotStreamed(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, nil)
	alpha := func(r *http.Request) *http.Request { return orgEnfIdentity(r, orgEnfMemberOrg) }
	got := i3123Events(t, f.deps(), alpha, []ws.Event{
		ConfigChangeEvent(orgEnfTenantOut),
		StateChangeEvent(statestore.SubsetEntity(orgEnfTenantOut)),
		StateChangeEvent("groups"),
		ConfigChangeEvent(orgEnfTenantIn), // positive control
	})
	sawIn := false
	for _, evt := range got {
		raw, _ := json.Marshal(evt)
		if strings.Contains(string(raw), orgEnfTenantOut) {
			t.Errorf("ORG-ALPHA caller received an event naming an ORG-BETA tenant: %s", raw)
		}
		if evt.TenantID == "groups" {
			t.Errorf("caller without unrestricted read received an unmapped state event: %s", raw)
		}
		if evt.Type == "config_change" && evt.TenantID == orgEnfTenantIn {
			sawIn = true
		}
	}
	if !sawIn {
		t.Errorf("positive control: the caller's own tenant's event was not streamed; got %+v", got)
	}
}

// #3123 item 1: a caller who reads every tenant — open mode, or an unscoped
// read on "*" — still receives everything, the unmapped state events included.
func TestIssue3123_Events_UnrestrictedCallerGetsEverything(t *testing.T) {
	t.Parallel()
	for name, yaml := range map[string]string{
		"open_mode":       "",
		"unscoped_reader": "groups:\n  - name: readers\n    tenants: [\"*\"]\n    permissions: [read]\n",
	} {
		yaml := yaml
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			d := &Deps{RBAC: newRBACManager(t, yaml)}
			id := func(r *http.Request) *http.Request {
				r.Header.Set("X-Forwarded-Email", "reader@example.com")
				r.Header.Set("X-Forwarded-Groups", "readers")
				return r
			}
			got := i3123Events(t, d, id, []ws.Event{
				ConfigChangeEvent("tenant-any-3123"),
				StateChangeEvent("groups"),
			})
			seen := map[string]bool{}
			for _, evt := range got {
				seen[evt.TenantID] = true
			}
			if !seen["tenant-any-3123"] || !seen["groups"] {
				t.Errorf("unrestricted caller missed events; got %+v", got)
			}
		})
	}
}

// ── item 2: GET /prs ───────────────────────────────────────────────────────

const i3123BatchRef = platform.BranchPrefix + "batch/20261017-101010"

func i3123ListPRs(t *testing.T, mgr *rbac.Manager, d *Deps, identity func(*http.Request) *http.Request, query string) string {
	t.Helper()
	req := httptest.NewRequest("GET", "/api/v1/prs"+query, nil)
	req = identity(req)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(ListPRs(d), mgr, rbac.PermRead, nil).ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("GET /prs%s = %d; body=%s", query, w.Code, w.Body.String())
	}
	return w.Body.String()
}

// #3123 item 2, as registered: createPRAndRegister enters a batch PR once per
// covered tenant, so a caller who can read ONE of them saw the PR (its URL,
// and a title/body describing every covered tenant).
func TestIssue3123_PRs_BatchPRNeedsEveryCoveredTenant(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, nil)
	tracker := newTestTracker(t, nil)
	pr := platform.PRInfo{Number: 77, WebURL: "https://forge.example/pr/77", State: "open", HeadRef: i3123BatchRef}
	d := &Deps{RBAC: f.rbacMgr, TenantOrg: f.tenantOrg, PRTracker: tracker,
		PRClient: &mockPlatformClient{createPRFunc: func(string, string, string, []string) (*platform.PRInfo, error) {
			p := pr
			return &p, nil
		}}}
	// Exactly what a PR-mode batch over both tenants registers (runBatchPR).
	if _, err := createPRAndRegister(d, "t", "b", i3123BatchRef, nil, []string{orgEnfTenantIn, orgEnfTenantOut}); err != nil {
		t.Fatal(err)
	}

	id := func(r *http.Request) *http.Request { return orgEnfIdentity(r, orgEnfMemberOrg) }
	for _, q := range []string{"", "?tenant=" + orgEnfTenantIn} {
		if body := i3123ListPRs(t, f.rbacMgr, d, id, q); strings.Contains(body, pr.WebURL) {
			t.Errorf("GET /prs%s: ORG-ALPHA caller sees a batch PR that also covers an ORG-BETA tenant: %s", q, body)
		}
	}
}

// #3123 item 2, after a forge sync: the branch tenant-api/batch/<ts> parsed to
// TenantID "batch", which any rule matching "batch" can read (here the
// org-scoped "*" rule in shadow mode, where "batch" is an unlabeled tenant).
func TestIssue3123_PRs_SyncedBatchPRHidden(t *testing.T) {
	t.Parallel()
	mgr, torg, _ := newOrgReadManager(t, orgReadRBACYAML, false)
	pr := platform.PRInfo{Number: 78, WebURL: "https://forge.example/pr/78", State: "open", HeadRef: i3123BatchRef}
	tracker := newTestTracker(t, []platform.PRInfo{pr})
	tracker.Sync()
	d := &Deps{RBAC: mgr, TenantOrg: torg, PRTracker: tracker}
	id := func(r *http.Request) *http.Request { return orgReadIdentity(r, orgReadMemberOrg) }
	for _, q := range []string{"", "?tenant=batch"} {
		if body := i3123ListPRs(t, mgr, d, id, q); strings.Contains(body, pr.WebURL) {
			t.Errorf("GET /prs%s: batch PR whose tenants are unknown is visible to a scoped caller: %s", q, body)
		}
	}
}

// #3123 item 2: the tenant list tenant-api recorded when it created a batch PR
// survives a forge sync, and a caller who reads every covered tenant sees the
// PR (once, with its tenants), on the bulk and the ?tenant= path.
func TestIssue3123_PRs_RecordedTenantsSurviveSync(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, nil)
	pr := platform.PRInfo{Number: 79, WebURL: "https://forge.example/pr/79", State: "open", HeadRef: i3123BatchRef}
	tracker := newTestTracker(t, []platform.PRInfo{pr})
	d := &Deps{RBAC: f.rbacMgr, TenantOrg: f.tenantOrg, PRTracker: tracker,
		PRClient: &mockPlatformClient{createPRFunc: func(string, string, string, []string) (*platform.PRInfo, error) {
			p := pr
			return &p, nil
		}}}
	if _, err := createPRAndRegister(d, "t", "b", i3123BatchRef, nil, []string{orgEnfTenantIn, orgEnfTenantOut}); err != nil {
		t.Fatal(err)
	}
	tracker.Sync()
	id := func(r *http.Request) *http.Request { return orgEnfIdentity(r, orgEnfMemberOrg) }
	if body := i3123ListPRs(t, f.rbacMgr, d, id, ""); strings.Contains(body, pr.WebURL) {
		t.Errorf("after sync, the batch PR is visible to a caller who cannot read every covered tenant: %s", body)
	}
	f.shareOut(t) // now ORG-ALPHA reads both tenants
	// Only the bulk path: after a sync the tracker no longer resolves a
	// covered tenant to its batch PR (PendingPRForTenant), as before #3123.
	for _, q := range []string{""} {
		var resp PRListResponse
		if err := json.Unmarshal([]byte(i3123ListPRs(t, f.rbacMgr, d, id, q)), &resp); err != nil {
			t.Fatal(err)
		}
		if resp.Count != 1 || len(resp.PendingPRs[0].Tenants) != 2 || resp.PendingPRs[0].TenantID != "" {
			t.Errorf("GET /prs%s for a caller reading every covered tenant = %+v, want the one batch PR with its 2 tenants", q, resp)
		}
	}
}

// #3123 item 2: "batch" is reserved as a tenant id on the write plane.
func TestIssue3123_BatchIsAReservedTenantID(t *testing.T) {
	t.Parallel()
	if err := ValidateWritableTenantID(platform.BatchTenantID); err == nil {
		t.Errorf("ValidateWritableTenantID(%q) = nil, want an error", platform.BatchTenantID)
	}
	if err := ValidateTenantID(platform.BatchTenantID); err != nil {
		t.Errorf("reads must keep accepting an existing tenant of that name: %v", err)
	}
}

// ── item 3a / 3b: PUT /groups/{id} and empty groups ───────────────────────

// #3123 item 3a: PUT on an existing group the caller cannot see answered 403
// "member tenants you cannot view" while a free id answered 200 — an oracle
// for group ids and for "has members you cannot see".
func TestIssue3123_PutGroup_InvisibleExistingGroupIs409(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": visGroupsYAML +
		"  g-out2:\n    label: Other2\n    members: [" + orgEnfTenantOut + ", t-unlabeled-3123]\n"})
	d := f.deps()
	body := `{"label":"x","members":["` + orgEnfTenantIn + `"]}`

	w1 := visCall(t, f, d, PutGroup(d), rbac.PermWrite, "PUT", "g-out", body)
	w2 := visCall(t, f, d, PutGroup(d), rbac.PermWrite, "PUT", "g-out2", body)
	if w1.Code != http.StatusConflict {
		t.Fatalf("PUT on an invisible existing group: status = %d, want 409; body=%s", w1.Code, w1.Body.String())
	}
	var env ErrorResponse
	_ = json.Unmarshal(w1.Body.Bytes(), &env)
	if env.Code != CodeGroupIDUnavailable {
		t.Errorf("code = %q, want %q", env.Code, CodeGroupIDUnavailable)
	}
	if strings.Contains(w1.Body.String(), orgEnfTenantOut) || strings.Contains(w1.Body.String(), "cannot view") {
		t.Errorf("409 says something about the stored members: %s", w1.Body.String())
	}
	// Byte-identical to the shared helper's output, whatever the group holds.
	want := i3123GroupIDUnavailableBody()
	for _, w := range []*httptest.ResponseRecorder{w1, w2} {
		if w.Code != http.StatusConflict || w.Body.String() != want {
			t.Errorf("409 body = %d %q, want %q", w.Code, w.Body.String(), want)
		}
	}
	if got := storedMembers(t, f, "g-out"); len(got) != 1 || got[0] != orgEnfTenantOut {
		t.Errorf("g-out members on disk = %v, want unchanged", got)
	}
}

// i3123GroupIDUnavailableBody is what writeGroupIDUnavailable writes for a
// request with no request id — the body every such 409 must match byte for byte.
func i3123GroupIDUnavailableBody() string {
	w := httptest.NewRecorder()
	writeGroupIDUnavailable(w, httptest.NewRequest("PUT", "/api/v1/groups/x", nil))
	return w.Body.String()
}

const i3123EmptyGroupYAML = "groups:\n  g-empty:\n    label: E\n    members: []\n"

// #3123 item 3b: an empty group had no member to check, so every writer could
// overwrite or delete it, and every reader could see it.
func TestIssue3123_EmptyGroup_HiddenFromScopedCaller(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, map[string]string{"_groups.yaml": i3123EmptyGroupYAML})
	d := f.deps()

	missing := visCall(t, f, d, GetGroup(d), rbac.PermRead, "GET", "g-none", "")
	get := visCall(t, f, d, GetGroup(d), rbac.PermRead, "GET", "g-empty", "")
	if get.Code != http.StatusNotFound || strings.ReplaceAll(get.Body.String(), "g-empty", "g-none") != missing.Body.String() {
		t.Errorf("GET empty group = %d %s, want the missing-group 404 %s", get.Code, get.Body.String(), missing.Body.String())
	}

	req := httptest.NewRequest("GET", "/api/v1/groups", nil)
	req = orgEnfIdentity(req, orgEnfMemberOrg)
	lw := httptest.NewRecorder()
	wrapWithRBACMiddleware(ListGroups(d), f.rbacMgr, rbac.PermRead, nil).ServeHTTP(lw, req)
	if strings.Contains(lw.Body.String(), "g-empty") {
		t.Errorf("LIST shows the empty group: %s", lw.Body.String())
	}

	asMissing := func(w *httptest.ResponseRecorder) bool {
		return w.Code == http.StatusNotFound && strings.ReplaceAll(w.Body.String(), "g-empty", "g-none") == missing.Body.String()
	}
	if w := visCall(t, f, d, GroupBatch(d), rbac.PermRead, "POST+batch", "g-empty", `{"patch":{"_silent_mode":"warning"}}`); !asMissing(w) {
		t.Errorf("POST /groups/g-empty/batch = %d %s, want the missing-group 404", w.Code, w.Body.String())
	}
	if w := visCall(t, f, d, PutGroup(d), rbac.PermWrite, "PUT", "g-empty", `{"label":"x","members":["`+orgEnfTenantIn+`"]}`); w.Code != http.StatusConflict || w.Body.String() != i3123GroupIDUnavailableBody() {
		t.Errorf("PUT over the empty group = %d %s, want the GROUP_ID_UNAVAILABLE 409", w.Code, w.Body.String())
	}
	if w := visCall(t, f, d, DeleteGroup(d), rbac.PermWrite, "DELETE", "g-empty", ""); !asMissing(w) {
		t.Errorf("DELETE the empty group = %d %s, want the missing-group 404", w.Code, w.Body.String())
	}
	stored, err := os.ReadFile(filepath.Join(f.configDir, "_groups.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(stored), "g-empty:") || strings.Contains(string(stored), orgEnfTenantIn) {
		t.Errorf("g-empty was removed or overwritten by a scoped caller: %s", stored)
	}
}

// #3123 item 3b: a PUT may no longer create or leave an empty group.
func TestIssue3123_PutGroup_EmptyMembersRefused(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, nil)
	d := f.deps()
	if w := visCall(t, f, d, PutGroup(d), rbac.PermWrite, "PUT", "g-new", `{"label":"x","members":[]}`); w.Code != http.StatusBadRequest {
		t.Errorf("PUT with members [] = %d %s, want 400", w.Code, w.Body.String())
	}
	if w := visCall(t, f, d, PutGroup(d), rbac.PermWrite, "PUT", "g-new", `{"label":"x"}`); w.Code != http.StatusBadRequest {
		t.Errorf("PUT without members = %d %s, want 400", w.Code, w.Body.String())
	}
}

// ── item 5: GET /tenants and /tenants/search 500 ──────────────────────────

// #3123 item 5: a duplicate tenant file fails the listing with an error naming
// the tenant and both files. The client must get a fixed text.
func TestIssue3123_TenantList500DoesNotNameTenants(t *testing.T) {
	t.Parallel()
	const dup = "tenant-dup-3123"
	f := newOrgEnfFixture(t, map[string]string{
		dup + ".yaml": "tenants:\n  " + dup + ":\n    cpu: \"70\"\n",
		dup + ".yml":  "tenants:\n  " + dup + ":\n    cpu: \"80\"\n",
	})
	d := f.deps()
	d.SearchCache = NewTenantSnapshotCache()
	for _, tc := range []struct {
		path string
		h    http.HandlerFunc
	}{{"/api/v1/tenants", ListTenants(d)}, {"/api/v1/tenants/search", SearchTenants(d)}} {
		req := httptest.NewRequest("GET", tc.path, nil)
		req = orgEnfIdentity(req, orgEnfMemberOrg)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(tc.h, f.rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)
		if w.Code != http.StatusInternalServerError {
			t.Fatalf("%s = %d, want 500; body=%s", tc.path, w.Code, w.Body.String())
		}
		if strings.Contains(w.Body.String(), dup) || strings.Contains(w.Body.String(), ".yml") {
			t.Errorf("%s 500 names tenants / files: %s", tc.path, w.Body.String())
		}
	}
}

// ── item 6: /tasks/{id} ───────────────────────────────────────────────────

// mustSubmit submits fn as p's task.
func mustSubmit(t *testing.T, m *async.Manager, id string, p *rbac.VerifiedPrincipal, fn async.TaskFunc) *async.Task {
	t.Helper()
	task, err := m.Submit(id, taskOwner(p), fn)
	if err != nil {
		t.Fatalf("Submit %s: %v", id, err)
	}
	return task
}

var i3123OldTaskID = regexp.MustCompile(`^(group-)?batch-`)

func i3123GetTask(t *testing.T, mgr *rbac.Manager, d *Deps, taskID string, identity func(*http.Request) *http.Request) *httptest.ResponseRecorder {
	t.Helper()
	req := newRequestWithChiParam("GET", "/api/v1/tasks/"+taskID, "id", taskID, nil)
	req = identity(req)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(GetTask(d), mgr, rbac.PermRead, nil).ServeHTTP(w, req)
	return w
}

// #3123 item 6a/6b: a task is visible to its submitter only. Another caller
// got the task as-is while it had no results (6a) and a 403 — "it exists" —
// once its results named only tenants it cannot read (6b).
func TestIssue3123_Task_OnlySubmitterSeesIt(t *testing.T) {
	t.Parallel()
	f := newOrgEnfFixture(t, nil)
	tasks := async.NewManager(2) // one worker stays blocked on the running task
	defer tasks.Close()
	d := f.deps()
	d.Tasks = tasks

	submitter := &rbac.VerifiedPrincipal{Subject: "submitter@example.com", Source: rbac.SourceHumanHopB}
	release := make(chan struct{})
	running := mustSubmit(t, tasks, "t3123-running", submitter, func(ctx context.Context) ([]async.TaskResult, error) {
		<-release
		return nil, nil
	})
	done := mustSubmit(t, tasks, "t3123-done", submitter, func(context.Context) ([]async.TaskResult, error) {
		return []async.TaskResult{{TenantID: orgEnfTenantOut, Status: "ok"}}, nil
	})
	defer close(release)
	pollUntilTerminal(t, tasks, done.ID, 5*time.Second)

	other := func(r *http.Request) *http.Request { return orgEnfIdentity(r, orgEnfMemberOrg) }
	missing := i3123GetTask(t, f.rbacMgr, d, "t3123-none", other)
	// The shared helper's body, byte for byte (it names no task id).
	canon := httptest.NewRecorder()
	writeTaskNotFound(canon, httptest.NewRequest("GET", "/api/v1/tasks/x", nil))
	if missing.Code != http.StatusNotFound || missing.Body.String() != canon.Body.String() {
		t.Fatalf("missing task = %d %q, want %q", missing.Code, missing.Body.String(), canon.Body.String())
	}
	for _, id := range []string{running.ID, done.ID} {
		w := i3123GetTask(t, f.rbacMgr, d, id, other)
		if w.Code != http.StatusNotFound || w.Body.String() != canon.Body.String() {
			t.Errorf("GET /tasks/%s as another caller = %d %s, want the missing-task 404 %s", id, w.Code, w.Body.String(), canon.Body.String())
		}
	}

	// The submitter sees its own tasks; results still filtered by its CURRENT
	// read permission (here it cannot read the ORG-BETA tenant).
	self := func(r *http.Request) *http.Request {
		r = orgEnfIdentity(r, orgEnfMemberOrg)
		r.Header.Set("X-Forwarded-Email", submitter.Subject)
		return r
	}
	if w := i3123GetTask(t, f.rbacMgr, d, running.ID, self); w.Code != http.StatusOK {
		t.Errorf("submitter GET running task = %d %s, want 200", w.Code, w.Body.String())
	}
	if w := i3123GetTask(t, f.rbacMgr, d, done.ID, self); w.Code != http.StatusOK || strings.Contains(w.Body.String(), orgEnfTenantOut) {
		t.Errorf("submitter GET done task = %d %s, want 200 without the unreadable tenant", w.Code, w.Body.String())
	}
}

// #3123 item 6: task ids were predictable (batch-<yyyymmdd>-<op count>), and
// two async batches on one day with the same op count shared an id —
// async.Manager.Submit returned the FIRST task for the second, so the second
// batch never ran and its caller polled someone else's task.
func TestIssue3123_AsyncBatch_TaskIDsUniqueAndBothRun(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	tasks := async.NewManager(2)
	defer tasks.Close()
	mgr := newRBACManager(t, "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read, write]\n")
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, State: mustDirState(configDir),
		RBAC: mgr, WriteMode: WriteModeDirect, Tasks: tasks}

	post := func(tenant string) string {
		body := `{"operations":[{"tenant_id":"` + tenant + `","patch":{"_silent_mode":"warning"}}]}`
		req := httptest.NewRequest("POST", "/api/v1/tenants/batch?async=true", bytes.NewBufferString(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Forwarded-Email", "ops@example.com")
		req.Header.Set("X-Forwarded-Groups", "ops")
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(BatchTenants(d), mgr, rbac.PermWrite, nil).ServeHTTP(w, req)
		if w.Code != http.StatusAccepted {
			t.Fatalf("async batch for %s = %d; body=%s", tenant, w.Code, w.Body.String())
		}
		var resp map[string]any
		_ = json.Unmarshal(w.Body.Bytes(), &resp)
		id, _ := resp["task_id"].(string)
		return id
	}
	idA := post("tenant-3123-a")
	idB := post("tenant-3123-b")
	if idA == idB {
		t.Fatalf("two async batches got the same task id %q", idA)
	}
	for _, id := range []string{idA, idB} {
		if i3123OldTaskID.MatchString(id) || len(id) < 32 {
			t.Errorf("task id %q is predictable / shorter than 128 bits of hex", id)
		}
	}
	for id, tenant := range map[string]string{idA: "tenant-3123-a", idB: "tenant-3123-b"} {
		task := pollUntilTerminal(t, tasks, id, 10*time.Second)
		if len(task.Results) != 1 || task.Results[0].TenantID != tenant {
			t.Errorf("task %s results = %+v, want one result for %s", id, task.Results, tenant)
		}
	}
}
