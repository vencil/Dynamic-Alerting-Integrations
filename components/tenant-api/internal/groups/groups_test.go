package groups

import (
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
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
	t.Parallel()
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
	t.Parallel()
	cfg, err := ParseConfig([]byte(""))
	if err != nil {
		t.Fatalf("ParseConfig: %v", err)
	}
	if len(cfg.Groups) != 0 {
		t.Errorf("expected 0 groups for empty input, got %d", len(cfg.Groups))
	}
}

func TestParseConfig_InvalidYAML(t *testing.T) {
	t.Parallel()
	_, err := ParseConfig([]byte("{{invalid"))
	if err == nil {
		t.Error("expected error for invalid YAML")
	}
}

func TestMarshalConfig_RoundTrip(t *testing.T) {
	t.Parallel()
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
	t.Parallel()
	tests := []struct {
		id      string
		wantErr bool
	}{
		{"production-dba", false},
		{"staging_all", false},
		{"group-123", false},
		{"a", false},
		{"", true},                        // empty
		{"UPPERCASE", true},               // uppercase
		{"has space", true},               // space
		{"has.dot", true},                 // dot
		{"has/slash", true},               // slash
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
	t.Parallel()
	dir := t.TempDir()
	mgr := NewDirManager(dir)

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
	t.Parallel()
	dir, _ := testutil.MkTempYAML(t, "_groups.yaml", sampleGroupsYAML)

	mgr := NewDirManager(dir)

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
	t.Parallel()
	dir, _ := testutil.MkTempYAML(t, "_groups.yaml", sampleGroupsYAML)

	mgr := NewDirManager(dir)

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
	t.Parallel()
	dir, _ := testutil.MkTempYAML(t, "_groups.yaml", sampleGroupsYAML)

	mgr := NewDirManager(dir)
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
	testutil.WriteYAML(t, dir, "_groups.yaml", updatedYAML)

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

// parseTreeRefused are groups records yaml.v3 alone reads (the first document;
// a `!!binary` or `!` value as a string) and parse-tree refuses (ADR-036 §7
// step 7, #3206): ParseConfig errors on each, and a reload onto one keeps the
// last good snapshot — never the empty config, never the first document.
var parseTreeRefused = []struct{ name, yaml, kind string }{
	{"second document", "groups:\n  g1:\n    label: one\n---\ngroups:\n  g2:\n    label: two\n", "multiple_documents"},
	{"trailing empty document", "groups:\n  g1:\n    label: one\n---\n", "multiple_documents"},
	{"!!binary value", "groups:\n  g1:\n    label: !!binary b25l\n", "tag_not_allowed"},
	{"non-specific tag", "groups:\n  g1:\n    label: ! \"one\"\n", "non_specific_tag"},
}

func TestParseConfig_ParseTreeRefusal(t *testing.T) {
	t.Parallel()
	for _, c := range parseTreeRefused {
		if cfg, err := ParseConfig([]byte(c.yaml)); err == nil || !strings.Contains(err.Error(), "kind "+c.kind) {
			t.Errorf("%s: ParseConfig = %v, %v; want an error of kind %s", c.name, cfg, err, c.kind)
		}
	}
}

func TestManager_Reload_ParseTreeRefusalKeepsLastGood(t *testing.T) {
	t.Parallel()
	for _, c := range parseTreeRefused {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir, _ := testutil.MkTempYAML(t, "_groups.yaml", "groups:\n  g0:\n    label: zero\n")
			mgr := NewDirManager(dir)
			if _, ok := mgr.GetGroup("g0"); !ok {
				t.Fatal("precondition: g0 loaded")
			}
			testutil.WriteYAML(t, dir, "_groups.yaml", c.yaml)
			if err := mgr.Reload(); err == nil || !strings.Contains(err.Error(), "kind "+c.kind) {
				t.Fatalf("Reload = %v, want an error of kind %s", err, c.kind)
			}
			if _, ok := mgr.GetGroup("g0"); !ok {
				t.Error("after the refused reload g0 is gone; want the last good snapshot")
			}
			if _, ok := mgr.GetGroup("g1"); ok {
				t.Error("after the refused reload g1 of the refused record is visible")
			}
		})
	}
}

// A record parse-tree refuses at startup is the initial-load failure it
// always was for this manager: logged, and the manager starts from the empty
// config — not from the refused record's first document.
func TestNewManager_ParseTreeRefusalStartsEmpty(t *testing.T) {
	t.Parallel()
	for _, c := range parseTreeRefused {
		dir, _ := testutil.MkTempYAML(t, "_groups.yaml", c.yaml)
		if _, ok := NewDirManager(dir).GetGroup("g1"); ok {
			t.Errorf("%s: g1 of the refused record was loaded", c.name)
		}
	}
}
