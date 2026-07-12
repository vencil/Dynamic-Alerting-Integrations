package handler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/testutil"
)

func setupGroupsFile(t *testing.T, configDir, content string) {
	t.Helper()
	if content != "" {
		testutil.WriteYAML(t, configDir, "_groups.yaml", content)
	}
}

// setRequestIdentity wraps a request through the RBAC middleware to set context identity.
// This simulates what oauth2-proxy + RBAC middleware would do in production.
func setRequestIdentity(req *http.Request, email string) *http.Request {
	req.Header.Set("X-Forwarded-Email", email)
	req.Header.Set("X-Forwarded-Groups", "platform-admins")
	return req
}

// executeWithRBAC runs a handler through the RBAC middleware to set identity context.
func executeWithRBAC(t *testing.T, handler http.HandlerFunc, req *http.Request) *httptest.ResponseRecorder {
	t.Helper()
	mgr := newRBACManager(t, "")
	wrapped := wrapWithRBACMiddleware(handler, mgr, rbac.PermRead, nil)
	w := httptest.NewRecorder()
	wrapped.ServeHTTP(w, req)
	return w
}

// permissiveRBACManager returns an RBAC manager that grants admin
// (read+write) on all tenants to the test caller's IDP group
// `platform-admins` (set by setRequestIdentity). Used by tests that
// invoke handlers requiring v2.8.0 B-6 PR-2 tenant-scoped write
// authz on member tenants — open-read mode (empty rbac.yaml) only
// grants reads, so those tests need an explicit permissive config.
func permissiveRBACManager(t *testing.T) *rbac.Manager {
	t.Helper()
	return newRBACManager(t, `groups:
  - name: platform-admins
    tenants: ["*"]
    permissions: [admin]
`)
}

// initGitRepo initializes a git repo in the given directory with an initial commit.
func initGitRepo(t *testing.T, dir string) {
	t.Helper()
	cmds := [][]string{
		{"git", "init"},
		{"git", "config", "user.email", "test@test.com"},
		{"git", "config", "user.name", "Test"},
		{"git", "add", "."},
		{"git", "commit", "--allow-empty", "-m", "init"},
	}
	for _, args := range cmds {
		cmd := exec.Command(args[0], args[1:]...)
		cmd.Dir = dir
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git command %v failed: %v\n%s", args, err, out)
		}
	}
}

const testGroupsYAML = `groups:
  production-dba:
    label: Production DBA
    description: All production DB tenants
    members:
      - db-a
      - db-b
  staging-all:
    label: All Staging
    members:
      - staging-pg-01
`

// --- ListGroups tests ---

func TestListGroups_Empty(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)

	rbacMgr := newRBACManager(t, "")
	h := ListGroups(&Deps{Groups: mgr, RBAC: rbacMgr})
	req := httptest.NewRequest("GET", "/api/v1/groups", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("ListGroups() status = %d, want %d", w.Code, http.StatusOK)
	}

	var resp []GroupResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(resp) != 0 {
		t.Errorf("expected 0 groups, got %d", len(resp))
	}
}

func TestListGroups_WithData(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	setupGroupsFile(t, configDir, testGroupsYAML)
	mgr := groups.NewManager(configDir)

	rbacMgr := newRBACManager(t, "")
	h := ListGroups(&Deps{Groups: mgr, RBAC: rbacMgr})
	req := httptest.NewRequest("GET", "/api/v1/groups", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("ListGroups() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}

	var resp []GroupResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(resp) != 2 {
		t.Fatalf("expected 2 groups, got %d", len(resp))
	}

	// Groups are sorted by ID
	if resp[0].ID != "production-dba" {
		t.Errorf("first group ID = %q, want %q", resp[0].ID, "production-dba")
	}
	if resp[0].Label != "Production DBA" {
		t.Errorf("first group label = %q, want %q", resp[0].Label, "Production DBA")
	}
	if len(resp[0].Members) != 2 {
		t.Errorf("first group members = %d, want 2", len(resp[0].Members))
	}
	if resp[1].ID != "staging-all" {
		t.Errorf("second group ID = %q, want %q", resp[1].ID, "staging-all")
	}
}

// --- GetGroup tests ---

func TestGetGroup_Success(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	setupGroupsFile(t, configDir, testGroupsYAML)
	mgr := groups.NewManager(configDir)

	// Open-mode RBAC (empty _rbac.yaml): filterAccessibleMembers is the
	// identity transform, so this pins the byte-identical open-mode path.
	h := GetGroup(&Deps{Groups: mgr, RBAC: newRBACManager(t, "")})
	req := newRequestWithChiParam("GET", "/api/v1/groups/production-dba", "id", "production-dba", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("GetGroup() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}

	var resp GroupResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.ID != "production-dba" {
		t.Errorf("GroupResponse.ID = %q, want %q", resp.ID, "production-dba")
	}
	if resp.Label != "Production DBA" {
		t.Errorf("GroupResponse.Label = %q, want %q", resp.Label, "Production DBA")
	}
	if resp.Description != "All production DB tenants" {
		t.Errorf("GroupResponse.Description = %q", resp.Description)
	}
	if len(resp.Members) != 2 {
		t.Errorf("GroupResponse.Members = %d, want 2", len(resp.Members))
	}
}

func TestGetGroup_NotFound(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)

	h := GetGroup(&Deps{Groups: mgr})
	req := newRequestWithChiParam("GET", "/api/v1/groups/nonexistent", "id", "nonexistent", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("GetGroup() status = %d, want %d", w.Code, http.StatusNotFound)
	}
}

func TestGetGroup_InvalidID(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)

	h := GetGroup(&Deps{Groups: mgr})
	req := newRequestWithChiParam("GET", "/api/v1/groups/INVALID!", "id", "INVALID!", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("GetGroup() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

// TestGetGroup_OrgScopeFiltersMembers grounds the #962 LD-6 P4c follow-up: GetGroup mirrors
// ListGroups' RBAC member filtering. Under org-scope enforce, an org-scoped
// reader who clears the route's PermRead("*") gate still sees only the members
// in its own org, and a group with no in-org member is hidden (404) — closing
// the read-by-id twin of the group-member enumeration oracle P4c closed for the
// list view. Fixture reused from org_read_enforce_test.go.
func TestGetGroup_OrgScopeFiltersMembers(t *testing.T) {
	t.Parallel()
	const groupsYAML = `groups:
  mixed-org:
    label: Mixed Org
    members:
      - ` + orgReadTenantIn + `
      - ` + orgReadTenantOut + `
  outsiders-only:
    label: Outsiders Only
    members:
      - ` + orgReadTenantOut + `
  empty-grp:
    label: Empty Group
    members: []
`
	configDir := setupConfigDir(t, nil)
	setupGroupsFile(t, configDir, groupsYAML)
	groupMgr := groups.NewManager(configDir)

	get := func(t *testing.T, groupID, callerOrg string) *httptest.ResponseRecorder {
		t.Helper()
		mgr, torg, _ := newOrgReadManager(t, orgReadRBACYAML, true) // enforce
		d := &Deps{Groups: groupMgr, RBAC: mgr, TenantOrg: torg}
		// GetGroup's route uses a nil tenantIDFn (the group id is not a tenant
		// id), so the middleware gate is the org-blind Allowed(p, "*", read) —
		// the org-scoped "*" reader clears it, and the per-member org filtering
		// happens inside the handler.
		wrapped := wrapWithRBACMiddleware(GetGroup(d), mgr, rbac.PermRead, nil)
		req := newRequestWithChiParam("GET", "/api/v1/groups/"+groupID, "id", groupID, nil)
		req = orgReadIdentity(req, callerOrg)
		w := httptest.NewRecorder()
		wrapped.ServeHTTP(w, req)
		return w
	}

	// In-org caller: sees the group, but the cross-org member is filtered out.
	w := get(t, "mixed-org", orgReadMemberOrg)
	if w.Code != http.StatusOK {
		t.Fatalf("in-org GetGroup status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	var resp GroupResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(resp.Members) != 1 || resp.Members[0] != orgReadTenantIn {
		t.Errorf("GetGroup members = %v, want [%s] (cross-org member must be filtered under enforce)", resp.Members, orgReadTenantIn)
	}

	// A group whose only member is cross-org is hidden — an identical 404 to a
	// missing group, so existence leaks nothing (mirrors ListGroups' skip).
	if w := get(t, "outsiders-only", orgReadMemberOrg); w.Code != http.StatusNotFound {
		t.Errorf("cross-org-only GetGroup status = %d, want 404 (group with no accessible member must be hidden)", w.Code)
	}

	// Edge: a group with NO members has no accessible member either, so it is
	// hidden (404) under configured RBAC — matching ListGroups' skip. This is a
	// behavior change vs pre-fix (200 with empty members) that applies whenever
	// RBAC groups are configured (not only under org-scope); pinned here so the
	// parity with ListGroups is intentional, not incidental.
	if w := get(t, "empty-grp", orgReadMemberOrg); w.Code != http.StatusNotFound {
		t.Errorf("empty-member GetGroup status = %d, want 404 (group with no members is hidden, matching ListGroups)", w.Code)
	}
}

// TestGetGroup_NoOrgRuleByteIdentical is the byte-identical control: with a
// plain (non-org-scoped) read grant, enforce mode changes nothing — a "*"
// reader sees every member, exactly as before this fix. Guards against the
// filter over-restricting a deployment that has RBAC groups but no org-scope.
func TestGetGroup_NoOrgRuleByteIdentical(t *testing.T) {
	t.Parallel()
	const groupsYAML = `groups:
  mixed-org:
    label: Mixed Org
    members:
      - ` + orgReadTenantIn + `
      - ` + orgReadTenantOut + `
`
	configDir := setupConfigDir(t, nil)
	setupGroupsFile(t, configDir, groupsYAML)
	groupMgr := groups.NewManager(configDir)

	mgr, torg, _ := newOrgReadManager(t, orgReadPlainRBACYAML, true) // enforce, but no org-scope rule
	d := &Deps{Groups: groupMgr, RBAC: mgr, TenantOrg: torg}
	wrapped := wrapWithRBACMiddleware(GetGroup(d), mgr, rbac.PermRead, nil)
	req := newRequestWithChiParam("GET", "/api/v1/groups/mixed-org", "id", "mixed-org", nil)
	req = orgReadIdentity(req, orgReadMemberOrg)
	w := httptest.NewRecorder()
	wrapped.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	var resp GroupResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(resp.Members) != 2 {
		t.Errorf("no-org-rule GetGroup members = %d, want 2 (no org-scope rule → org axis (true,true) → no filtering)", len(resp.Members))
	}
}

// --- PutGroup tests ---

func TestPutGroup_Create(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	mgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)

	body := `{"label":"My Group","description":"Test group","members":["db-a","db-b"]}`
	req := newRequestWithChiParam("PUT", "/api/v1/groups/my-group", "id", "my-group",
		bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	setRequestIdentity(req, "test@example.com")

	// permissiveRBACManager: v2.8.0 B-6 PR-2 hardening requires
	// PermWrite on every member tenant. Open-mode RBAC only grants
	// PermRead, so the test caller needs an explicit admin grant.
	h := PutGroup(&Deps{Groups: mgr, Writer: writer, RBAC: permissiveRBACManager(t)})
	w := executeWithRBAC(t, h, req)

	if w.Code != http.StatusOK {
		t.Fatalf("PutGroup() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}

	// Verify the group was created
	g, ok := mgr.GetGroup("my-group")
	if !ok {
		t.Fatal("expected group to exist after PutGroup")
	}
	if g.Label != "My Group" {
		t.Errorf("group label = %q, want %q", g.Label, "My Group")
	}
	if len(g.Members) != 2 {
		t.Errorf("group members = %d, want 2", len(g.Members))
	}

	// Verify _groups.yaml was written
	data, err := os.ReadFile(filepath.Join(configDir, "_groups.yaml"))
	if err != nil {
		t.Fatalf("read _groups.yaml: %v", err)
	}
	if len(data) == 0 {
		t.Error("_groups.yaml should not be empty")
	}
}

func TestPutGroup_Update(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	setupGroupsFile(t, configDir, testGroupsYAML)
	initGitRepo(t, configDir)
	mgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)

	body := `{"label":"Updated DBA","members":["db-a","db-b","db-c"]}`
	req := newRequestWithChiParam("PUT", "/api/v1/groups/production-dba", "id", "production-dba",
		bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	setRequestIdentity(req, "test@example.com")

	h := PutGroup(&Deps{Groups: mgr, Writer: writer, RBAC: permissiveRBACManager(t)})
	w := executeWithRBAC(t, h, req)

	if w.Code != http.StatusOK {
		t.Fatalf("PutGroup() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}

	g, _ := mgr.GetGroup("production-dba")
	if g.Label != "Updated DBA" {
		t.Errorf("group label = %q, want %q", g.Label, "Updated DBA")
	}
	if len(g.Members) != 3 {
		t.Errorf("group members = %d, want 3", len(g.Members))
	}
}

func TestPutGroup_MissingLabel(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)

	body := `{"members":["db-a"]}`
	req := newRequestWithChiParam("PUT", "/api/v1/groups/my-group", "id", "my-group",
		bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")

	w := httptest.NewRecorder()
	h := PutGroup(&Deps{Groups: mgr, Writer: writer, RBAC: newRBACManager(t, "")})
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("PutGroup() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

func TestPutGroup_InvalidJSON(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)

	req := newRequestWithChiParam("PUT", "/api/v1/groups/my-group", "id", "my-group",
		bytes.NewBufferString("not json"))
	req.Header.Set("Content-Type", "application/json")

	w := httptest.NewRecorder()
	h := PutGroup(&Deps{Groups: mgr, Writer: writer, RBAC: newRBACManager(t, "")})
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("PutGroup() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

// --- DeleteGroup tests ---

func TestDeleteGroup_Success(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	setupGroupsFile(t, configDir, testGroupsYAML)
	initGitRepo(t, configDir)
	mgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)

	req := newRequestWithChiParam("DELETE", "/api/v1/groups/staging-all", "id", "staging-all", nil)
	setRequestIdentity(req, "test@example.com")

	h := DeleteGroup(&Deps{Groups: mgr, Writer: writer, RBAC: permissiveRBACManager(t)})
	w := executeWithRBAC(t, h, req)

	if w.Code != http.StatusOK {
		t.Fatalf("DeleteGroup() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}

	_, ok := mgr.GetGroup("staging-all")
	if ok {
		t.Error("expected group to be deleted")
	}

	// production-dba should still exist
	_, ok = mgr.GetGroup("production-dba")
	if !ok {
		t.Error("production-dba should still exist after deleting staging-all")
	}
}

func TestDeleteGroup_NotFound(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)

	req := newRequestWithChiParam("DELETE", "/api/v1/groups/nonexistent", "id", "nonexistent", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")

	w := httptest.NewRecorder()
	h := DeleteGroup(&Deps{Groups: mgr, Writer: writer, RBAC: newRBACManager(t, "")})
	h(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("DeleteGroup() status = %d, want %d", w.Code, http.StatusNotFound)
	}
}

// --- GroupBatch tests ---

func TestGroupBatch_Success(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: \"70\"\n",
		"db-b.yaml": "tenants:\n  db-b:\n    mysql_connections: \"80\"\n",
	})
	setupGroupsFile(t, configDir, testGroupsYAML)

	groupMgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")

	body := `{"patch":{"_silent_mode":"warning"}}`
	req := newRequestWithChiParam("POST", "/api/v1/groups/production-dba/batch",
		"id", "production-dba", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "test@example.com")

	w := httptest.NewRecorder()
	h := GroupBatch(&Deps{Groups: groupMgr, Writer: writer, ConfigDir: configDir, RBAC: rbacMgr})
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("GroupBatch() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}

	var resp GroupBatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Status != "completed" {
		t.Errorf("status = %q, want %q", resp.Status, "completed")
	}
	if resp.GroupID != "production-dba" {
		t.Errorf("group_id = %q, want %q", resp.GroupID, "production-dba")
	}
	if len(resp.Results) != 2 {
		t.Errorf("results = %d, want 2", len(resp.Results))
	}
}

func TestGroupBatch_GroupNotFound(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	groupMgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")

	body := `{"patch":{"_silent_mode":"warning"}}`
	req := newRequestWithChiParam("POST", "/api/v1/groups/nonexistent/batch",
		"id", "nonexistent", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")

	w := httptest.NewRecorder()
	h := GroupBatch(&Deps{Groups: groupMgr, Writer: writer, ConfigDir: configDir, RBAC: rbacMgr})
	h(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("GroupBatch() status = %d, want %d", w.Code, http.StatusNotFound)
	}
}

func TestGroupBatch_EmptyPatch(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	setupGroupsFile(t, configDir, testGroupsYAML)
	groupMgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")

	body := `{"patch":{}}`
	req := newRequestWithChiParam("POST", "/api/v1/groups/production-dba/batch",
		"id", "production-dba", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")

	w := httptest.NewRecorder()
	h := GroupBatch(&Deps{Groups: groupMgr, Writer: writer, ConfigDir: configDir, RBAC: rbacMgr})
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("GroupBatch() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

// --- ListTenants with metadata tests ---

func TestListTenants_WithMetadata(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": `tenants:
  db-a:
    mysql_connections: "70"
    _metadata:
      environment: production
      region: ap-northeast-1
      tier: tier-1
      domain: finance
      db_type: mariadb
      owner: team-dba
      tags:
        - critical-path
      groups:
        - production-dba
`,
	})

	rbacMgr := newRBACManager(t, "")
	h := ListTenants(&Deps{ConfigDir: configDir, RBAC: rbacMgr})
	req := httptest.NewRequest("GET", "/api/v1/tenants", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("ListTenants() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}

	var resp []TenantSummary
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(resp) != 1 {
		t.Fatalf("expected 1 tenant, got %d", len(resp))
	}

	ts := resp[0]
	if ts.Environment != "production" {
		t.Errorf("Environment = %q, want %q", ts.Environment, "production")
	}
	if ts.Region != "ap-northeast-1" {
		t.Errorf("Region = %q, want %q", ts.Region, "ap-northeast-1")
	}
	if ts.Tier != "tier-1" {
		t.Errorf("Tier = %q, want %q", ts.Tier, "tier-1")
	}
	if ts.Domain != "finance" {
		t.Errorf("Domain = %q, want %q", ts.Domain, "finance")
	}
	if ts.DBType != "mariadb" {
		t.Errorf("DBType = %q, want %q", ts.DBType, "mariadb")
	}
	if ts.Owner != "team-dba" {
		t.Errorf("Owner = %q, want %q", ts.Owner, "team-dba")
	}
	if len(ts.Tags) != 1 || ts.Tags[0] != "critical-path" {
		t.Errorf("Tags = %v, want [critical-path]", ts.Tags)
	}
	if len(ts.Groups) != 1 || ts.Groups[0] != "production-dba" {
		t.Errorf("Groups = %v, want [production-dba]", ts.Groups)
	}
}
