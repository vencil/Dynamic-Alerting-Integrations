package handler

// #1231 1b: author-facing deprecation wiring at the HTTP boundary. A tenant
// body still spelling mysql_threads_running as mysql_cpu must (a) never be
// blocked by any write path, and (b) get the rename notice on every surface:
// PUT 200 (direct + PR mode) `warnings`, batch per-op `warnings`, POST
// /validate `notices` (with `warnings`/`valid` staying blocking-only), and
// GET /{id} `validation_notices` (with `validation_warnings` staying
// Errors-only). Fixtures use the post-rename shipped state (new-spelled
// _defaults.yaml + old-spelled tenant override) — the gitops layer covers the
// pre-rename state.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/testutil"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

const deprecationDefaultsYAML = "defaults:\n  container_cpu: 80\n  mysql_threads_running: 80\n"

// assertHandlerDeprecationNotices pins the same wording contract the gitops
// layer pins (assertDeprecationNotice there): both spellings named, no
// "skipping" substring, no "ERROR:" prefix.
func assertHandlerDeprecationNotices(t *testing.T, notices []string) {
	t.Helper()
	if len(notices) == 0 {
		t.Fatal("expected at least one deprecation notice, got none")
	}
	joined := strings.Join(notices, "; ")
	if !strings.Contains(joined, "mysql_cpu") || !strings.Contains(joined, "mysql_threads_running") {
		t.Errorf("notice must name both spellings, got: %v", notices)
	}
	for _, n := range notices {
		if strings.Contains(n, "skipping") {
			t.Errorf("notice must not contain 'skipping', got: %q", n)
		}
		if strings.HasPrefix(strings.TrimSpace(n), "ERROR:") {
			t.Errorf("notice must not start with 'ERROR:', got: %q", n)
		}
	}
}

// TestPutTenant_DeprecatedKey_DirectMode200CarriesWarnings is the end-to-end
// direct-write proof: deprecated body → committed to disk → 200 with the
// migration signal in `warnings`.
func TestPutTenant_DeprecatedKey_DirectMode200CarriesWarnings(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{"_defaults.yaml": deprecationDefaultsYAML})
	initGitRepo(t, configDir)
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, policyTestRBACYAML)

	h := PutTenant(&Deps{Writer: gw, ConfigDir: configDir, RBAC: rbacMgr, WriteMode: WriteModeDirect})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    mysql_cpu: \"70\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	policyTestIdentity(req)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("PutTenant() status = %d, want 200 (deprecated key must NOT block); body: %s",
			w.Code, w.Body.String())
	}
	var resp PutTenantResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Status != "ok" {
		t.Errorf("status = %q, want ok", resp.Status)
	}
	assertHandlerDeprecationNotices(t, resp.Warnings)
	// The write really landed — 200-with-warnings is a SUCCESS signal.
	if _, err := os.Stat(filepath.Join(configDir, "db-a.yaml")); err != nil {
		t.Errorf("200 response but no file on disk: %v", err)
	}
}

// TestPutTenant_DeprecatedKey_PRMode200CarriesWarnings covers the PR-mode 200
// (pending_review): PRWriteResult.Notices must reach the response `warnings`.
func TestPutTenant_DeprecatedKey_PRMode200CarriesWarnings(t *testing.T) {
	t.Parallel()
	configDir := initGitConfigDir(t)
	testutil.WriteYAML(t, configDir, "_defaults.yaml", deprecationDefaultsYAML)
	writer := newTestWriter(configDir)

	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, headBranch string, labels []string) (*platform.PRInfo, error) {
			return &platform.PRInfo{Number: 7, WebURL: "https://gh/7", State: "open", Title: title, HeadRef: headBranch}, nil
		},
	}
	rbacMgr := newRBACManager(t, policyTestRBACYAML)
	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePR, PRClient: mockClient, PRTracker: &mockPlatformTracker{}, RBAC: rbacMgr})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    mysql_cpu: \"70\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	// Routed through the RBAC middleware so the feature-branch git commit gets
	// a non-empty author identity (same discipline as the direct-mode test).
	policyTestIdentity(req)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("PR-mode PUT status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var resp PutTenantResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Status != "pending_review" {
		t.Errorf("status = %q, want pending_review", resp.Status)
	}
	assertHandlerDeprecationNotices(t, resp.Warnings)
}

// TestBatchTenants_DeprecatedKeyPatch_PerOpWarnings covers the direct batch
// path: the succeeding op's BatchResult carries its own warnings list.
func TestBatchTenants_DeprecatedKeyPatch_PerOpWarnings(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{"_defaults.yaml": deprecationDefaultsYAML})
	initGitRepo(t, configDir)
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, policyTestRBACYAML)

	h := BatchTenants(&Deps{Writer: gw, ConfigDir: configDir, RBAC: rbacMgr, WriteMode: WriteModeDirect})
	reqBody, _ := json.Marshal(BatchRequest{Operations: []BatchOperation{
		{TenantID: "db-a", Patch: map[string]string{"mysql_cpu": "70"}},
		{TenantID: "db-b", Patch: map[string]string{"container_cpu": "75"}},
	}})
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBuffer(reqBody))
	req.Header.Set("Content-Type", "application/json")
	policyTestIdentity(req)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, nil).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("BatchTenants() status = %d, body: %s", w.Code, w.Body.String())
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	byTenant := map[string]BatchResult{}
	for _, r := range resp.Results {
		byTenant[r.TenantID] = r
	}
	dep := byTenant["db-a"]
	if dep.Status != "ok" {
		t.Fatalf("deprecated-key op must still succeed, got: %+v", dep)
	}
	assertHandlerDeprecationNotices(t, dep.Warnings)
	// The clean sibling carries NO warnings — the signal is per-tenant, not
	// smeared across the whole batch.
	if clean := byTenant["db-b"]; clean.Status != "ok" || len(clean.Warnings) != 0 {
		t.Errorf("clean op result = %+v, want ok with no warnings", clean)
	}
}

// TestBatchTenants_PRModeAllNoOp_200CarriesWarnings (#1231 review F5): an
// all-no-op PR-mode batch (idempotent patch / client retry) returns the clean
// "no changes" 200 — and that 200 must still carry the deprecation warnings
// the per-op merges collected, matching the direct path (WriteMerged keeps
// notices on its no-op short-circuit). Before the fix the ErrNoChanges branch
// answered with an empty Warnings list, so exactly the retry case lost the
// tenant's migration signal.
func TestBatchTenants_PRModeAllNoOp_200CarriesWarnings(t *testing.T) {
	t.Parallel()
	configDir := initGitConfigDir(t)
	testutil.WriteYAML(t, configDir, "_defaults.yaml", deprecationDefaultsYAML)

	// Fixed-point seed: commit the EXACT bytes mergePatchYAML produces for
	// this patch, so replaying the same patch is byte-identical (a true no-op).
	patch := map[string]string{"mysql_cpu": "70"}
	seed, err := mergePatchYAML([]byte("tenants:\n  db-a:\n    mysql_cpu: \"70\"\n"), "db-a", patch)
	if err != nil {
		t.Fatalf("build fixed-point seed: %v", err)
	}
	testutil.WriteYAML(t, configDir, "db-a.yaml", seed)
	writer := newTestWriter(configDir)

	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, headBranch string, labels []string) (*platform.PRInfo, error) {
			t.Error("an all-no-op batch must never open a PR/MR")
			return &platform.PRInfo{Number: 1, WebURL: "https://gh/1", State: "open"}, nil
		},
	}
	rbacMgr := newRBACManager(t, policyTestRBACYAML)
	h := BatchTenants(&Deps{Writer: writer, WriteMode: WriteModePR, PRClient: mockClient, PRTracker: &mockPlatformTracker{}, RBAC: rbacMgr})

	reqBody, _ := json.Marshal(BatchRequest{Operations: []BatchOperation{{TenantID: "db-a", Patch: patch}}})
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBuffer(reqBody))
	req.Header.Set("Content-Type", "application/json")
	policyTestIdentity(req)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, nil).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("all-no-op PR-mode batch status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Status != "completed" || resp.PRURL != "" {
		t.Errorf("want the clean no-changes completion (no PR), got status=%q pr_url=%q", resp.Status, resp.PRURL)
	}
	if !strings.Contains(resp.Message, "No changes") {
		t.Errorf("message should state no changes were applied, got: %q", resp.Message)
	}
	assertHandlerDeprecationNotices(t, resp.Warnings)
}

// TestValidateTenant_DeprecatedKey_ChannelSplit locks the /validate three-field
// contract: `valid` and `warnings` see ONLY the blocking set; the deprecation
// advisory rides `notices` — for a body carrying both an unknown key and a
// deprecated key, the two channels must not bleed into each other.
func TestValidateTenant_DeprecatedKey_ChannelSplit(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{"_defaults.yaml": deprecationDefaultsYAML})
	h := ValidateTenant(&Deps{ConfigDir: configDir})

	// Case 1: deprecated key only → valid, no warnings, notices present.
	body := bytes.NewBufferString("tenants:\n  db-a:\n    mysql_cpu: \"70\"\n")
	req := newRequestWithChiParam("POST", "/api/v1/tenants/db-a/validate", "id", "db-a", body)
	w := httptest.NewRecorder()
	h(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("validate status = %d, body: %s", w.Code, w.Body.String())
	}
	var resp ValidateResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if !resp.Valid {
		t.Errorf("deprecated-but-aliased body must stay VALID (verdict gates on blocking set only), warnings: %v", resp.Warnings)
	}
	if len(resp.Warnings) != 0 {
		t.Errorf("warnings must stay blocking-only (empty here), got: %v", resp.Warnings)
	}
	assertHandlerDeprecationNotices(t, resp.Notices)

	// Case 2: deprecated key + unknown key → invalid; the unknown key is in
	// warnings, the rename notice stays in notices (no channel bleed).
	body = bytes.NewBufferString("tenants:\n  db-a:\n    mysql_cpu: \"70\"\n    not_a_real_metric: \"1\"\n")
	req = newRequestWithChiParam("POST", "/api/v1/tenants/db-a/validate", "id", "db-a", body)
	w = httptest.NewRecorder()
	h(w, req)
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Valid {
		t.Error("unknown key must still invalidate the body")
	}
	joined := strings.Join(resp.Warnings, "; ")
	if !strings.Contains(joined, "not_a_real_metric") {
		t.Errorf("warnings must carry the blocking unknown-key entry, got: %v", resp.Warnings)
	}
	if strings.Contains(joined, "renamed") {
		t.Errorf("the rename notice must NOT ride the blocking warnings channel, got: %v", resp.Warnings)
	}
	assertHandlerDeprecationNotices(t, resp.Notices)
}

// TestGetTenant_DeprecatedKey_NoticesField locks the GET split: a file whose
// only issue is the deprecated spelling reports validation_notices and an
// EMPTY validation_warnings (Errors-only, as before #1231's 1a interim merge).
func TestGetTenant_DeprecatedKey_NoticesField(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": deprecationDefaultsYAML,
		"db-a.yaml":      "tenants:\n  db-a:\n    mysql_cpu: \"70\"\n",
	})

	h := GetTenant(&Deps{ConfigDir: configDir})
	req := newRequestWithChiParam("GET", "/api/v1/tenants/db-a", "id", "db-a", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("GetTenant() status = %d, body: %s", w.Code, w.Body.String())
	}
	var detail TenantDetail
	if err := json.Unmarshal(w.Body.Bytes(), &detail); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(detail.Warnings) != 0 {
		t.Errorf("validation_warnings must stay Errors-only (empty here), got: %v", detail.Warnings)
	}
	assertHandlerDeprecationNotices(t, detail.Notices)
}

// TestPutCustomAlerts_DeprecatedKeyFile_200CarriesWarnings covers the portal's
// only tenant-file write path (PUT .../custom-alerts): saving recipes into a
// tenant file that still carries a deprecated threshold key succeeds AND
// surfaces the migration signal in the response `warnings`.
func TestPutCustomAlerts_DeprecatedKeyFile_200CarriesWarnings(t *testing.T) {
	tenantYAML := "tenants:\n  db-a:\n    mysql_cpu: \"70\"\n"
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": deprecationDefaultsYAML,
		"db-a.yaml":      tenantYAML,
	})
	initGitRepo(t, configDir)
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, policyTestRBACYAML)

	h := PutTenantCustomAlerts(&Deps{Writer: gw, ConfigDir: configDir, RBAC: rbacMgr, WriteMode: WriteModeDirect})
	payload := map[string]any{
		"custom_alerts": []map[string]any{{
			"recipe": "threshold", "name": "q", "metric": "qd",
			"op": ">", "window": "5m", "threshold": "1:warning",
		}},
		"base_hash": cfg.ComputeSourceHash([]byte(tenantYAML)),
	}
	raw, _ := json.Marshal(payload)
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a/custom-alerts", "id", "db-a", bytes.NewBuffer(raw))
	req.Header.Set("Content-Type", "application/json")
	policyTestIdentity(req)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("PutTenantCustomAlerts() status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var resp PutCustomAlertsResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	assertHandlerDeprecationNotices(t, resp.Warnings)
}
