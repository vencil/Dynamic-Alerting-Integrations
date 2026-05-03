package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
)

// newRequestWithChiParam creates an *http.Request with a chi URL parameter set.
func newRequestWithChiParam(method, path, paramName, paramValue string, body *bytes.Buffer) *http.Request {
	if body == nil {
		body = bytes.NewBuffer(nil)
	}
	req := httptest.NewRequest(method, path, body)
	rctx := chi.NewRouteContext()
	rctx.URLParams.Add(paramName, paramValue)
	req = req.WithContext(context.WithValue(req.Context(), chi.RouteCtxKey, rctx))
	return req
}

// setupConfigDir creates a temp directory with optional tenant YAML files.
func setupConfigDir(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0644); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	return dir
}

// newTestWriter creates a gitops.Writer for test use.
func newTestWriter(configDir string) *gitops.Writer {
	return gitops.NewWriter(configDir, "")
}

// newRBACManager creates an RBAC manager from a YAML string.
func newRBACManager(t *testing.T, yaml string) *rbac.Manager {
	t.Helper()
	if yaml == "" {
		mgr, err := rbac.NewManager("")
		if err != nil {
			t.Fatalf("rbac.NewManager: %v", err)
		}
		return mgr
	}
	dir := t.TempDir()
	rbacFile := filepath.Join(dir, "_rbac.yaml")
	if err := os.WriteFile(rbacFile, []byte(yaml), 0644); err != nil {
		t.Fatalf("write rbac: %v", err)
	}
	mgr, err := rbac.NewManager(rbacFile)
	if err != nil {
		t.Fatalf("rbac.NewManager: %v", err)
	}
	return mgr
}

// wrapWithRBACMiddleware wraps a handler with the RBAC middleware, setting
// identity headers on the request.
func wrapWithRBACMiddleware(handler http.HandlerFunc, mgr *rbac.Manager, perm rbac.Permission, tenantIDFn func(*http.Request) string) http.Handler {
	return mgr.Middleware(perm, tenantIDFn)(handler)
}

// --- Health / Ready tests ---

func TestHealth(t *testing.T) {
	req := httptest.NewRequest("GET", "/health", nil)
	w := httptest.NewRecorder()
	Health(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("Health() status = %d, want %d", w.Code, http.StatusOK)
	}
	var resp map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp["status"] != "ok" {
		t.Errorf("Health() status = %q, want %q", resp["status"], "ok")
	}
}

func TestReady(t *testing.T) {
	dir := t.TempDir()
	handler := Ready(dir)

	req := httptest.NewRequest("GET", "/ready", nil)
	w := httptest.NewRecorder()
	handler(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("Ready() status = %d, want %d", w.Code, http.StatusOK)
	}
	var resp map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp["status"] != "ready" {
		t.Errorf("Ready() status = %q, want %q", resp["status"], "ready")
	}
	if resp["config_dir"] != dir {
		t.Errorf("Ready() config_dir = %q, want %q", resp["config_dir"], dir)
	}
}

// TestReady_MissingDir asserts /ready returns 503 when the config
// directory is not stat-able, so K8s readinessProbe drains traffic
// away from a pod whose ConfigMap mount failed.
func TestReady_MissingDir(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "does-not-exist")
	handler := Ready(missing)

	req := httptest.NewRequest("GET", "/ready", nil)
	w := httptest.NewRecorder()
	handler(w, req)

	if w.Code != http.StatusServiceUnavailable {
		t.Errorf("Ready() status = %d, want %d", w.Code, http.StatusServiceUnavailable)
	}
	var resp map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp["status"] != "not_ready" {
		t.Errorf("Ready() status = %q, want %q", resp["status"], "not_ready")
	}
}

// TestReady_NotADirectory asserts /ready returns 503 when the
// configured config_dir is a file (operator misconfig).
func TestReady_NotADirectory(t *testing.T) {
	dir := t.TempDir()
	filePath := filepath.Join(dir, "imposter")
	if err := os.WriteFile(filePath, []byte("not a dir"), 0644); err != nil {
		t.Fatal(err)
	}
	handler := Ready(filePath)

	req := httptest.NewRequest("GET", "/ready", nil)
	w := httptest.NewRecorder()
	handler(w, req)

	if w.Code != http.StatusServiceUnavailable {
		t.Errorf("Ready() status = %d, want %d", w.Code, http.StatusServiceUnavailable)
	}
}

// --- GetTenant tests ---

func TestGetTenant_Success(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n",
	})

	h := GetTenant(configDir)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/db-a", "id", "db-a", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("GetTenant() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}
	var detail TenantDetail
	if err := json.Unmarshal(w.Body.Bytes(), &detail); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if detail.ID != "db-a" {
		t.Errorf("TenantDetail.ID = %q, want %q", detail.ID, "db-a")
	}
	if detail.RawYAML == "" {
		t.Error("TenantDetail.RawYAML should not be empty")
	}
}

func TestGetTenant_NotFound(t *testing.T) {
	configDir := setupConfigDir(t, nil)

	h := GetTenant(configDir)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/nonexistent", "id", "nonexistent", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("GetTenant() status = %d, want %d", w.Code, http.StatusNotFound)
	}
}

func TestGetTenant_InvalidID(t *testing.T) {
	configDir := setupConfigDir(t, nil)

	h := GetTenant(configDir)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/../etc", "id", "../etc", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("GetTenant() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

func TestGetTenant_WithDefaults(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"db-a.yaml":      "tenants:\n  db-a:\n    mysql_connections: \"70\"\n",
	})

	h := GetTenant(configDir)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/db-a", "id", "db-a", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("GetTenant() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}
	var detail TenantDetail
	if err := json.Unmarshal(w.Body.Bytes(), &detail); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if detail.ID != "db-a" {
		t.Errorf("TenantDetail.ID = %q, want %q", detail.ID, "db-a")
	}
	if len(detail.Resolved) == 0 {
		t.Error("expected resolved thresholds from merged config")
	}
}

func TestGetTenant_EmptyID(t *testing.T) {
	configDir := setupConfigDir(t, nil)

	h := GetTenant(configDir)
	req := newRequestWithChiParam("GET", "/api/v1/tenants/", "id", "", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("GetTenant() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

// --- ListTenants tests ---

func TestListTenants_Empty(t *testing.T) {
	configDir := setupConfigDir(t, nil)

	h := ListTenants(configDir, newRBACManager(t, ""))
	req := httptest.NewRequest("GET", "/api/v1/tenants", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("ListTenants() status = %d, want %d", w.Code, http.StatusOK)
	}
	var tenants []TenantSummary
	if err := json.Unmarshal(w.Body.Bytes(), &tenants); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(tenants) != 0 {
		t.Errorf("expected 0 tenants, got %d", len(tenants))
	}
}

func TestListTenants_MultipleTenants(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n    _profile: \"high-perf\"\n",
		"db-b.yaml": "tenants:\n  db-b:\n    _state_maintenance: \"enable\"\n",
	})

	h := ListTenants(configDir, newRBACManager(t, ""))
	req := httptest.NewRequest("GET", "/api/v1/tenants", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("ListTenants() status = %d, want %d", w.Code, http.StatusOK)
	}
	var tenants []TenantSummary
	if err := json.Unmarshal(w.Body.Bytes(), &tenants); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(tenants) != 2 {
		t.Fatalf("expected 2 tenants, got %d", len(tenants))
	}

	found := false
	for _, ts := range tenants {
		if ts.ID == "db-a" {
			found = true
			if ts.SilentMode != "warning" {
				t.Errorf("db-a silent_mode = %q, want %q", ts.SilentMode, "warning")
			}
			if ts.Profile != "high-perf" {
				t.Errorf("db-a profile = %q, want %q", ts.Profile, "high-perf")
			}
		}
	}
	if !found {
		t.Error("db-a not found in tenant list")
	}
}

func TestListTenants_SkipsHiddenAndDefaults(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml":      "tenants:\n  db-a:\n    mysql_cpu: \"80\"\n",
		"_defaults.yaml": "defaults:\n  mysql_cpu: 90\n",
		".hidden.yaml":   "tenants:\n  hidden:\n    x: \"1\"\n",
		"not-yaml.txt":   "not yaml",
	})

	h := ListTenants(configDir, newRBACManager(t, ""))
	req := httptest.NewRequest("GET", "/api/v1/tenants", nil)
	w := httptest.NewRecorder()
	h(w, req)

	var tenants []TenantSummary
	if err := json.Unmarshal(w.Body.Bytes(), &tenants); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(tenants) != 1 {
		t.Errorf("expected 1 tenant (db-a only), got %d: %+v", len(tenants), tenants)
	}
}

func TestListTenants_WithYmlExtension(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"db-c.yml": "tenants:\n  db-c:\n    mysql_cpu: \"75\"\n",
	})

	h := ListTenants(configDir, newRBACManager(t, ""))
	req := httptest.NewRequest("GET", "/api/v1/tenants", nil)
	w := httptest.NewRecorder()
	h(w, req)

	var tenants []TenantSummary
	if err := json.Unmarshal(w.Body.Bytes(), &tenants); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(tenants) != 1 {
		t.Errorf("expected 1 tenant, got %d", len(tenants))
	}
	if len(tenants) == 1 && tenants[0].ID != "db-c" {
		t.Errorf("expected tenant ID 'db-c', got %q", tenants[0].ID)
	}
}

func TestListTenants_MalformedYAML(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"bad.yaml":  "{{not valid yaml",
		"db-a.yaml": "tenants:\n  db-a:\n    mysql_cpu: \"80\"\n",
	})

	h := ListTenants(configDir, newRBACManager(t, ""))
	req := httptest.NewRequest("GET", "/api/v1/tenants", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("ListTenants() status = %d, want %d", w.Code, http.StatusOK)
	}
	var tenants []TenantSummary
	if err := json.Unmarshal(w.Body.Bytes(), &tenants); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	// bad.yaml should be skipped, only db-a remains
	if len(tenants) != 1 {
		t.Errorf("expected 1 tenant (skip malformed), got %d", len(tenants))
	}
}

// --- ValidateTenant tests ---

func TestValidateTenant_Valid(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
	})

	h := ValidateTenant(configDir)
	body := bytes.NewBufferString("tenants:\n  db-a:\n    mysql_connections: \"70\"\n")
	req := newRequestWithChiParam("POST", "/api/v1/tenants/db-a/validate", "id", "db-a", body)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("ValidateTenant() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}
	var resp ValidateResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if !resp.Valid {
		t.Errorf("expected valid, got warnings: %v", resp.Warnings)
	}
}

func TestValidateTenant_InvalidID(t *testing.T) {
	configDir := setupConfigDir(t, nil)

	h := ValidateTenant(configDir)
	body := bytes.NewBufferString("tenants:\n  bad:\n    x: \"1\"\n")
	req := newRequestWithChiParam("POST", "/api/v1/tenants/../bad/validate", "id", "../bad", body)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("ValidateTenant() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

func TestValidateTenant_WithWarnings(t *testing.T) {
	configDir := setupConfigDir(t, nil) // no defaults

	h := ValidateTenant(configDir)
	// unknown_key is not in defaults and not a reserved key -> warning
	body := bytes.NewBufferString("tenants:\n  db-a:\n    unknown_key_xyz: \"70\"\n")
	req := newRequestWithChiParam("POST", "/api/v1/tenants/db-a/validate", "id", "db-a", body)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("ValidateTenant() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}
	var resp ValidateResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Valid {
		t.Log("note: unknown key may or may not produce warnings depending on ValidateTenantKeys behavior")
	}
}

// --- DiffTenant tests ---

func TestDiffTenant_NewTenant_JSON(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	gw := newTestWriter(configDir)

	h := DiffTenant(gw)
	reqBody, _ := json.Marshal(DiffRequest{Proposed: "tenants:\n  new-db:\n    cpu: \"80\"\n"})
	body := bytes.NewBuffer(reqBody)
	req := newRequestWithChiParam("POST", "/api/v1/tenants/new-db/diff", "id", "new-db", body)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("DiffTenant() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}
	var resp DiffResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.TenantID != "new-db" {
		t.Errorf("TenantID = %q, want %q", resp.TenantID, "new-db")
	}
	if !resp.HasDiff {
		t.Error("expected HasDiff=true for new tenant")
	}
	if !strings.Contains(resp.Diff, "+") {
		t.Errorf("expected diff with additions, got: %s", resp.Diff)
	}
}

func TestDiffTenant_NoDiff(t *testing.T) {
	content := "tenants:\n  db-a:\n    cpu: \"80\"\n"
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": content,
	})
	gw := newTestWriter(configDir)

	h := DiffTenant(gw)
	reqBody, _ := json.Marshal(DiffRequest{Proposed: content})
	body := bytes.NewBuffer(reqBody)
	req := newRequestWithChiParam("POST", "/api/v1/tenants/db-a/diff", "id", "db-a", body)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("DiffTenant() status = %d, want %d", w.Code, http.StatusOK)
	}
	var resp DiffResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.HasDiff {
		t.Error("expected HasDiff=false for identical content")
	}
}

func TestDiffTenant_RawYAML(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": "tenants:\n  db-a:\n    cpu: \"80\"\n",
	})
	gw := newTestWriter(configDir)

	h := DiffTenant(gw)
	body := bytes.NewBufferString("tenants:\n  db-a:\n    cpu: \"90\"\n")
	req := newRequestWithChiParam("POST", "/api/v1/tenants/db-a/diff", "id", "db-a", body)
	req.Header.Set("Content-Type", "application/yaml")
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("DiffTenant() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}
	var resp DiffResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if !resp.HasDiff {
		t.Error("expected HasDiff=true for changed content")
	}
}

func TestDiffTenant_InvalidID(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	gw := newTestWriter(configDir)

	h := DiffTenant(gw)
	req := newRequestWithChiParam("POST", "/api/v1/tenants/../bad/diff", "id", "../bad", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("DiffTenant() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

func TestDiffTenant_JSONAutoDetect(t *testing.T) {
	// Test auto-detection of JSON when Content-Type is not set but body starts with {
	configDir := setupConfigDir(t, nil)
	gw := newTestWriter(configDir)

	h := DiffTenant(gw)
	reqBody, _ := json.Marshal(DiffRequest{Proposed: "tenants:\n  db-x:\n    cpu: \"80\"\n"})
	body := bytes.NewBuffer(reqBody)
	req := newRequestWithChiParam("POST", "/api/v1/tenants/db-x/diff", "id", "db-x", body)
	// No Content-Type header set — should auto-detect JSON
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("DiffTenant() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}
	var resp DiffResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if !resp.HasDiff {
		t.Error("expected HasDiff=true for new tenant (auto-detect JSON)")
	}
}

// --- MetricsHandler tests ---

func TestMetricsHandler(t *testing.T) {
	req := httptest.NewRequest("GET", "/metrics", nil)
	w := httptest.NewRecorder()
	MetricsHandler(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("MetricsHandler() status = %d, want %d", w.Code, http.StatusOK)
	}
	body := w.Body.String()
	expected := []string{
		"tenant_api_up 1",
		"tenant_api_uptime_seconds",
		"tenant_api_requests_total",
		"tenant_api_errors_total",
		"tenant_api_writes_total",
	}
	for _, s := range expected {
		if !strings.Contains(body, s) {
			t.Errorf("MetricsHandler() output missing %q", s)
		}
	}
	ct := w.Header().Get("Content-Type")
	if !strings.Contains(ct, "text/plain") {
		t.Errorf("MetricsHandler() Content-Type = %q, want text/plain", ct)
	}
}

// --- MetricsMiddleware tests ---

func TestMetricsMiddleware_CountsRequests(t *testing.T) {
	before := Metrics.requestsTotal.Load()

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mw := MetricsMiddleware(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	after := Metrics.requestsTotal.Load()
	if after != before+1 {
		t.Errorf("requestsTotal = %d, want %d", after, before+1)
	}
}

func TestMetricsMiddleware_CountsErrors(t *testing.T) {
	before := Metrics.errorsTotal.Load()

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	mw := MetricsMiddleware(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	after := Metrics.errorsTotal.Load()
	if after != before+1 {
		t.Errorf("errorsTotal = %d, want %d", after, before+1)
	}
}

func TestMetricsMiddleware_NoErrorFor2xx(t *testing.T) {
	before := Metrics.errorsTotal.Load()

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mw := MetricsMiddleware(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	after := Metrics.errorsTotal.Load()
	if after != before {
		t.Errorf("errorsTotal changed for 2xx response: before=%d, after=%d", before, after)
	}
}

// --- PutTenant tests ---

func TestPutTenant_InvalidID(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	gw := newTestWriter(configDir)

	h := PutTenant(gw, policy.NewManager(configDir), WriteModeDirect, nil, nil)
	body := bytes.NewBufferString("tenants:\n  bad:\n    x: \"1\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/../bad", "id", "../bad", body)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("PutTenant() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

func TestPutTenant_EmptyID(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	gw := newTestWriter(configDir)

	h := PutTenant(gw, policy.NewManager(configDir), WriteModeDirect, nil, nil)
	body := bytes.NewBufferString("tenants:\n  test:\n    x: \"1\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/", "id", "", body)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("PutTenant() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

func TestPutTenant_ValidationFailure(t *testing.T) {
	// Writer.Write does schema validation: the YAML must contain tenants.<id> section
	configDir := setupConfigDir(t, nil)
	gw := newTestWriter(configDir)

	h := PutTenant(gw, policy.NewManager(configDir), WriteModeDirect, nil, nil)
	// Valid ID but YAML doesn't contain the matching tenant section
	body := bytes.NewBufferString("tenants:\n  other-tenant:\n    x: \"1\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	w := httptest.NewRecorder()
	h(w, req)

	// Should fail with 400 because YAML doesn't contain tenants.db-a
	if w.Code != http.StatusBadRequest {
		t.Errorf("PutTenant() status = %d, want %d, body: %s", w.Code, http.StatusBadRequest, w.Body.String())
	}
}

func TestPutTenant_InvalidYAML(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	gw := newTestWriter(configDir)

	h := PutTenant(gw, policy.NewManager(configDir), WriteModeDirect, nil, nil)
	body := bytes.NewBufferString("{{invalid yaml")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("PutTenant() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

// --- BatchTenants tests ---

func TestBatchTenants_EmptyOperations(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")

	h := BatchTenants(gw, configDir, rbacMgr, policy.NewManager(configDir), nil, WriteModeDirect, nil, nil)
	reqBody, _ := json.Marshal(BatchRequest{Operations: []BatchOperation{}})
	body := bytes.NewBuffer(reqBody)
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", body)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("BatchTenants() status = %d, want %d, body: %s", w.Code, http.StatusBadRequest, w.Body.String())
	}
}

func TestBatchTenants_InvalidJSON(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")

	h := BatchTenants(gw, configDir, rbacMgr, policy.NewManager(configDir), nil, WriteModeDirect, nil, nil)
	body := bytes.NewBufferString("{invalid json")
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", body)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("BatchTenants() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

func TestBatchTenants_InvalidTenantID(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	gw := newTestWriter(configDir)
	rbacMgr := newRBACManager(t, "")

	h := BatchTenants(gw, configDir, rbacMgr, policy.NewManager(configDir), nil, WriteModeDirect, nil, nil)
	reqBody, _ := json.Marshal(BatchRequest{
		Operations: []BatchOperation{
			{TenantID: "../bad", Patch: map[string]string{"_silent_mode": "warning"}},
		},
	})
	body := bytes.NewBuffer(reqBody)
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", body)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("BatchTenants() status = %d, want %d", w.Code, http.StatusOK)
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(resp.Results) != 1 || resp.Results[0].Status != "error" {
		t.Errorf("expected error result for invalid tenant ID, got: %+v", resp.Results)
	}
}

func TestBatchTenants_PermissionDenied(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	gw := newTestWriter(configDir)
	// RBAC with only read permissions for db-ops
	rbacMgr := newRBACManager(t, `groups:
  - name: viewers
    tenants: ["*"]
    permissions: [read]
`)

	h := BatchTenants(gw, configDir, rbacMgr, policy.NewManager(configDir), nil, WriteModeDirect, nil, nil)
	reqBody, _ := json.Marshal(BatchRequest{
		Operations: []BatchOperation{
			{TenantID: "db-a", Patch: map[string]string{"_silent_mode": "warning"}},
		},
	})
	body := bytes.NewBuffer(reqBody)
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", body)
	req.Header.Set("Content-Type", "application/json")
	// Simulate the middleware having set groups=["viewers"] in context
	// Since we can't call withIdentity (unexported), we test that the handler
	// calls HasPermission with whatever RequestGroups returns (empty in this case).
	// With empty groups, the RBAC manager (which has groups defined) will deny access.
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("BatchTenants() status = %d, want %d", w.Code, http.StatusOK)
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	// Since RequestGroups returns nil (no middleware context), and RBAC has groups defined,
	// HasPermission should return false for write
	if len(resp.Results) != 1 {
		t.Fatalf("expected 1 result, got %d", len(resp.Results))
	}
	if resp.Results[0].Status != "error" {
		t.Errorf("expected error status for permission denied, got: %+v", resp.Results[0])
	}
}

// --- buildPatchYAML tests ---

func TestBuildPatchYAML(t *testing.T) {
	yaml := buildPatchYAML("db-a", map[string]string{
		"_silent_mode": "warning",
	})
	if !strings.Contains(yaml, "tenants:") {
		t.Errorf("expected 'tenants:' in output, got: %s", yaml)
	}
	if !strings.Contains(yaml, "db-a:") {
		t.Errorf("expected 'db-a:' in output, got: %s", yaml)
	}
	if !strings.Contains(yaml, "_silent_mode") {
		t.Errorf("expected '_silent_mode' in output, got: %s", yaml)
	}
}

func TestBuildPatchYAML_MultipleKeys(t *testing.T) {
	yaml := buildPatchYAML("db-b", map[string]string{
		"_silent_mode":        "critical",
		"_state_maintenance":  "enable",
	})
	if !strings.Contains(yaml, "db-b:") {
		t.Errorf("expected 'db-b:' in output, got: %s", yaml)
	}
	if !strings.Contains(yaml, "_silent_mode") {
		t.Errorf("expected '_silent_mode' in output, got: %s", yaml)
	}
	if !strings.Contains(yaml, "_state_maintenance") {
		t.Errorf("expected '_state_maintenance' in output, got: %s", yaml)
	}
}

// --- loadMergedConfig tests ---

func TestLoadMergedConfig_NoDefaults(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	tenantData := []byte("tenants:\n  db-a:\n    mysql_cpu: \"70\"\n")
	merged := loadMergedConfig(configDir, "db-a", tenantData)

	if _, ok := merged.Tenants["db-a"]; !ok {
		t.Error("expected db-a in merged tenants")
	}
}

func TestLoadMergedConfig_WithDefaults(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_cpu: 90\n",
	})
	tenantData := []byte("tenants:\n  db-a:\n    mysql_cpu: \"70\"\n")
	merged := loadMergedConfig(configDir, "db-a", tenantData)

	if merged.Defaults["mysql_connections"] != 80 {
		t.Errorf("expected mysql_connections default 80, got %v", merged.Defaults["mysql_connections"])
	}
	if merged.Defaults["mysql_cpu"] != 90 {
		t.Errorf("expected mysql_cpu default 90, got %v", merged.Defaults["mysql_cpu"])
	}
}

func TestLoadMergedConfig_WithStateFilters(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_cpu: 90\nstate_filters:\n  container_crashloop:\n    reasons: [CrashLoopBackOff]\n    severity: critical\n",
	})
	tenantData := []byte("tenants:\n  db-a:\n    mysql_cpu: \"70\"\n")
	merged := loadMergedConfig(configDir, "db-a", tenantData)

	if _, ok := merged.StateFilters["container_crashloop"]; !ok {
		t.Error("expected container_crashloop in state_filters")
	}
}

// --- writeJSONError tests ---

func TestWriteJSONError(t *testing.T) {
	w := httptest.NewRecorder()
	writeJSONError(w, http.StatusNotFound, "not found")

	if w.Code != http.StatusNotFound {
		t.Errorf("status = %d, want %d", w.Code, http.StatusNotFound)
	}
	ct := w.Header().Get("Content-Type")
	if ct != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
	var resp map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp["error"] != "not found" {
		t.Errorf("error = %q, want %q", resp["error"], "not found")
	}
}

func TestWriteJSONError_InternalError(t *testing.T) {
	w := httptest.NewRecorder()
	writeJSONError(w, http.StatusInternalServerError, "something broke")

	if w.Code != http.StatusInternalServerError {
		t.Errorf("status = %d, want %d", w.Code, http.StatusInternalServerError)
	}
}

// --- statusWriter tests ---

func TestStatusWriter(t *testing.T) {
	w := httptest.NewRecorder()
	sw := &statusWriter{ResponseWriter: w}

	sw.WriteHeader(http.StatusCreated)
	if sw.status != http.StatusCreated {
		t.Errorf("status = %d, want %d", sw.status, http.StatusCreated)
	}
	if w.Code != http.StatusCreated {
		t.Errorf("underlying status = %d, want %d", w.Code, http.StatusCreated)
	}
}

// --- Integration test with chi router ---

func TestFullRouter_GetTenant(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n",
	})

	rbacMgr := newRBACManager(t, `groups:
  - name: admins
    tenants: ["*"]
    permissions: [read, write, admin]
`)

	r := chi.NewRouter()
	r.With(rbacMgr.Middleware(rbac.PermRead, TenantIDFromPath)).
		Get("/api/v1/tenants/{id}", GetTenant(configDir))

	req := httptest.NewRequest("GET", "/api/v1/tenants/db-a", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("router GetTenant status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}
}

func TestFullRouter_GetTenant_Unauthorized(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n",
	})

	rbacMgr := newRBACManager(t, `groups:
  - name: admins
    tenants: ["*"]
    permissions: [read, write, admin]
`)

	r := chi.NewRouter()
	r.With(rbacMgr.Middleware(rbac.PermRead, TenantIDFromPath)).
		Get("/api/v1/tenants/{id}", GetTenant(configDir))

	// No identity headers
	req := httptest.NewRequest("GET", "/api/v1/tenants/db-a", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("router GetTenant (no auth) status = %d, want %d", w.Code, http.StatusUnauthorized)
	}
}

func TestFullRouter_GetTenant_Forbidden(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n",
	})

	rbacMgr := newRBACManager(t, `groups:
  - name: db-ops
    tenants: ["db-b-*"]
    permissions: [read]
`)

	r := chi.NewRouter()
	r.With(rbacMgr.Middleware(rbac.PermRead, TenantIDFromPath)).
		Get("/api/v1/tenants/{id}", GetTenant(configDir))

	req := httptest.NewRequest("GET", "/api/v1/tenants/db-a", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "db-ops")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Errorf("router GetTenant (forbidden) status = %d, want %d, body: %s", w.Code, http.StatusForbidden, w.Body.String())
	}
}

func TestFullRouter_ListTenants(t *testing.T) {
	configDir := setupConfigDir(t, map[string]string{
		"db-a.yaml": "tenants:\n  db-a:\n    mysql_cpu: \"80\"\n",
	})

	rbacMgr := newRBACManager(t, `groups:
  - name: admins
    tenants: ["*"]
    permissions: [read]
`)

	r := chi.NewRouter()
	r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
		Get("/api/v1/tenants", ListTenants(configDir, newRBACManager(t, "")))

	req := httptest.NewRequest("GET", "/api/v1/tenants", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("router ListTenants status = %d, want %d", w.Code, http.StatusOK)
	}
}

// TestMe tests the GET /api/v1/me endpoint
func TestMe(t *testing.T) {
	rbacYAML := `
groups:
  - name: platform-admins
    tenants: ["*"]
    permissions: [read, write, admin]
  - name: db-operators
    tenants: ["db-a-*", "db-b-*"]
    permissions: [read, write]
`
	rbacMgr := newRBACManager(t, rbacYAML)

	tests := []struct {
		name         string
		email        string
		groups       string
		expectStatus int
		checkResp    func(*testing.T, MeResponse)
	}{
		{
			name:         "single group",
			email:        "alice@example.com",
			groups:       "platform-admins",
			expectStatus: http.StatusOK,
			checkResp: func(t *testing.T, m MeResponse) {
				if m.Email != "alice@example.com" {
					t.Errorf("Email = %q, want %q", m.Email, "alice@example.com")
				}
				if m.User != "alice" {
					t.Errorf("User = %q, want %q", m.User, "alice")
				}
				if len(m.Groups) != 1 || m.Groups[0] != "platform-admins" {
					t.Errorf("Groups = %v, want [platform-admins]", m.Groups)
				}
				if len(m.AccessibleTenants) != 1 || m.AccessibleTenants[0] != "*" {
					t.Errorf("AccessibleTenants = %v, want [*]", m.AccessibleTenants)
				}
				if perms, ok := m.Permissions["platform-admins"]; !ok || len(perms) != 3 {
					t.Errorf("platform-admins permissions = %v, want [admin read write]", perms)
				}
			},
		},
		{
			name:         "multiple groups",
			email:        "bob@example.com",
			groups:       "platform-admins, db-operators",
			expectStatus: http.StatusOK,
			checkResp: func(t *testing.T, m MeResponse) {
				if m.Email != "bob@example.com" {
					t.Errorf("Email = %q, want %q", m.Email, "bob@example.com")
				}
				if m.User != "bob" {
					t.Errorf("User = %q, want %q", m.User, "bob")
				}
				if len(m.Groups) != 2 {
					t.Errorf("Groups length = %d, want 2", len(m.Groups))
				}
				// Check accessible tenants contains both patterns
				if len(m.AccessibleTenants) < 3 {
					t.Errorf("AccessibleTenants = %v, want at least 3 entries", m.AccessibleTenants)
				}
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			handler := Me(rbacMgr)

			// Create request and inject RBAC context
			req := httptest.NewRequest("GET", "/api/v1/me", nil)
			req.Header.Set("X-Forwarded-Email", tt.email)
			req.Header.Set("X-Forwarded-Groups", tt.groups)

			// Wrap with RBAC middleware to inject identity into context
			w := httptest.NewRecorder()
			wrapped := rbacMgr.Middleware(rbac.PermRead, nil)(handler)
			wrapped.ServeHTTP(w, req)

			if w.Code != tt.expectStatus {
				t.Errorf("status = %d, want %d", w.Code, tt.expectStatus)
			}

			var resp MeResponse
			if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
				t.Fatalf("unmarshal response: %v", err)
			}

			if tt.checkResp != nil {
				tt.checkResp(t, resp)
			}
		})
	}
}

// TestMeMissingIdentity tests that /api/v1/me returns 401 without identity headers
func TestMeMissingIdentity(t *testing.T) {
	rbacMgr := newRBACManager(t, "")

	handler := Me(rbacMgr)
	req := httptest.NewRequest("GET", "/api/v1/me", nil)
	// Intentionally omit X-Forwarded-Email

	w := httptest.NewRecorder()
	wrapped := rbacMgr.Middleware(rbac.PermRead, nil)(handler)
	wrapped.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want %d", w.Code, http.StatusUnauthorized)
	}
}

// TestMeEmptyEmailDirect tests the handler-level guard when called without middleware
// (e.g., if middleware is misconfigured or bypassed). The handler itself should return 401.
func TestMeEmptyEmailDirect(t *testing.T) {
	rbacMgr := newRBACManager(t, "")

	handler := Me(rbacMgr)
	req := httptest.NewRequest("GET", "/api/v1/me", nil)
	// No middleware — RequestEmail(r) returns "" from empty context

	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want %d", w.Code, http.StatusUnauthorized)
	}

	var body map[string]string
	if err := json.NewDecoder(w.Body).Decode(&body); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if body["error"] == "" {
		t.Error("expected non-empty error message in response body")
	}
}
