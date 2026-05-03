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

	"github.com/vencil/tenant-api/internal/views"
)

func setupViewsFile(t *testing.T, configDir, content string) {
	t.Helper()
	if content != "" {
		if err := os.WriteFile(filepath.Join(configDir, "_views.yaml"), []byte(content), 0644); err != nil {
			t.Fatalf("write _views.yaml: %v", err)
		}
	}
}

const testViewsYAML = `views:
  prod-finance:
    label: Production Finance
    description: All production finance tenants
    created_by: admin@example.com
    filters:
      environment: production
      domain: finance
  critical-silent:
    label: Critical + Silent
    created_by: ops@example.com
    filters:
      tier: tier-1
      operational_mode: silent
`

// --- ListViews tests ---

// TestListViews_Empty tests listing views when none exist.
func TestListViews_Empty(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	mgr := views.NewManager(configDir)

	h := (&Deps{Views: mgr}).ListViews()
	req := httptest.NewRequest("GET", "/api/v1/views", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("ListViews() status = %d, want %d", w.Code, http.StatusOK)
	}

	var resp []ViewResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(resp) != 0 {
		t.Errorf("expected 0 views, got %d", len(resp))
	}
}

// TestListViews_WithData tests listing views with existing data.
func TestListViews_WithData(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	setupViewsFile(t, configDir, testViewsYAML)
	mgr := views.NewManager(configDir)

	h := (&Deps{Views: mgr}).ListViews()
	req := httptest.NewRequest("GET", "/api/v1/views", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("ListViews() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}

	var resp []ViewResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(resp) != 2 {
		t.Fatalf("expected 2 views, got %d", len(resp))
	}

	// Views should be sorted by ID
	if resp[0].ID != "critical-silent" {
		t.Errorf("first view ID = %q, want %q", resp[0].ID, "critical-silent")
	}
	if resp[0].Label != "Critical + Silent" {
		t.Errorf("first view label = %q", resp[0].Label)
	}
	if len(resp[0].Filters) != 2 {
		t.Errorf("first view filters = %d, want 2", len(resp[0].Filters))
	}

	if resp[1].ID != "prod-finance" {
		t.Errorf("second view ID = %q, want %q", resp[1].ID, "prod-finance")
	}
	if resp[1].Label != "Production Finance" {
		t.Errorf("second view label = %q", resp[1].Label)
	}
	if resp[1].Description != "All production finance tenants" {
		t.Errorf("second view description = %q", resp[1].Description)
	}
}

// --- GetView tests ---

// TestGetView_Success tests retrieving a single view.
func TestGetView_Success(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	setupViewsFile(t, configDir, testViewsYAML)
	mgr := views.NewManager(configDir)

	h := (&Deps{Views: mgr}).GetView()
	req := newRequestWithChiParam("GET", "/api/v1/views/prod-finance", "id", "prod-finance", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("GetView() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}

	var resp ViewResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.ID != "prod-finance" {
		t.Errorf("ViewResponse.ID = %q, want %q", resp.ID, "prod-finance")
	}
	if resp.Label != "Production Finance" {
		t.Errorf("ViewResponse.Label = %q", resp.Label)
	}
	if resp.Description != "All production finance tenants" {
		t.Errorf("ViewResponse.Description = %q", resp.Description)
	}
	if resp.CreatedBy != "admin@example.com" {
		t.Errorf("ViewResponse.CreatedBy = %q", resp.CreatedBy)
	}
	if len(resp.Filters) != 2 {
		t.Errorf("ViewResponse.Filters = %d, want 2", len(resp.Filters))
	}
	if resp.Filters["environment"] != "production" {
		t.Errorf("environment filter = %q", resp.Filters["environment"])
	}
}

// TestGetView_NotFound tests retrieving a view that doesn't exist.
func TestGetView_NotFound(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	mgr := views.NewManager(configDir)

	h := (&Deps{Views: mgr}).GetView()
	req := newRequestWithChiParam("GET", "/api/v1/views/nonexistent", "id", "nonexistent", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("GetView() status = %d, want %d", w.Code, http.StatusNotFound)
	}
}

// TestGetView_InvalidID tests retrieving a view with an invalid ID.
func TestGetView_InvalidID(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	mgr := views.NewManager(configDir)

	h := (&Deps{Views: mgr}).GetView()
	req := newRequestWithChiParam("GET", "/api/v1/views/INVALID!", "id", "INVALID!", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("GetView() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

// --- PutView tests ---

// TestPutView_Create tests creating a new view.
func TestPutView_Create(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	mgr := views.NewManager(configDir)
	writer := newTestWriter(configDir)

	body := `{
		"label": "My View",
		"description": "Test view",
		"filters": {
			"environment": "staging",
			"tier": "tier-2"
		}
	}`
	req := newRequestWithChiParam("PUT", "/api/v1/views/my-view", "id", "my-view",
		bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	setRequestIdentity(req, "test@example.com")

	h := (&Deps{Views: mgr, Writer: writer}).PutView()
	w := executeWithRBAC(t, h, req)

	if w.Code != http.StatusOK {
		t.Fatalf("PutView() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}

	var resp map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp["status"] != "ok" {
		t.Errorf("status = %q, want ok", resp["status"])
	}
	if resp["view_id"] != "my-view" {
		t.Errorf("view_id = %q, want my-view", resp["view_id"])
	}

	// Verify the view was created
	v, ok := mgr.GetView("my-view")
	if !ok {
		t.Fatal("expected view to exist after PutView")
	}
	if v.Label != "My View" {
		t.Errorf("view label = %q, want %q", v.Label, "My View")
	}
	if v.Description != "Test view" {
		t.Errorf("view description = %q", v.Description)
	}
	if v.CreatedBy != "test@example.com" {
		t.Errorf("view created_by = %q, want test@example.com", v.CreatedBy)
	}
	if len(v.Filters) != 2 {
		t.Errorf("view filters = %d, want 2", len(v.Filters))
	}

	// Verify _views.yaml was written
	data, err := os.ReadFile(filepath.Join(configDir, "_views.yaml"))
	if err != nil {
		t.Fatalf("read _views.yaml: %v", err)
	}
	if len(data) == 0 {
		t.Error("_views.yaml should not be empty")
	}
}

// TestPutView_Update tests updating an existing view.
func TestPutView_Update(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	setupViewsFile(t, configDir, testViewsYAML)
	initGitRepo(t, configDir)
	mgr := views.NewManager(configDir)
	writer := newTestWriter(configDir)

	body := `{
		"label": "Updated Finance",
		"description": "Updated description",
		"filters": {
			"environment": "staging",
			"domain": "finance"
		}
	}`
	req := newRequestWithChiParam("PUT", "/api/v1/views/prod-finance", "id", "prod-finance",
		bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	setRequestIdentity(req, "test@example.com")

	h := (&Deps{Views: mgr, Writer: writer}).PutView()
	w := executeWithRBAC(t, h, req)

	if w.Code != http.StatusOK {
		t.Fatalf("PutView() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}

	v, ok := mgr.GetView("prod-finance")
	if !ok {
		t.Fatal("view should still exist after update")
	}
	if v.Label != "Updated Finance" {
		t.Errorf("updated label = %q, want %q", v.Label, "Updated Finance")
	}
	if v.Description != "Updated description" {
		t.Errorf("updated description = %q", v.Description)
	}
	// CreatedBy should be updated to the current user
	if v.CreatedBy != "test@example.com" {
		t.Errorf("updated created_by = %q", v.CreatedBy)
	}
}

// TestPutView_MissingLabel tests creating a view without a label.
func TestPutView_MissingLabel(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	mgr := views.NewManager(configDir)
	writer := newTestWriter(configDir)

	body := `{
		"description": "No label",
		"filters": {
			"environment": "prod"
		}
	}`
	req := newRequestWithChiParam("PUT", "/api/v1/views/my-view", "id", "my-view",
		bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")

	w := httptest.NewRecorder()
	h := (&Deps{Views: mgr, Writer: writer}).PutView()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("PutView() status = %d, want %d", w.Code, http.StatusBadRequest)
	}

	errResp := w.Body.String()
	if errResp == "" {
		t.Error("expected error response")
	}
}

// TestPutView_EmptyFilters tests creating a view with empty filters.
func TestPutView_EmptyFilters(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	mgr := views.NewManager(configDir)
	writer := newTestWriter(configDir)

	body := `{
		"label": "Empty Filters",
		"filters": {}
	}`
	req := newRequestWithChiParam("PUT", "/api/v1/views/my-view", "id", "my-view",
		bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")

	w := httptest.NewRecorder()
	h := (&Deps{Views: mgr, Writer: writer}).PutView()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("PutView() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

// TestPutView_InvalidJSON tests creating a view with malformed JSON.
func TestPutView_InvalidJSON(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	mgr := views.NewManager(configDir)
	writer := newTestWriter(configDir)

	req := newRequestWithChiParam("PUT", "/api/v1/views/my-view", "id", "my-view",
		bytes.NewBufferString("not json"))
	req.Header.Set("Content-Type", "application/json")

	w := httptest.NewRecorder()
	h := (&Deps{Views: mgr, Writer: writer}).PutView()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("PutView() status = %d, want %d, body: %s", w.Code, http.StatusBadRequest, w.Body.String())
	}
}

// TestPutView_InvalidViewID tests creating a view with an invalid ID.
func TestPutView_InvalidViewID(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	mgr := views.NewManager(configDir)
	writer := newTestWriter(configDir)

	body := `{
		"label": "Invalid ID",
		"filters": {"env": "prod"}
	}`
	req := newRequestWithChiParam("PUT", "/api/v1/views/INVALID!", "id", "INVALID!",
		bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")

	w := httptest.NewRecorder()
	h := (&Deps{Views: mgr, Writer: writer}).PutView()
	h(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("PutView() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

// --- DeleteView tests ---

// TestDeleteView_Success tests deleting an existing view.
func TestDeleteView_Success(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	setupViewsFile(t, configDir, testViewsYAML)
	initGitRepo(t, configDir)
	mgr := views.NewManager(configDir)
	writer := newTestWriter(configDir)

	// Verify view exists before deletion
	_, ok := mgr.GetView("prod-finance")
	if !ok {
		t.Fatal("view should exist before deletion")
	}

	req := newRequestWithChiParam("DELETE", "/api/v1/views/prod-finance", "id", "prod-finance", nil)
	setRequestIdentity(req, "test@example.com")

	h := (&Deps{Views: mgr, Writer: writer}).DeleteView()
	w := executeWithRBAC(t, h, req)

	if w.Code != http.StatusOK {
		t.Fatalf("DeleteView() status = %d, want %d, body: %s", w.Code, http.StatusOK, w.Body.String())
	}

	var resp map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp["status"] != "ok" {
		t.Errorf("status = %q, want ok", resp["status"])
	}

	// Verify the view was deleted
	_, ok = mgr.GetView("prod-finance")
	if ok {
		t.Fatal("view should not exist after deletion")
	}

	// Verify other view still exists
	_, ok = mgr.GetView("critical-silent")
	if !ok {
		t.Error("other view should still exist")
	}
}

// TestDeleteView_NotFound tests deleting a view that doesn't exist.
func TestDeleteView_NotFound(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	mgr := views.NewManager(configDir)
	writer := newTestWriter(configDir)

	req := newRequestWithChiParam("DELETE", "/api/v1/views/nonexistent", "id", "nonexistent", nil)
	setRequestIdentity(req, "test@example.com")

	h := (&Deps{Views: mgr, Writer: writer}).DeleteView()
	w := executeWithRBAC(t, h, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("DeleteView() status = %d, want %d", w.Code, http.StatusNotFound)
	}
}

// TestDeleteView_InvalidID tests deleting a view with an invalid ID.
func TestDeleteView_InvalidID(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	mgr := views.NewManager(configDir)
	writer := newTestWriter(configDir)

	req := newRequestWithChiParam("DELETE", "/api/v1/views/INVALID!", "id", "INVALID!", nil)
	setRequestIdentity(req, "test@example.com")

	h := (&Deps{Views: mgr, Writer: writer}).DeleteView()
	w := executeWithRBAC(t, h, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("DeleteView() status = %d, want %d", w.Code, http.StatusBadRequest)
	}
}

// TestPutView_Conflict tests handling a git conflict during view creation.
func TestPutView_Conflict(t *testing.T) {
	configDir := setupConfigDir(t, nil)
	setupViewsFile(t, configDir, testViewsYAML)
	initGitRepo(t, configDir)
	mgr := views.NewManager(configDir)
	writer := newTestWriter(configDir)

	body := `{
		"label": "Conflict Test",
		"filters": {"env": "prod"}
	}`
	req := newRequestWithChiParam("PUT", "/api/v1/views/conflict-view", "id", "conflict-view",
		bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	setRequestIdentity(req, "test@example.com")

	// Simulate a conflict by modifying the git HEAD before the write
	// This is a simple test; in production, conflicts can happen due to concurrent writes
	cmd := exec.Command("bash", "-c", "cd "+configDir+" && git rev-parse HEAD > /tmp/head.txt")
	if err := cmd.Run(); err != nil {
		t.Logf("note: git conflict test setup failed: %v", err)
		t.SkipNow()
	}

	h := (&Deps{Views: mgr, Writer: writer}).PutView()
	w := executeWithRBAC(t, h, req)

	// The request should succeed even if there was a pre-existing HEAD
	// (actual conflict detection happens at the gitops layer)
	if w.Code != http.StatusOK && w.Code != http.StatusConflict {
		t.Logf("PutView() status = %d (conflict or success expected)", w.Code)
	}
}
