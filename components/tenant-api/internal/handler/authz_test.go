package handler

// ============================================================
// Tests for v2.8.0 Phase B Track C (B-6) PR-2 tenant-scoped
// authz hardening:
//
//   * tenantsLackingPermission helper invariants
//   * PutGroup forbidden-member rejection
//   * DeleteGroup forbidden-member rejection
//   * GetTask result filtering by tenant access
//   * GetTask 403 when caller has zero accessible results
//   * ListPRs filters non-accessible-tenant PRs out of bulk list
//   * ListPRs returns empty (not 403) when ?tenant=<forbidden>
//
// The 403-not-leak-existence pattern in ListPRs is deliberate
// and tested explicitly — it's a security UX choice (see pr.go
// docstring) that future refactors might naively "fix" by
// returning 403, accidentally regressing the tenant-existence
// oracle.
// ============================================================

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
)

// ─────────────────────────────────────────────────────────────────
// tenantsLackingPermission unit tests
// ─────────────────────────────────────────────────────────────────

func TestTenantsLackingPermission_EmptyInput(t *testing.T) {
	rbacMgr := newRBACManager(t, "")
	if got := tenantsLackingPermission(rbacMgr, []string{"any"}, nil, rbac.PermWrite); len(got) != 0 {
		t.Errorf("nil tenant list: got %v, want empty", got)
	}
	if got := tenantsLackingPermission(rbacMgr, []string{"any"}, []string{}, rbac.PermWrite); len(got) != 0 {
		t.Errorf("empty tenant list: got %v, want empty", got)
	}
}

func TestTenantsLackingPermission_OpenModeRead(t *testing.T) {
	// Open-mode RBAC grants PermRead to any caller. tenantsLackingPermission
	// for PermRead in open mode returns empty (no restrictions).
	rbacMgr := newRBACManager(t, "")
	got := tenantsLackingPermission(rbacMgr, []string{"any"}, []string{"db-a", "db-b"}, rbac.PermRead)
	if len(got) != 0 {
		t.Errorf("open-mode read: got %v, want empty", got)
	}
}

func TestTenantsLackingPermission_OpenModeWriteRejectsAll(t *testing.T) {
	// Open-mode RBAC does NOT grant PermWrite (intentional —
	// missing _rbac.yaml is a pre-prod state, writes should fail).
	rbacMgr := newRBACManager(t, "")
	got := tenantsLackingPermission(rbacMgr, []string{"any"}, []string{"db-a", "db-b"}, rbac.PermWrite)
	if len(got) != 2 {
		t.Errorf("open-mode write: got %v, want all 2 forbidden", got)
	}
}

func TestTenantsLackingPermission_GrantedReadButNotWrite(t *testing.T) {
	// Caller can READ db-a but cannot WRITE — ensure the helper
	// distinguishes by `want`.
	rbacMgr := newRBACManager(t, `groups:
  - name: viewers
    tenants: ["db-a", "db-b"]
    permissions: [read]
`)
	idpGroups := []string{"viewers"}

	read := tenantsLackingPermission(rbacMgr, idpGroups, []string{"db-a", "db-b"}, rbac.PermRead)
	if len(read) != 0 {
		t.Errorf("read check: got %v, want empty", read)
	}
	write := tenantsLackingPermission(rbacMgr, idpGroups, []string{"db-a", "db-b"}, rbac.PermWrite)
	if len(write) != 2 {
		t.Errorf("write check: got %v, want both forbidden", write)
	}
}

func TestTenantsLackingPermission_PartialAccess(t *testing.T) {
	// Caller can write db-a but not db-b — return ONLY db-b.
	rbacMgr := newRBACManager(t, `groups:
  - name: dba-team-a
    tenants: ["db-a"]
    permissions: [admin]
`)
	idpGroups := []string{"dba-team-a"}

	got := tenantsLackingPermission(rbacMgr, idpGroups, []string{"db-a", "db-b"}, rbac.PermWrite)
	if len(got) != 1 || got[0] != "db-b" {
		t.Errorf("partial access: got %v, want [db-b]", got)
	}
}

func TestTenantsLackingPermission_DeduplicatesInput(t *testing.T) {
	// Duplicate IDs in input → no duplicates in forbidden list.
	rbacMgr := newRBACManager(t, "")
	got := tenantsLackingPermission(rbacMgr, []string{"any"},
		[]string{"db-a", "db-a", "db-b", "db-a"}, rbac.PermWrite)
	if len(got) != 2 {
		t.Errorf("dedup: got %v, want 2 unique forbidden", got)
	}
	seen := map[string]int{}
	for _, id := range got {
		seen[id]++
	}
	for id, n := range seen {
		if n > 1 {
			t.Errorf("dedup: id %q appears %d times in forbidden list", id, n)
		}
	}
}

func TestTenantsLackingPermission_SkipsEmptyIDs(t *testing.T) {
	// Empty-string IDs in input are silently skipped (defensive
	// — shouldn't happen but caller may have stray "" entries).
	rbacMgr := newRBACManager(t, "")
	got := tenantsLackingPermission(rbacMgr, []string{"any"},
		[]string{"", "db-a", ""}, rbac.PermWrite)
	if len(got) != 1 || got[0] != "db-a" {
		t.Errorf("empty-skip: got %v, want [db-a]", got)
	}
}

// ─────────────────────────────────────────────────────────────────
// PutGroup forbidden-member rejection
// ─────────────────────────────────────────────────────────────────

func TestPutGroup_ForbiddenMember_Returns403(t *testing.T) {
	// Caller has admin on db-a but NOT db-b. Group request includes
	// both members → 403, with both… er, one (db-b) listed as forbidden.
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)

	rbacYAML := `groups:
  - name: dba-team-a
    tenants: ["db-a"]
    permissions: [admin]
`
	rbacMgr := newRBACManager(t, rbacYAML)

	body := `{"label":"Mixed","members":["db-a","db-b"]}`
	req := newRequestWithChiParam("PUT", "/api/v1/groups/mixed", "id", "mixed",
		bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := servePopulatingRBAC(t, PutGroup(mgr, writer, rbacMgr), req,
		"alice@example.com", []string{"dba-team-a"})

	if w.Code != http.StatusForbidden {
		t.Fatalf("PutGroup with forbidden member: status = %d, want 403; body = %s",
			w.Code, w.Body.String())
	}
	var resp map[string]string
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatalf("response not JSON: %v", err)
	}
	if got := resp["error"]; got == "" || !contains(got, "db-b") {
		t.Errorf("error message must list forbidden tenant db-b; got %q", got)
	}
	if contains(resp["error"], "db-a") {
		t.Errorf("error message must NOT list permitted tenant db-a; got %q", resp["error"])
	}
}

func TestPutGroup_AllMembersForbidden_ListsAllInError(t *testing.T) {
	// Caller has zero permission. Group with 3 members → all 3 in
	// the error message so operator can fix in one round-trip
	// (ergonomics: discover-all-failures-up-front).
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)

	rbacMgr := newRBACManager(t, `groups: []` + "\n")

	body := `{"label":"All Forbidden","members":["db-a","db-b","db-c"]}`
	req := newRequestWithChiParam("PUT", "/api/v1/groups/forbidden", "id", "forbidden",
		bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := servePopulatingRBAC(t, PutGroup(mgr, writer, rbacMgr), req,
		"alice@example.com", []string{"unprivileged"})

	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", w.Code)
	}
	var resp map[string]string
	_ = json.NewDecoder(w.Body).Decode(&resp)
	for _, tid := range []string{"db-a", "db-b", "db-c"} {
		if !contains(resp["error"], tid) {
			t.Errorf("error message must list %q; got %q", tid, resp["error"])
		}
	}
}

// ─────────────────────────────────────────────────────────────────
// DeleteGroup forbidden-member rejection
// ─────────────────────────────────────────────────────────────────

func TestDeleteGroup_ForbiddenMember_Returns403(t *testing.T) {
	// Pre-existing group with members caller can't write → DELETE
	// fails 403. Without this check, a malicious operator could
	// destroy a group whose members they don't own (DoS).
	configDir := setupConfigDir(t, nil)
	setupGroupsFile(t, configDir, `groups:
  cross-team:
    label: "Cross team"
    members: ["db-a", "db-b"]
`)
	initGitRepo(t, configDir)
	mgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)

	// Caller has admin on db-a but not db-b.
	rbacMgr := newRBACManager(t, `groups:
  - name: dba-team-a
    tenants: ["db-a"]
    permissions: [admin]
`)

	req := newRequestWithChiParam("DELETE", "/api/v1/groups/cross-team", "id", "cross-team", nil)
	w := servePopulatingRBAC(t, DeleteGroup(mgr, writer, rbacMgr), req,
		"alice@example.com", []string{"dba-team-a"})

	if w.Code != http.StatusForbidden {
		t.Fatalf("DeleteGroup forbidden member: status = %d, want 403; body = %s",
			w.Code, w.Body.String())
	}
}

// ─────────────────────────────────────────────────────────────────
// GetTask filtering
// ─────────────────────────────────────────────────────────────────

func TestGetTask_FiltersResultsByTenantAccess(t *testing.T) {
	// Task touched 3 tenants. Caller can read 2 → response omits
	// the third (info disclosure scrubbed).
	taskMgr := async.NewManager(2)

	rbacMgr := newRBACManager(t, `groups:
  - name: viewers
    tenants: ["db-a", "db-b"]
    permissions: [read]
`)

	task := taskMgr.Submit("test-task", func(ctx context.Context) ([]async.TaskResult, error) {
		return []async.TaskResult{
			{TenantID: "db-a", Status: "ok"},
			{TenantID: "db-b", Status: "error", Message: "conflict"},
			{TenantID: "db-secret", Status: "ok"},
		}, nil
	})
	// Wait for task completion.
	for i := 0; i < 50; i++ {
		if cur, ok := taskMgr.Get(task.ID); ok && cur.Status == async.TaskCompleted {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	taskID := task.ID

	req := newRequestWithChiParam("GET", "/api/v1/tasks/"+taskID, "id", taskID, nil)
	w := servePopulatingRBAC(t, GetTask(taskMgr, rbacMgr), req,
		"alice@example.com", []string{"viewers"})

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body = %s", w.Code, w.Body.String())
	}
	var resp async.Task
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(resp.Results) != 2 {
		t.Errorf("filtered results = %d, want 2 (db-a + db-b only)", len(resp.Results))
	}
	for _, r := range resp.Results {
		if r.TenantID == "db-secret" {
			t.Errorf("forbidden tenant db-secret leaked into response")
		}
	}
}

func TestGetTask_NoAccessibleResults_Returns403(t *testing.T) {
	// Task touched a tenant the caller cannot read → 403, not 200
	// with empty results, not 404. The task DOES exist but the
	// caller has no business knowing about it.
	taskMgr := async.NewManager(2)

	rbacMgr := newRBACManager(t, `groups:
  - name: viewers
    tenants: ["db-a"]
    permissions: [read]
`)

	task := taskMgr.Submit("test-task", func(ctx context.Context) ([]async.TaskResult, error) {
		return []async.TaskResult{{TenantID: "db-secret", Status: "ok"}}, nil
	})
	for i := 0; i < 50; i++ {
		if cur, ok := taskMgr.Get(task.ID); ok && cur.Status == async.TaskCompleted {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	taskID := task.ID

	req := newRequestWithChiParam("GET", "/api/v1/tasks/"+taskID, "id", taskID, nil)
	w := servePopulatingRBAC(t, GetTask(taskMgr, rbacMgr), req,
		"alice@example.com", []string{"viewers"})

	if w.Code != http.StatusForbidden {
		t.Fatalf("zero-access task: status = %d, want 403; body = %s", w.Code, w.Body.String())
	}
}

// ─────────────────────────────────────────────────────────────────
// ListPRs filtering
// ─────────────────────────────────────────────────────────────────

// fakeTracker is a stub implementation of platform.Tracker used by
// ListPRs tests. Returns whatever PRs the test plants; doesn't
// actually call any GitHub/GitLab API.
type fakeTracker struct {
	pending []platform.PRInfo
}

func (f *fakeTracker) PendingPRs() []platform.PRInfo { return f.pending }
func (f *fakeTracker) PendingPRForTenant(tenantID string) (platform.PRInfo, bool) {
	for _, p := range f.pending {
		if p.TenantID == tenantID {
			return p, true
		}
	}
	return platform.PRInfo{}, false
}
func (f *fakeTracker) HasPendingPR(tenantID string) bool {
	_, ok := f.PendingPRForTenant(tenantID)
	return ok
}
func (f *fakeTracker) RegisterPR(pr platform.PRInfo) {
	f.pending = append(f.pending, pr)
}
func (f *fakeTracker) LastSyncTime() time.Time          { return time.Now() }
func (f *fakeTracker) WatchLoop(stopCh <-chan struct{}) {}

func TestListPRs_FiltersBulkListByTenantAccess(t *testing.T) {
	tracker := &fakeTracker{pending: []platform.PRInfo{
		{Number: 1, TenantID: "db-a"},
		{Number: 2, TenantID: "db-secret"},
		{Number: 3, TenantID: "db-b"},
	}}
	rbacMgr := newRBACManager(t, `groups:
  - name: viewers
    tenants: ["db-a", "db-b"]
    permissions: [read]
`)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/prs", nil)
	w := servePopulatingRBAC(t, ListPRs(tracker, rbacMgr), req,
		"alice@example.com", []string{"viewers"})

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}
	var resp PRListResponse
	_ = json.NewDecoder(w.Body).Decode(&resp)
	if resp.Count != 2 {
		t.Errorf("count = %d, want 2 (db-a + db-b only)", resp.Count)
	}
	for _, p := range resp.PendingPRs {
		if p.TenantID == "db-secret" {
			t.Errorf("forbidden tenant db-secret leaked into PR list")
		}
	}
}

func TestListPRs_TenantQueryReturnsEmptyWhenForbidden(t *testing.T) {
	// `?tenant=db-secret` from a caller without read access:
	// returns empty list, NOT 403. This is deliberate — 403 would
	// reveal "tenant exists" via the access check, while empty
	// list is indistinguishable from "no pending PR for that
	// tenant" (the API shape).
	tracker := &fakeTracker{pending: []platform.PRInfo{
		{Number: 99, TenantID: "db-secret"},
	}}
	rbacMgr := newRBACManager(t, `groups:
  - name: viewers
    tenants: ["db-a"]
    permissions: [read]
`)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/prs?tenant=db-secret", nil)
	w := servePopulatingRBAC(t, ListPRs(tracker, rbacMgr), req,
		"alice@example.com", []string{"viewers"})

	if w.Code != http.StatusOK {
		t.Errorf("forbidden tenant query: status = %d, want 200 (empty list, no leak)", w.Code)
	}
	var resp PRListResponse
	_ = json.NewDecoder(w.Body).Decode(&resp)
	if resp.Count != 0 {
		t.Errorf("forbidden tenant query: count = %d, want 0", resp.Count)
	}
}

// ─────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────

// servePopulatingRBAC drives `inner` through a no-op RBAC middleware
// (open-mode manager, PermRead) so that `rbac.RequestEmail` and
// `rbac.RequestGroups` see the values inside `inner` — the same way
// production handlers see them after the route-level RBAC middleware
// runs. We cannot just `req.WithContext(rbacContextHelper(...))`
// because chi URL params live on the request context too, and
// constructing a fresh context discards them. Wrapping the whole
// inner handler preserves chi params (req is mutated in place).
//
// `email` and `idpGroups` are injected as the X-Forwarded-* headers
// the production middleware reads. The middleware writes them onto
// the request context before delegating to `inner`.
func servePopulatingRBAC(t *testing.T, inner http.HandlerFunc, req *http.Request, email string, idpGroups []string) *httptest.ResponseRecorder {
	t.Helper()
	req.Header.Set("X-Forwarded-Email", email)
	if len(idpGroups) > 0 {
		req.Header.Set("X-Forwarded-Groups", joinGroups(idpGroups))
	}
	mgr, _ := rbac.NewManager("")
	wrapped := mgr.Middleware(rbac.PermRead, nil)(inner)
	rec := httptest.NewRecorder()
	wrapped.ServeHTTP(rec, req)
	return rec
}

func joinGroups(g []string) string {
	if len(g) == 0 {
		return ""
	}
	out := g[0]
	for _, s := range g[1:] {
		out += "," + s
	}
	return out
}

// contains is a tiny strings.Contains wrapper for readable test
// assertions.
func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && stringIndex(haystack, needle) >= 0
}

func stringIndex(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}

// Unused-import silencers (chi is referenced indirectly through
// newRequestWithChiParam in handler_test.go; explicit anchor here
// for clarity even though Go's unused-import check won't fire on
// indirectly-used imports).
var _ = chi.URLParam
