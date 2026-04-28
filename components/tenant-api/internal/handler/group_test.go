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
)

func setupGroupsFile(t *testing.T, configDir, content string) {
	t.Helper()
	if content != "" {
		if err := os.WriteFile(filepath.Join(configDir, "_groups.yaml"), []byte(content), 0644); err != nil {
			t.Fatalf("write _groups.yaml: %v", err)
		}
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
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)

	rbacMgr := newRBACManager(t, "")
	h := ListGroups(mgr, rbacMgr)
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
	configDir := setupConfigDir(t, nil)
	setupGroupsFile(t, configDir, testGroupsYAML)
	mgr := groups.NewManager(configDir)

	rbacMgr := newRBACManager(t, "")
	h := ListGroups(mgr, rbacMgr)
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
	configDir := setupConfigDir(t, nil)
	setupGroupsFile(t, configDir, testGroupsYAML)
	mgr := groups.NewManager(configDir)

	h := GetGroup(mgr)
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
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)

	h := GetGroup(mgr)
	req := newRequestWithChiParam("GET", "/api/v1/groups/nonexistent", "id", "nonexistent", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("GetGroup() status = %d, want %d", w.Code, http.StatusNotFound)
	}
}

func TestGetGroup_InvalidID(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)

	h := GetGroup(mgr)
	req := newRequestWithChiParam("GET", "/api/v1/groups/INVALID!", "id", "INVALID!", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("GetGroup() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

// --- PutGroup tests ---

func TestPutGroup_Create(t *testing.T) {
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
	h := PutGroup(mgr, writer, permissiveRBACManager(t))
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

	h := PutGroup(mgr, writer, permissiveRBACManager(t))
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
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)

	body := `{"members":["db-a"]}`
	req := newRequestWithChiParam("PUT", "/api/v1/groups/my-group", "id", "my-group",
		bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")

	w := httptest.NewRecorder()
	h := PutGroup(mgr, writer, newRBACManager(t, ""))
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("PutGroup() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

func TestPutGroup_InvalidJSON(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)

	req := newRequestWithChiParam("PUT", "/api/v1/groups/my-group", "id", "my-group",
		bytes.NewBufferString("not json"))
	req.Header.Set("Content-Type", "application/json")

	w := httptest.NewRecorder()
	h := PutGroup(mgr, writer, newRBACManager(t, ""))
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("PutGroup() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

// --- DeleteGroup tests ---

func TestDeleteGroup_Success(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	setupGroupsFile(t, configDir, testGroupsYAML)
	initGitRepo(t, configDir)
	mgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)

	req := newRequestWithChiParam("DELETE", "/api/v1/groups/staging-all", "id", "staging-all", nil)
	setRequestIdentity(req, "test@example.com")

	h := DeleteGroup(mgr, writer, permissiveRBACManager(t))
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
	configDir := setupConfigDir(t, nil)
	mgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)

	req := newRequestWithChiParam("DELETE", "/api/v1/groups/nonexistent", "id", "nonexistent", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")

	w := httptest.NewRecorder()
	h := DeleteGroup(mgr, writer, newRBACManager(t, ""))
	h(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("DeleteGroup() status = %d, want %d", w.Code, http.StatusNotFound)
	}
}

// --- GroupBatch tests ---

func TestGroupBatch_Success(t *testing.T) {
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
	h := GroupBatch(groupMgr, writer, configDir, rbacMgr, nil)
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
	configDir := setupConfigDir(t, nil)
	groupMgr := groups.NewManager(configDir)
	writer := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")

	body := `{"patch":{"_silent_mode":"warning"}}`
	req := newRequestWithChiParam("POST", "/api/v1/groups/nonexistent/batch",
		"id", "nonexistent", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")

	w := httptest.NewRecorder()
	h := GroupBatch(groupMgr, writer, configDir, rbacMgr, nil)
	h(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("GroupBatch() status = %d, want %d", w.Code, http.StatusNotFound)
	}
}

func TestGroupBatch_EmptyPatch(t *testing.T) {
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
	h := GroupBatch(groupMgr, writer, configDir, rbacMgr, nil)
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("GroupBatch() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

// --- ListTenants with metadata tests ---

func TestListTenants_WithMetadata(t *testing.T) {
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
	h := ListTenants(configDir, rbacMgr)
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
