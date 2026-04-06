package groups

import (
	"os"
	"path/filepath"
	"testing"
)

const sampleGroupsYAML = `groups:
  production-dba:
    label: Production DBA
    description: All production DB tenants
    filters:
      environment: production
    members:
      - db-a
      - db-b
  staging-all:
    label: All Staging
    members:
      - staging-pg-01
`

func TestParseConfig_Valid(t *testing.T) {
	cfg, err := ParseConfig([]byte(sampleGroupsYAML))
	if err != nil {
		t.Fatalf("ParseConfig: %v", err)
	}
	if len(cfg.Groups) != 2 {
		t.Fatalf("expected 2 groups, got %d", len(cfg.Groups))
	}

	g := cfg.Groups["production-dba"]
	if g.Label != "Production DBA" {
		t.Errorf("label = %q, want %q", g.Label, "Production DBA")
	}
	if g.Description != "All production DB tenants" {
		t.Errorf("description = %q", g.Description)
	}
	if len(g.Filters) != 1 || g.Filters["environment"] != "production" {
		t.Errorf("filters = %v", g.Filters)
	}
	if len(g.Members) != 2 {
		t.Errorf("members = %d, want 2", len(g.Members))
	}
}

func TestParseConfig_Empty(t *testing.T) {
	cfg, err := ParseConfig([]byte(""))
	if err != nil {
		t.Fatalf("ParseConfig: %v", err)
	}
	if len(cfg.Groups) != 0 {
		t.Errorf("expected 0 groups for empty input, got %d", len(cfg.Groups))
	}
}

func TestParseConfig_InvalidYAML(t *testing.T) {
	_, err := ParseConfig([]byte("{{invalid"))
	if err == nil {
		t.Error("expected error for invalid YAML")
	}
}

func TestMarshalConfig_RoundTrip(t *testing.T) {
	original, err := ParseConfig([]byte(sampleGroupsYAML))
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

	if len(roundTrip.Groups) != len(original.Groups) {
		t.Errorf("roundtrip groups = %d, want %d", len(roundTrip.Groups), len(original.Groups))
	}

	for id, g := range original.Groups {
		rt, ok := roundTrip.Groups[id]
		if !ok {
			t.Errorf("missing group %q after roundtrip", id)
			continue
		}
		if rt.Label != g.Label {
			t.Errorf("group %q label = %q, want %q", id, rt.Label, g.Label)
		}
		if len(rt.Members) != len(g.Members) {
			t.Errorf("group %q members = %d, want %d", id, len(rt.Members), len(g.Members))
		}
	}
}

func TestValidateGroupID(t *testing.T) {
	tests := []struct {
		id      string
		wantErr bool
	}{
		{"production-dba", false},
		{"staging_all", false},
		{"group-123", false},
		{"a", false},
		{"", true},            // empty
		{"UPPERCASE", true},   // uppercase
		{"has space", true},   // space
		{"has.dot", true},     // dot
		{"has/slash", true},   // slash
		{string(make([]byte, 129)), true}, // too long
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
			err := ValidateGroupID(tt.id)
			if (err != nil) != tt.wantErr {
				t.Errorf("ValidateGroupID(%q) error = %v, wantErr = %v", tt.id, err, tt.wantErr)
			}
		})
	}
}

func TestNewManager_NoFile(t *testing.T) {
	dir := t.TempDir()
	mgr := NewManager(dir)

	cfg := mgr.Get()
	if len(cfg.Groups) != 0 {
		t.Errorf("expected 0 groups when no file exists, got %d", len(cfg.Groups))
	}

	list := mgr.ListGroups()
	if len(list) != 0 {
		t.Errorf("expected empty list, got %d", len(list))
	}
}

func TestNewManager_WithFile(t *testing.T) {
	dir := t.TempDir()
	err := os.WriteFile(filepath.Join(dir, "_groups.yaml"), []byte(sampleGroupsYAML), 0644)
	if err != nil {
		t.Fatalf("write: %v", err)
	}

	mgr := NewManager(dir)

	list := mgr.ListGroups()
	if len(list) != 2 {
		t.Fatalf("expected 2 groups, got %d", len(list))
	}

	// Verify sorted order
	if list[0].ID != "production-dba" {
		t.Errorf("first group = %q, want %q", list[0].ID, "production-dba")
	}
	if list[1].ID != "staging-all" {
		t.Errorf("second group = %q, want %q", list[1].ID, "staging-all")
	}
}

func TestManager_GetGroup(t *testing.T) {
	dir := t.TempDir()
	err := os.WriteFile(filepath.Join(dir, "_groups.yaml"), []byte(sampleGroupsYAML), 0644)
	if err != nil {
		t.Fatalf("write: %v", err)
	}

	mgr := NewManager(dir)

	g, ok := mgr.GetGroup("production-dba")
	if !ok {
		t.Fatal("expected to find production-dba")
	}
	if g.Label != "Production DBA" {
		t.Errorf("label = %q", g.Label)
	}

	_, ok = mgr.GetGroup("nonexistent")
	if ok {
		t.Error("expected not to find nonexistent group")
	}
}

func TestManager_Reload(t *testing.T) {
	dir := t.TempDir()
	err := os.WriteFile(filepath.Join(dir, "_groups.yaml"), []byte(sampleGroupsYAML), 0644)
	if err != nil {
		t.Fatalf("write: %v", err)
	}

	mgr := NewManager(dir)
	if len(mgr.ListGroups()) != 2 {
		t.Fatal("expected 2 groups initially")
	}

	// Update the file
	updatedYAML := `groups:
  new-group:
    label: New Group
    members:
      - tenant-1
`
	err = os.WriteFile(filepath.Join(dir, "_groups.yaml"), []byte(updatedYAML), 0644)
	if err != nil {
		t.Fatalf("write updated: %v", err)
	}

	if err := mgr.Reload(); err != nil {
		t.Fatalf("Reload: %v", err)
	}

	list := mgr.ListGroups()
	if len(list) != 1 {
		t.Fatalf("expected 1 group after reload, got %d", len(list))
	}
	if list[0].ID != "new-group" {
		t.Errorf("group ID = %q, want %q", list[0].ID, "new-group")
	}
}
