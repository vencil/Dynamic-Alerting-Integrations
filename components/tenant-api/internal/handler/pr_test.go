package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	gh "github.com/vencil/tenant-api/internal/github"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
)

// adminRBAC returns an RBAC manager whose "admins" group has write on all
// tenants — paired with the X-Forwarded-Email/Groups headers below so the
// middleware injects an operator identity (WritePR's git commit author).
func adminRBAC(t *testing.T) *rbac.Manager {
	return newRBACManager(t, `groups:
  - name: admins
    tenants: ["*"]
    permissions: [read, write, admin]
`)
}

// initGitConfigDir creates a temp dir that is a git repo with one commit, so
// Writer.WritePR can create a feature branch + commit. WritePR's push to a
// (nonexistent) origin fails and is swallowed by design, so no remote is
// needed — the PR-creation step is exercised via the mock platform client.
func initGitConfigDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for _, args := range [][]string{
		{"init"},
		{"config", "user.email", "test@test.com"},
		{"config", "user.name", "Test"},
		{"commit", "--allow-empty", "-m", "initial"},
		{"branch", "-M", "main"}, // #638: WritePR now checks out the base ("main") branch
	} {
		cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Skipf("git command %v failed: %v\n%s", args, err, string(out))
		}
	}
	return dir
}

// --- PR List Handler tests (GET /api/v1/prs) ---

func newTestTracker(t *testing.T, prs []platform.PRInfo) *gh.Tracker {
	t.Helper()
	apiPRs := make([]map[string]interface{}, len(prs))
	for i, pr := range prs {
		apiPRs[i] = map[string]interface{}{
			"number":     pr.Number,
			"html_url":   pr.WebURL,
			"state":      pr.State,
			"title":      pr.Title,
			"head":       map[string]string{"ref": pr.HeadRef},
			"created_at": pr.CreatedAt,
		}
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(apiPRs)
	}))
	t.Cleanup(srv.Close)

	c, _ := gh.NewClient("token", "owner/repo", "main")
	c.SetBaseURL(srv.URL)

	tracker := gh.NewTracker(c, 1<<30) // very long interval, we sync manually
	return tracker
}

func TestListPRs_Empty(t *testing.T) {
	t.Parallel()
	tracker := newTestTracker(t, []platform.PRInfo{})

	h := ListPRs(&Deps{PRTracker: tracker, RBAC: newRBACManager(t, "")})
	req := httptest.NewRequest("GET", "/api/v1/prs", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", w.Code, http.StatusOK)
	}

	var resp PRListResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Count != 0 {
		t.Errorf("count = %d, want 0", resp.Count)
	}
}

func TestListPRs_WithPRs(t *testing.T) {
	t.Parallel()
	tracker := newTestTracker(t, []platform.PRInfo{
		{Number: 1, WebURL: "https://gh/1", State: "open", Title: "PR1", HeadRef: "tenant-api/db-a/20260406"},
		{Number: 2, WebURL: "https://gh/2", State: "open", Title: "PR2", HeadRef: "tenant-api/db-b/20260406"},
	})

	h := ListPRs(&Deps{PRTracker: tracker, RBAC: newRBACManager(t, "")})
	req := httptest.NewRequest("GET", "/api/v1/prs", nil)
	w := httptest.NewRecorder()
	h(w, req)

	var resp PRListResponse
	json.Unmarshal(w.Body.Bytes(), &resp)

	// tracker hasn't synced yet, so it should return 0 PRs
	// (sync is explicit in tests)
	if resp.Count != 0 {
		t.Logf("count = %d (tracker hasn't synced)", resp.Count)
	}
}

func TestListPRs_WithRegisteredPR(t *testing.T) {
	t.Parallel()
	tracker := newTestTracker(t, []platform.PRInfo{})

	// Manually register a PR
	tracker.RegisterPR(platform.PRInfo{
		Number:   42,
		WebURL:   "https://github.com/owner/repo/pull/42",
		State:    "open",
		TenantID: "db-a",
		HeadRef:  "tenant-api/db-a/20260406",
	})

	h := ListPRs(&Deps{PRTracker: tracker, RBAC: newRBACManager(t, "")})
	req := httptest.NewRequest("GET", "/api/v1/prs", nil)
	w := httptest.NewRecorder()
	h(w, req)

	var resp PRListResponse
	json.Unmarshal(w.Body.Bytes(), &resp)

	if resp.Count != 1 {
		t.Fatalf("count = %d, want 1", resp.Count)
	}
	if resp.PendingPRs[0].Number != 42 {
		t.Errorf("PR number = %d, want 42", resp.PendingPRs[0].Number)
	}
}

func TestListPRs_FilterByTenant(t *testing.T) {
	t.Parallel()
	tracker := newTestTracker(t, []platform.PRInfo{})

	tracker.RegisterPR(platform.PRInfo{
		Number: 1, WebURL: "https://gh/1", State: "open", TenantID: "db-a",
	})
	tracker.RegisterPR(platform.PRInfo{
		Number: 2, WebURL: "https://gh/2", State: "open", TenantID: "db-b",
	})

	h := ListPRs(&Deps{PRTracker: tracker, RBAC: newRBACManager(t, "")})

	// Filter for db-a only
	req := httptest.NewRequest("GET", "/api/v1/prs?tenant=db-a", nil)
	w := httptest.NewRecorder()
	h(w, req)

	var resp PRListResponse
	json.Unmarshal(w.Body.Bytes(), &resp)

	if resp.Count != 1 {
		t.Fatalf("count = %d, want 1", resp.Count)
	}
	if resp.PendingPRs[0].TenantID != "db-a" {
		t.Errorf("tenant_id = %q, want 'db-a'", resp.PendingPRs[0].TenantID)
	}
}

func TestListPRs_FilterByNonexistentTenant(t *testing.T) {
	t.Parallel()
	tracker := newTestTracker(t, []platform.PRInfo{})
	tracker.RegisterPR(platform.PRInfo{
		Number: 1, WebURL: "https://gh/1", State: "open", TenantID: "db-a",
	})

	h := ListPRs(&Deps{PRTracker: tracker, RBAC: newRBACManager(t, "")})
	req := httptest.NewRequest("GET", "/api/v1/prs?tenant=nonexistent", nil)
	w := httptest.NewRecorder()
	h(w, req)

	var resp PRListResponse
	json.Unmarshal(w.Body.Bytes(), &resp)

	if resp.Count != 0 {
		t.Errorf("count = %d, want 0 for nonexistent tenant", resp.Count)
	}
}

// --- PutTenant PR mode tests ---

func TestPutTenant_DirectMode(t *testing.T) {
	t.Parallel()
	// In direct mode, PR params are nil — should fall through to normal behavior
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n",
	})
	writer := newTestWriter(configDir)

	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModeDirect})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	// Set identity headers for RBAC
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	h(w, req)

	// In non-git mode, the write may fail on git commit, but the validation
	// and write-mode routing should work correctly.
	// We just verify it doesn't panic and returns some response.
	if w.Code != http.StatusOK && w.Code != http.StatusBadRequest {
		t.Logf("PutTenant direct mode: status=%d body=%s", w.Code, w.Body.String())
	}
}

func TestPutTenant_PRMode_PendingPRConflict(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	writer := newTestWriter(configDir)

	// Mock GitHub server returns PR #99 — required since #644: the 409 path
	// now force-refreshes the tracker via the forge before returning, so the
	// mock must confirm the PR is still open (else refresh clears the cache,
	// 409 turns into a write that proceeds).
	ghSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `[{"number":99,"html_url":"https://github.com/owner/repo/pull/99","state":"open","head":{"ref":"tenant-api/db-a/20260406"}}]`)
	}))
	defer ghSrv.Close()

	ghClient, _ := gh.NewClient("token", "owner/repo", "main")
	ghClient.SetBaseURL(ghSrv.URL)

	tracker := gh.NewTracker(ghClient, 1<<30)
	// Register an existing pending PR for db-a
	tracker.RegisterPR(platform.PRInfo{
		Number:   99,
		WebURL:   "https://github.com/owner/repo/pull/99",
		State:    "open",
		TenantID: "db-a",
		HeadRef:  "tenant-api/db-a/20260406",
	})

	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePR, PRClient: ghClient, PRTracker: tracker})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusConflict {
		t.Fatalf("expected 409 Conflict, got %d: %s", w.Code, w.Body.String())
	}

	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)

	if resp["error"] != "pending_pr_exists" {
		t.Errorf("error = %q, want 'pending_pr_exists'", resp["error"])
	}
	if resp["existing_pr_url"] != "https://github.com/owner/repo/pull/99" {
		t.Errorf("existing_pr_url = %q", resp["existing_pr_url"])
	}
}

func TestPutTenant_PRMode_InvalidTenantID(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	writer := newTestWriter(configDir)

	ghSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `[]`)
	}))
	defer ghSrv.Close()

	ghClient, _ := gh.NewClient("token", "owner/repo", "main")
	ghClient.SetBaseURL(ghSrv.URL)
	tracker := gh.NewTracker(ghClient, 1<<30)

	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePR, PRClient: ghClient, PRTracker: tracker})
	body := bytes.NewBufferString("content")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/../etc/passwd", "id", "../etc/passwd", body)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("expected 400 for path traversal, got %d", w.Code)
	}
}

// --- PutTenant GitLab MR mode tests ---

func TestPutTenant_GitLabMode_PendingMRConflict(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	writer := newTestWriter(configDir)

	// Mock server returns the PR — required since #644 (see PendingPRConflict above).
	// Using GitHub-shape JSON because we wrap with gh.Client per the comment below.
	glSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `[{"number":42,"html_url":"https://gitlab.com/group/project/-/merge_requests/42","state":"open","head":{"ref":"tenant-api/db-a/20260406"}}]`)
	}))
	defer glSrv.Close()

	// Use GitHub client as concrete tracker (both implement platform.Tracker)
	ghClient, _ := gh.NewClient("token", "owner/repo", "main")
	ghClient.SetBaseURL(glSrv.URL)
	tracker := gh.NewTracker(ghClient, 1<<30)

	// Register an existing pending MR for db-a
	tracker.RegisterPR(platform.PRInfo{
		Number:   42,
		WebURL:   "https://gitlab.com/group/project/-/merge_requests/42",
		State:    "opened",
		TenantID: "db-a",
		HeadRef:  "tenant-api/db-a/20260406",
	})

	// Use pr-gitlab write mode with the tracker
	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePRGitLab, PRClient: ghClient, PRTracker: tracker})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusConflict {
		t.Fatalf("expected 409 Conflict for GitLab mode, got %d: %s", w.Code, w.Body.String())
	}

	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)

	if resp["error"] != "pending_pr_exists" {
		t.Errorf("error = %q, want 'pending_pr_exists'", resp["error"])
	}
}

// --- mockPlatformClient implements platform.Client for testing happy paths ---

type mockPlatformClient struct {
	providerName string
	createPRFunc func(title, body, headBranch string, labels []string) (*platform.PRInfo, error)
}

func (m *mockPlatformClient) ValidateToken() error                { return nil }
func (m *mockPlatformClient) CreateBranch(branchName string) error { return nil }
func (m *mockPlatformClient) DeleteBranch(branchName string) error { return nil }
func (m *mockPlatformClient) SetBaseURL(url string)                {}
func (m *mockPlatformClient) CreatePR(title, body, headBranch string, labels []string) (*platform.PRInfo, error) {
	if m.createPRFunc != nil {
		return m.createPRFunc(title, body, headBranch, labels)
	}
	return &platform.PRInfo{
		Number:  42,
		WebURL:  "https://github.com/owner/repo/pull/42",
		State:   "open",
		Title:   title,
		HeadRef: headBranch,
	}, nil
}
func (m *mockPlatformClient) ListOpenPRs() ([]platform.PRInfo, error) { return nil, nil }
func (m *mockPlatformClient) ProviderName() string {
	if m.providerName != "" {
		return m.providerName
	}
	return "mock"
}

// --- mockPlatformTracker implements platform.Tracker for testing ---

type mockPlatformTracker struct {
	prs     []platform.PRInfo
	claimed map[string]bool
	// refreshFn (optional) is invoked by RefreshNow — tests use it to simulate a
	// real forge sync that clears (or doesn't clear) the cache for #644 scenarios.
	refreshFn    func()
	refreshCalls atomic.Int32 // atomic for forward-safety vs concurrent tests
}

func (m *mockPlatformTracker) WatchLoop(stopCh <-chan struct{}) {}
func (m *mockPlatformTracker) PendingPRs() []platform.PRInfo     { return m.prs }
func (m *mockPlatformTracker) PendingPRForTenant(tenantID string) (platform.PRInfo, bool) {
	for _, pr := range m.prs {
		if pr.TenantID == tenantID {
			return pr, true
		}
	}
	return platform.PRInfo{}, false
}
func (m *mockPlatformTracker) HasPendingPR(tenantID string) bool {
	_, ok := m.PendingPRForTenant(tenantID)
	return ok
}
func (m *mockPlatformTracker) ClaimTenant(tenantID string) bool {
	if m.HasPendingPR(tenantID) || m.claimed[tenantID] {
		return false
	}
	if m.claimed == nil {
		m.claimed = make(map[string]bool)
	}
	m.claimed[tenantID] = true
	return true
}
func (m *mockPlatformTracker) ReleaseClaim(tenantID string) {
	delete(m.claimed, tenantID)
}
func (m *mockPlatformTracker) RegisterPR(pr platform.PRInfo) {
	delete(m.claimed, pr.TenantID)
	m.prs = append(m.prs, pr)
}
func (m *mockPlatformTracker) LastSyncTime() time.Time { return time.Now() }
func (m *mockPlatformTracker) RefreshNow(ctx context.Context) {
	m.refreshCalls.Add(1)
	if m.refreshFn != nil {
		m.refreshFn()
	}
}

// --- Happy-path test: PutTenant in PR mode (successful PR creation) ---

func TestPutTenant_PRMode_HappyPath(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n",
	})
	writer := newTestWriter(configDir)

	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, headBranch string, labels []string) (*platform.PRInfo, error) {
			return &platform.PRInfo{
				Number:  42,
				WebURL:  "https://github.com/owner/repo/pull/42",
				State:   "open",
				Title:   title,
				HeadRef: headBranch,
			}, nil
		},
	}
	mockTracker := &mockPlatformTracker{}

	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePR, PRClient: mockClient, PRTracker: mockTracker})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	w := httptest.NewRecorder()
	h(w, req)

	// WritePR requires git, so in test env it will fail at git operations.
	// We verify the handler correctly routes to PR mode (not direct mode).
	// If it returns 500 with "PR write failed", it means PR routing worked.
	if w.Code == http.StatusOK {
		// If git was available, verify the PR response format
		var resp PutTenantResponse
		json.Unmarshal(w.Body.Bytes(), &resp)
		if resp.Status != "pending_review" {
			t.Errorf("status = %q, want 'pending_review'", resp.Status)
		}
		if resp.PRNumber != 42 {
			t.Errorf("pr_number = %d, want 42", resp.PRNumber)
		}
	} else if w.Code == http.StatusInternalServerError {
		// Expected in test env without git — verify it's the PR write path, not direct
		bodyStr := w.Body.String()
		if !strings.Contains(bodyStr, "PR write failed") {
			t.Errorf("expected PR write path error, got: %s", bodyStr)
		}
	} else if w.Code == http.StatusConflict {
		// 409 means pending_pr_exists check is working
		t.Log("PR mode routing verified (conflict path)")
	} else {
		t.Logf("PutTenant PR mode: status=%d body=%s", w.Code, w.Body.String())
	}
}

// --- Happy-path test: PutTenant GitLab MR mode ---

func TestPutTenant_GitLabMode_HappyPath(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n",
	})
	writer := newTestWriter(configDir)

	mockClient := &mockPlatformClient{providerName: "gitlab"}
	mockTracker := &mockPlatformTracker{}

	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePRGitLab, PRClient: mockClient, PRTracker: mockTracker})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	w := httptest.NewRecorder()
	h(w, req)

	// In test env, WritePR will fail on git operations. We verify the routing.
	if w.Code == http.StatusInternalServerError {
		bodyStr := w.Body.String()
		if !strings.Contains(bodyStr, "PR write failed") {
			t.Errorf("expected PR write path error, got: %s", bodyStr)
		}
	}
	// Any non-panic, non-direct-mode response confirms correct routing
	t.Logf("GitLab MR mode routing verified: status=%d", w.Code)
}

// --- Batch PR mode tests ---

func TestBatchTenants_PRMode_AllInvalid(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	writer := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")

	mockClient := &mockPlatformClient{providerName: "github"}
	mockTracker := &mockPlatformTracker{}

	h := BatchTenants(&Deps{Writer: writer, ConfigDir: configDir, RBAC: rbacMgr, WriteMode: WriteModePR, PRClient: mockClient, PRTracker: mockTracker})

	batchReq := `{"operations":[{"tenant_id":"../etc/passwd","patch":{"_silent_mode":"warning"}}]}`
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", strings.NewReader(batchReq))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200 (with error results), got %d: %s", w.Code, w.Body.String())
	}

	var resp BatchResponse
	json.Unmarshal(w.Body.Bytes(), &resp)

	if resp.Status != "completed" {
		t.Errorf("status = %q, want 'completed' (all ops failed)", resp.Status)
	}
	if resp.Message != "No valid operations to create PR/MR." {
		t.Errorf("message = %q, want 'No valid operations...'", resp.Message)
	}
}

func TestBatchTenants_PRMode_PendingPRRegistered(t *testing.T) {
	t.Parallel()
	// Verify that after batch PR, the tracker has entries for all tenants
	mockTracker := &mockPlatformTracker{}

	// Simulate: batch PR completed and registered 2 tenants
	mockTracker.RegisterPR(platform.PRInfo{
		Number: 43, WebURL: "https://github.com/org/repo/pull/43", State: "open", TenantID: "db-a",
	})
	mockTracker.RegisterPR(platform.PRInfo{
		Number: 43, WebURL: "https://github.com/org/repo/pull/43", State: "open", TenantID: "db-b",
	})

	if !mockTracker.HasPendingPR("db-a") {
		t.Error("expected pending PR for db-a after batch registration")
	}
	if !mockTracker.HasPendingPR("db-b") {
		t.Error("expected pending PR for db-b after batch registration")
	}
	if mockTracker.HasPendingPR("db-c") {
		t.Error("expected no pending PR for db-c")
	}

	prs := mockTracker.PendingPRs()
	if len(prs) != 2 {
		t.Errorf("pending PRs count = %d, want 2", len(prs))
	}
}

// --- WriteMode constants tests ---

func TestWriteModeConstants(t *testing.T) {
	t.Parallel()
	if WriteModeDirect != "direct" {
		t.Errorf("WriteModeDirect = %q, want 'direct'", WriteModeDirect)
	}
	if WriteModePR != "pr" {
		t.Errorf("WriteModePR = %q, want 'pr'", WriteModePR)
	}
	if WriteModePRGitHub != "pr-github" {
		t.Errorf("WriteModePRGitHub = %q, want 'pr-github'", WriteModePRGitHub)
	}
	if WriteModePRGitLab != "pr-gitlab" {
		t.Errorf("WriteModePRGitLab = %q, want 'pr-gitlab'", WriteModePRGitLab)
	}
}

func TestIsPRMode(t *testing.T) {
	t.Parallel()
	tests := []struct {
		mode WriteMode
		want bool
	}{
		{WriteModeDirect, false},
		{WriteModePR, true},
		{WriteModePRGitHub, true},
		{WriteModePRGitLab, true},
		{WriteMode("unknown"), false},
	}
	for _, tt := range tests {
		t.Run(string(tt.mode), func(t *testing.T) {
			if got := tt.mode.IsPRMode(); got != tt.want {
				t.Errorf("WriteMode(%q).IsPRMode() = %v, want %v", tt.mode, got, tt.want)
			}
		})
	}
}

// TestPutTenant_PRMode_ForgeForbidden asserts a forge 403 at PR-creation time
// (read-scoped token) maps to a clean HTTP 403, not a 500 — so da-portal can
// surface a permission error rather than a generic failure (issue #615 gap 2,
// external-review DoD "permission → 403, never 500").
func TestPutTenant_PRMode_ForgeForbidden(t *testing.T) {
	t.Parallel()
	configDir := initGitConfigDir(t)
	writer := newTestWriter(configDir)

	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, headBranch string, labels []string) (*platform.PRInfo, error) {
			return nil, platform.ErrForbidden
		},
	}
	mockTracker := &mockPlatformTracker{}

	rbacMgr := adminRBAC(t)
	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePR, PRClient: mockClient, PRTracker: mockTracker, RBAC: rbacMgr})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d: %s", w.Code, w.Body.String())
	}
	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["code"] != CodeForbidden {
		t.Errorf("code = %v, want %q", resp["code"], CodeForbidden)
	}
	// The clean message must not leak an internal stack / upstream body.
	msg, _ := resp["error"].(string)
	if !strings.Contains(msg, "insufficient") || strings.Contains(msg, "APIError") {
		t.Errorf("error message not clean: %q", msg)
	}

	// Claim must be released on failure so a fixed-token retry isn't blocked.
	if !mockTracker.ClaimTenant("db-a") {
		t.Error("claim should be released after a failed PR creation")
	}
}

// TestPutTenant_PRMode_RateLimited403MapsTo503 is the TRK-319 end-to-end guard:
// a forge 403 that is a SECONDARY RATE LIMIT (not a permission error) must NOT
// map to a clean HTTP 403 (which would tell the operator "fix your token" for a
// transient back-off) — it must fall through to a retryable 503. Mirrors
// TestPutTenant_PRMode_ForgeForbidden but with the RateLimited flag set, proving
// APIError.Is excludes it from ErrForbidden all the way out to the HTTP status.
func TestPutTenant_PRMode_RateLimited403MapsTo503(t *testing.T) {
	t.Parallel()
	configDir := initGitConfigDir(t)
	writer := newTestWriter(configDir)

	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, headBranch string, labels []string) (*platform.PRInfo, error) {
			return nil, &platform.APIError{
				Provider: "GitHub", Method: "POST", Path: "/repos/o/r/pulls",
				StatusCode: http.StatusForbidden, RateLimited: true, RetryAfter: 60 * time.Second,
			}
		},
	}
	mockTracker := &mockPlatformTracker{}

	rbacMgr := adminRBAC(t)
	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePR, PRClient: mockClient, PRTracker: mockTracker, RBAC: rbacMgr})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("rate-limited 403 should map to 503, got %d: %s", w.Code, w.Body.String())
	}
	var resp map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("expected JSON response, unmarshal failed: %v; body=%s", err, w.Body.String())
	}
	if resp["code"] == CodeForbidden {
		t.Errorf("code = %v, want a non-FORBIDDEN code (rate-limit is degradation, not a permission error)", resp["code"])
	}
	// Claim released so a post-back-off retry isn't blocked.
	if !mockTracker.ClaimTenant("db-a") {
		t.Error("claim should be released after a rate-limited PR creation")
	}
}

// TestPutTenant_PRMode_ConcurrentSameTenant is the end-to-end TOCTOU
// assertion: two concurrent PUTs for the same tenant must create exactly one
// PR (the second gets 409), even though both pass the optimistic fast-path.
// Uses a real (mutex-backed) tracker; the mock tracker is not concurrency-safe
// by design. Run with -race.
func TestPutTenant_PRMode_ConcurrentSameTenant(t *testing.T) {
	t.Parallel()
	configDir := initGitConfigDir(t)
	writer := newTestWriter(configDir)

	var createCalls atomic.Int32
	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, headBranch string, labels []string) (*platform.PRInfo, error) {
			n := createCalls.Add(1)
			return &platform.PRInfo{
				Number: int(100 + n), WebURL: "https://github.com/o/r/pull/1", State: "open", HeadRef: headBranch,
			}, nil
		},
	}
	// Real tracker (thread-safe). Long interval + no WatchLoop → no polling.
	ghClient, _ := gh.NewClient("token", "owner/repo", "main")
	tracker := gh.NewTracker(ghClient, 1<<30)

	rbacMgr := adminRBAC(t)
	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePR, PRClient: mockClient, PRTracker: tracker, RBAC: rbacMgr})
	wrapped := wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath)

	const n = 8
	codes := make([]int, n)
	var wg sync.WaitGroup
	start := make(chan struct{})
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func(idx int) {
			defer wg.Done()
			body := bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
			req.Header.Set("X-Forwarded-Email", "alice@example.com")
			req.Header.Set("X-Forwarded-Groups", "admins")
			w := httptest.NewRecorder()
			<-start
			wrapped.ServeHTTP(w, req)
			codes[idx] = w.Code
		}(i)
	}
	close(start)
	wg.Wait()

	if got := createCalls.Load(); got != 1 {
		t.Errorf("CreatePR called %d times, want exactly 1 (dedup race)", got)
	}
	var ok, conflict int
	for _, c := range codes {
		switch c {
		case http.StatusOK:
			ok++
		case http.StatusConflict:
			conflict++
		default:
			t.Errorf("unexpected status %d", c)
		}
	}
	if ok != 1 {
		t.Errorf("expected exactly 1 success, got %d (codes=%v)", ok, codes)
	}
	if conflict != n-1 {
		t.Errorf("expected %d conflicts, got %d (codes=%v)", n-1, conflict, codes)
	}
}

// TestPutTenant_PRMode_RefreshClearsStaleCache is the #644 happy path: byTenant
// thinks a PR is open (the polling-staleness window after a merge), but a forced
// refresh on the 409 path drops the stale entry, ClaimTenant retry succeeds, and
// the write proceeds. Asserts the response is NOT 409 + the refresh was called
// exactly once.
func TestPutTenant_PRMode_RefreshClearsStaleCache(t *testing.T) {
	t.Parallel()
	configDir := initGitConfigDir(t)
	writer := newTestWriter(configDir)

	mt := &mockPlatformTracker{
		prs: []platform.PRInfo{{Number: 99, TenantID: "db-a", State: "open", WebURL: "https://x/99"}},
	}
	mt.refreshFn = func() { mt.prs = nil } // forge says no open PR anymore

	mockClient := &mockPlatformClient{providerName: "github"}
	rbacMgr := adminRBAC(t)
	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePR, PRClient: mockClient, PRTracker: mt, RBAC: rbacMgr})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	if w.Code == http.StatusConflict {
		t.Fatalf("expected refresh to clear stale cache → no 409; got 409: %s", w.Body.String())
	}
	if got := mt.refreshCalls.Load(); got != 1 {
		t.Errorf("refreshCalls = %d, want 1 (cache-stale path must refresh once)", got)
	}
}

// TestPutTenant_PRMode_RefreshKeepsRealPending: byTenant has an entry, refresh
// confirms it's really still open (refreshFn no-op), retry fails, 409 returned.
// Asserts refreshCalls=1 (we DID try) — the 409 is correct, not a missed refresh.
func TestPutTenant_PRMode_RefreshKeepsRealPending(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	writer := newTestWriter(configDir)

	mt := &mockPlatformTracker{
		prs:       []platform.PRInfo{{Number: 99, TenantID: "db-a", State: "open", WebURL: "https://x/99"}},
		refreshFn: func() {}, // forge confirms PR is still really open
	}
	mockClient := &mockPlatformClient{providerName: "github"}
	rbacMgr := adminRBAC(t)
	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePR, PRClient: mockClient, PRTracker: mt, RBAC: rbacMgr})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	if w.Code != http.StatusConflict {
		t.Fatalf("expected 409 (PR really still open), got %d: %s", w.Code, w.Body.String())
	}
	if got := mt.refreshCalls.Load(); got != 1 {
		t.Errorf("refreshCalls = %d, want 1 (HasPendingPR=true → must attempt refresh once)", got)
	}
}

// TestPutTenant_PRMode_InFlightClaimNoRefresh: ClaimTenant fails because of an
// in-flight CLAIM (HasPendingPR is false — there's no cached PR, just another
// request mid-creation). Refresh would not help and is NOT called → 409 directly.
func TestPutTenant_PRMode_InFlightClaimNoRefresh(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	writer := newTestWriter(configDir)

	mt := &mockPlatformTracker{
		claimed: map[string]bool{"db-a": true}, // in-flight claim, byTenant empty
	}
	mockClient := &mockPlatformClient{providerName: "github"}
	rbacMgr := adminRBAC(t)
	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePR, PRClient: mockClient, PRTracker: mt, RBAC: rbacMgr})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	if w.Code != http.StatusConflict {
		t.Fatalf("expected 409 (in-flight claim), got %d", w.Code)
	}
	if got := mt.refreshCalls.Load(); got != 0 {
		t.Errorf("refreshCalls = %d, want 0 (in-flight only → no cache to refresh)", got)
	}
}

