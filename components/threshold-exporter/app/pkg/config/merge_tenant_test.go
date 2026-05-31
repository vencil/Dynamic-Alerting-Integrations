package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestMergeTenantWithRootDefaults_PopulatesDefaults verifies the consolidation
// behind the GET / validate / write-boundary parity (ADR-024 PR4 / #704): a
// tenant-only body merged against a root _defaults.yaml yields a config whose
// Defaults are populated, so ValidateTenantKeys recognises plain metric keys
// instead of flagging them "unknown key not in defaults".
func TestMergeTenantWithRootDefaults_PopulatesDefaults(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"),
		[]byte("defaults:\n  container_cpu: 80\n  mysql_cpu: 80\n"), 0o644); err != nil {
		t.Fatalf("write defaults: %v", err)
	}

	body := []byte("tenants:\n  db-a:\n    container_cpu: \"70\"\n")
	merged := MergeTenantWithRootDefaults(dir, "db-a", body)

	if merged.Defaults["container_cpu"] != 80 {
		t.Errorf("expected container_cpu default 80, got %v", merged.Defaults["container_cpu"])
	}
	if _, ok := merged.Tenants["db-a"]["container_cpu"]; !ok {
		t.Error("tenant override container_cpu should be present in merged.Tenants")
	}
	if warnings := merged.ValidateTenantKeys(); len(warnings) != 0 {
		t.Errorf("tenant-only metric body should validate clean against merged defaults, got: %v", warnings)
	}
}

// TestMergeTenantWithRootDefaults_NoDefaultsFile confirms a missing
// _defaults.yaml is tolerated (empty Defaults), and that ValidateTenantKeys
// then still flags an ordinary metric key — i.e. the merge does not fabricate
// defaults, it only surfaces ones that genuinely exist on disk.
func TestMergeTenantWithRootDefaults_NoDefaultsFile(t *testing.T) {
	t.Parallel()
	dir := t.TempDir() // no _defaults.yaml

	body := []byte("tenants:\n  db-a:\n    container_cpu: \"70\"\n")
	merged := MergeTenantWithRootDefaults(dir, "db-a", body)

	if len(merged.Defaults) != 0 {
		t.Errorf("expected empty Defaults without a _defaults.yaml, got: %v", merged.Defaults)
	}
	if warnings := merged.ValidateTenantKeys(); len(warnings) == 0 {
		t.Error("an unmatched metric key should warn when no defaults are present")
	}
}

// TestCheckTenantRootKeys covers the root-key contract (#705): a tenant body may
// carry ONLY a top-level `tenants` block (tenant-config.schema.json
// additionalProperties:false). Shared by the PUT write boundary + POST /validate.
func TestCheckTenantRootKeys(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name    string
		yaml    string
		wantBad bool
		wantSub string // substring expected in the warning when wantBad
	}{
		{"tenants only", "tenants:\n  db-a:\n    container_cpu: \"70\"\n", false, ""},
		{"stray defaults", "defaults:\n  container_cpu: 80\ntenants:\n  db-a:\n    container_cpu: \"70\"\n", true, "defaults"},
		{"stray state_filters", "state_filters:\n  x: {}\ntenants:\n  db-a: {}\n", true, "state_filters"},
		{"stray profiles", "profiles:\n  p: {}\ntenants:\n  db-a: {}\n", true, "profiles"},
		{"typo tenant", "tenant:\n  db-a:\n    container_cpu: \"70\"\n", true, "tenant"},
		{"scalar doc (not a map)", "just-a-string\n", false, ""}, // YAML-validity is the caller's gate
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			got := CheckTenantRootKeys([]byte(tt.yaml))
			if tt.wantBad {
				if len(got) == 0 {
					t.Fatalf("CheckTenantRootKeys(%q) = no warning, want a root-key violation", tt.yaml)
				}
				if !strings.Contains(got[0], tt.wantSub) {
					t.Errorf("warning %q should name %q", got[0], tt.wantSub)
				}
				return
			}
			if len(got) != 0 {
				t.Errorf("CheckTenantRootKeys(%q) = %v, want no warning", tt.yaml, got)
			}
		})
	}
}
