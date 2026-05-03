package views

import (
	"os"
	"path/filepath"
	"testing"
)

const sampleViewsYAML = `views:
  prod-finance:
    label: Production Finance
    description: All production tenants in finance domain
    created_by: admin@example.com
    filters:
      environment: production
      domain: finance
  critical-silent:
    label: Critical + Silent
    created_by: user@example.com
    filters:
      tier: tier-1
      operational_mode: silent
`

// TestParseConfig_Valid tests parsing a valid views YAML document.
func TestParseConfig_Valid(t *testing.T) {
	cfg, err := ParseConfig([]byte(sampleViewsYAML))
	if err != nil {
		t.Fatalf("ParseConfig: %v", err)
	}
	if len(cfg.Views) != 2 {
		t.Fatalf("expected 2 views, got %d", len(cfg.Views))
	}

	v := cfg.Views["prod-finance"]
	if v.Label != "Production Finance" {
		t.Errorf("label = %q, want %q", v.Label, "Production Finance")
	}
	if v.Description != "All production tenants in finance domain" {
		t.Errorf("description = %q", v.Description)
	}
	if v.CreatedBy != "admin@example.com" {
		t.Errorf("created_by = %q", v.CreatedBy)
	}
	if len(v.Filters) != 2 || v.Filters["environment"] != "production" {
		t.Errorf("filters = %v", v.Filters)
	}

	v2 := cfg.Views["critical-silent"]
	if v2.Label != "Critical + Silent" {
		t.Errorf("second view label = %q", v2.Label)
	}
	if v2.Description != "" {
		t.Errorf("second view description should be empty, got %q", v2.Description)
	}
	if len(v2.Filters) != 2 {
		t.Errorf("second view filters = %d, want 2", len(v2.Filters))
	}
}

// TestParseConfig_Empty tests parsing empty YAML (no views).
func TestParseConfig_Empty(t *testing.T) {
	cfg, err := ParseConfig([]byte(""))
	if err != nil {
		t.Fatalf("ParseConfig: %v", err)
	}
	if len(cfg.Views) != 0 {
		t.Errorf("expected 0 views for empty input, got %d", len(cfg.Views))
	}
}

// TestParseConfig_InvalidYAML tests parsing invalid YAML.
func TestParseConfig_InvalidYAML(t *testing.T) {
	_, err := ParseConfig([]byte("{{invalid"))
	if err == nil {
		t.Error("expected error for invalid YAML")
	}
}

// TestMarshalConfig_RoundTrip tests that parsing and marshaling are symmetric.
func TestMarshalConfig_RoundTrip(t *testing.T) {
	original, err := ParseConfig([]byte(sampleViewsYAML))
	if err != nil {
		t.Fatalf("ParseConfig: %v", err)
	}

	data, err := MarshalConfig(original)
	if err != nil {
		t.Fatalf("MarshalConfig: %v", err)
	}

	roundTrip, err := ParseConfig(data)
	if err != nil {
		t.Fatalf("ParseConfig roundtrip: %v", err)
	}

	if len(roundTrip.Views) != len(original.Views) {
		t.Errorf("roundtrip views = %d, want %d", len(roundTrip.Views), len(original.Views))
	}

	for id, v := range original.Views {
		rt, ok := roundTrip.Views[id]
		if !ok {
			t.Errorf("missing view %q after roundtrip", id)
			continue
		}
		if rt.Label != v.Label {
			t.Errorf("view %q label = %q, want %q", id, rt.Label, v.Label)
		}
		if rt.Description != v.Description {
			t.Errorf("view %q description = %q, want %q", id, rt.Description, v.Description)
		}
		if rt.CreatedBy != v.CreatedBy {
			t.Errorf("view %q created_by = %q, want %q", id, rt.CreatedBy, v.CreatedBy)
		}
		if len(rt.Filters) != len(v.Filters) {
			t.Errorf("view %q filters = %d, want %d", id, len(rt.Filters), len(v.Filters))
		}
		for k, f := range v.Filters {
			if rt.Filters[k] != f {
				t.Errorf("view %q filter %q = %q, want %q", id, k, rt.Filters[k], f)
			}
		}
	}
}

// TestValidateViewID tests view ID validation.
func TestValidateViewID(t *testing.T) {
	tests := []struct {
		id      string
		wantErr bool
	}{
		{"prod-finance", false},
		{"critical_silent", false},
		{"view-123", false},
		{"a", false},
		{"view_with_many_chars_and_numbers_12345", false},

		// Invalid cases
		{"", true},                                       // empty
		{"UPPERCASE", true},                             // uppercase
		{"has space", true},                             // space
		{"has.dot", true},                               // dot
		{"has/slash", true},                             // slash
		{"has@at", true},                                // at sign
		{string(make([]byte, 129)), true},               // too long (129 chars)
		{"view-with-CAPS", true},                        // mixed case with uppercase
	}

	for _, tt := range tests {
		name := tt.id
		if name == "" {
			name = "(empty)"
		}
		if len(name) > 20 {
			name = name[:20] + "..."
		}
		t.Run(name, func(t *testing.T) {
			err := ValidateViewID(tt.id)
			if (err != nil) != tt.wantErr {
				t.Errorf("ValidateViewID(%q) error = %v, wantErr = %v", tt.id, err, tt.wantErr)
			}
		})
	}
}

// TestNewManager_NoFile tests Manager creation when no _views.yaml file exists.
func TestNewManager_NoFile(t *testing.T) {
	dir := t.TempDir()
	mgr := NewManager(dir)

	cfg := mgr.Get()
	if len(cfg.Views) != 0 {
		t.Errorf("expected 0 views when no file exists, got %d", len(cfg.Views))
	}

	list := mgr.ListViews()
	if len(list) != 0 {
		t.Errorf("expected empty list, got %d", len(list))
	}
}

// TestNewManager_WithFile tests Manager creation with an existing _views.yaml file.
func TestNewManager_WithFile(t *testing.T) {
	dir := t.TempDir()
	err := os.WriteFile(filepath.Join(dir, "_views.yaml"), []byte(sampleViewsYAML), 0644)
	if err != nil {
		t.Fatalf("write: %v", err)
	}

	mgr := NewManager(dir)

	list := mgr.ListViews()
	if len(list) != 2 {
		t.Fatalf("expected 2 views, got %d", len(list))
	}

	// Verify sorted order
	if list[0].ID != "critical-silent" {
		t.Errorf("first view (sorted) = %q, want %q", list[0].ID, "critical-silent")
	}
	if list[1].ID != "prod-finance" {
		t.Errorf("second view (sorted) = %q, want %q", list[1].ID, "prod-finance")
	}
}

// TestManager_GetView tests retrieving a single view by ID.
func TestManager_GetView(t *testing.T) {
	dir := t.TempDir()
	err := os.WriteFile(filepath.Join(dir, "_views.yaml"), []byte(sampleViewsYAML), 0644)
	if err != nil {
		t.Fatalf("write: %v", err)
	}

	mgr := NewManager(dir)

	t.Run("found", func(t *testing.T) {
		v, ok := mgr.GetView("prod-finance")
		if !ok {
			t.Fatal("expected to find prod-finance view")
		}
		if v.Label != "Production Finance" {
			t.Errorf("label = %q", v.Label)
		}
		if v.CreatedBy != "admin@example.com" {
			t.Errorf("created_by = %q", v.CreatedBy)
		}
	})

	t.Run("not_found", func(t *testing.T) {
		_, ok := mgr.GetView("nonexistent")
		if ok {
			t.Error("expected not to find nonexistent view")
		}
	})
}

// TestManager_ListViews tests listing all views in sorted order.
func TestManager_ListViews(t *testing.T) {
	dir := t.TempDir()

	// Create a config with multiple views in non-alphabetical order
	multiViewYAML := `views:
  zebra-view:
    label: Zebra
    filters:
      env: prod
  alpha-view:
    label: Alpha
    filters:
      env: dev
  mike-view:
    label: Mike
    filters:
      env: staging
`
	err := os.WriteFile(filepath.Join(dir, "_views.yaml"), []byte(multiViewYAML), 0644)
	if err != nil {
		t.Fatalf("write: %v", err)
	}

	mgr := NewManager(dir)
	list := mgr.ListViews()

	if len(list) != 3 {
		t.Fatalf("expected 3 views, got %d", len(list))
	}

	// Verify alphabetical order
	expectedOrder := []string{"alpha-view", "mike-view", "zebra-view"}
	for i, expected := range expectedOrder {
		if list[i].ID != expected {
			t.Errorf("view[%d] ID = %q, want %q", i, list[i].ID, expected)
		}
	}
}

// TestManager_Reload tests reloading the views config after file changes.
func TestManager_Reload(t *testing.T) {
	dir := t.TempDir()

	// Start with initial config
	initialYAML := `views:
  view-1:
    label: View One
    filters:
      env: prod
`
	filePath := filepath.Join(dir, "_views.yaml")
	err := os.WriteFile(filePath, []byte(initialYAML), 0644)
	if err != nil {
		t.Fatalf("write initial: %v", err)
	}

	mgr := NewManager(dir)
	initial := mgr.Get()
	if len(initial.Views) != 1 {
		t.Errorf("initial views = %d, want 1", len(initial.Views))
	}

	// Update the file
	updatedYAML := `views:
  view-1:
    label: View One
    filters:
      env: prod
  view-2:
    label: View Two
    filters:
      env: staging
  view-3:
    label: View Three
    filters:
      env: dev
`
	err = os.WriteFile(filePath, []byte(updatedYAML), 0644)
	if err != nil {
		t.Fatalf("write updated: %v", err)
	}

	// Before reload, should still see old config
	beforeReload := mgr.Get()
	if len(beforeReload.Views) != 1 {
		t.Errorf("before reload views = %d, want 1", len(beforeReload.Views))
	}

	// After reload, should see new config
	if err := mgr.Reload(); err != nil {
		t.Fatalf("Reload: %v", err)
	}
	afterReload := mgr.Get()
	if len(afterReload.Views) != 3 {
		t.Errorf("after reload views = %d, want 3", len(afterReload.Views))
	}

	// Verify new views exist
	if _, ok := afterReload.Views["view-2"]; !ok {
		t.Error("expected to find view-2 after reload")
	}
	if _, ok := afterReload.Views["view-3"]; !ok {
		t.Error("expected to find view-3 after reload")
	}
}

// TestManager_Reload_NoFile tests reloading when the file is deleted.
func TestManager_Reload_NoFile(t *testing.T) {
	dir := t.TempDir()

	// Start with a file
	initialYAML := `views:
  view-1:
    label: View One
    filters:
      env: prod
`
	filePath := filepath.Join(dir, "_views.yaml")
	err := os.WriteFile(filePath, []byte(initialYAML), 0644)
	if err != nil {
		t.Fatalf("write initial: %v", err)
	}

	mgr := NewManager(dir)
	initial := mgr.Get()
	if len(initial.Views) != 1 {
		t.Errorf("initial views = %d, want 1", len(initial.Views))
	}

	// Delete the file
	err = os.Remove(filePath)
	if err != nil {
		t.Fatalf("remove file: %v", err)
	}

	// Reload should handle gracefully (file not found)
	if err := mgr.Reload(); err != nil {
		t.Fatalf("Reload with missing file: %v", err)
	}

	// Should revert to empty config
	afterReload := mgr.Get()
	if len(afterReload.Views) != 0 {
		t.Errorf("after reload with no file, views = %d, want 0", len(afterReload.Views))
	}
}

// TestManager_Reload_HashCaching tests that Reload skips parsing if hash unchanged.
func TestManager_Reload_HashCaching(t *testing.T) {
	dir := t.TempDir()

	yaml := `views:
  view-1:
    label: View One
    filters:
      env: prod
`
	filePath := filepath.Join(dir, "_views.yaml")
	err := os.WriteFile(filePath, []byte(yaml), 0644)
	if err != nil {
		t.Fatalf("write: %v", err)
	}

	mgr := NewManager(dir)
	first := mgr.Get()
	initialHash := mgr.LastHash()

	// Reload without file changes
	if err := mgr.Reload(); err != nil {
		t.Fatalf("Reload: %v", err)
	}
	second := mgr.Get()

	// Should be the same config object (cached)
	if len(second.Views) != len(first.Views) {
		t.Errorf("views changed after reload without file changes")
	}
	if initialHash == "" {
		t.Error("expected hash to be populated after first load")
	}
}
