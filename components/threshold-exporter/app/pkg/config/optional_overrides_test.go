package config

import (
	"os"
	"path/filepath"
	"testing"

	"gopkg.in/yaml.v3"
)

// OptionalOverrides names keys the platform recognises but supplies no value
// for. PR-A wires the FIELD through every merge path without changing any
// behaviour — the list is empty everywhere in the repo today. These tests exist
// because the failure mode of a missed seam is silence: the field would decode
// fine, survive some paths, and vanish on others (a full rebuild would carry it
// while the first incremental reload dropped it), with nothing saying so.
//
// One test per seam. If a seam is removed, exactly one of these goes red and
// names it.

func TestOptionalOverridesDecodesFromYAML(t *testing.T) {
	t.Parallel()
	var cfg ThresholdConfig
	if err := yaml.Unmarshal([]byte(`
defaults:
  mysql_connections: 100
optional_overrides:
  - db2_log_usage_percent
  - oracle_process_count
`), &cfg); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got := len(cfg.OptionalOverrides); got != 2 {
		t.Fatalf("OptionalOverrides = %v, want 2 entries", cfg.OptionalOverrides)
	}
	if cfg.Defaults["mysql_connections"] != 100 {
		t.Fatalf("adding the field disturbed defaults decoding: %v", cfg.Defaults)
	}
}

// A config that predates the field must keep decoding — every shipped
// _defaults.yaml is in this shape.
func TestOptionalOverridesAbsentIsNilNotError(t *testing.T) {
	t.Parallel()
	var cfg ThresholdConfig
	if err := yaml.Unmarshal([]byte("defaults:\n  mysql_connections: 100\n"), &cfg); err != nil {
		t.Fatalf("unmarshal without the field must not error: %v", err)
	}
	if cfg.OptionalOverrides != nil {
		t.Fatalf("want nil slice, got %v", cfg.OptionalOverrides)
	}
	// Pins the reason Load() does NOT initialise this like it does the maps:
	// a nil slice is already usable. If this ever becomes a map, that changes.
	if len(cfg.OptionalOverrides) != 0 {
		t.Fatal("nil slice must have len 0")
	}
	cfg.OptionalOverrides = append(cfg.OptionalOverrides, "x") // must not panic
}

// ⛔ The reason a null inside `defaults:` is not an option: yaml.v3 decodes it
// into map[string]float64 as 0, which would emit a threshold of zero for every
// tenant rather than "no opinion". This pins the claim the design rests on.
func TestNullInsideDefaultsDecodesToZeroNotAbsent(t *testing.T) {
	t.Parallel()
	var cfg ThresholdConfig
	if err := yaml.Unmarshal([]byte("defaults:\n  db2_log_usage_percent:\n"), &cfg); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	v, present := cfg.Defaults["db2_log_usage_percent"]
	if !present || v != 0 {
		t.Fatalf("expected the key to be PRESENT with value 0 (that is why it cannot "+
			"express 'declared without value'); got present=%v value=%v", present, v)
	}
}

// Seam: mergeTenantConfig is the whole of what the tenant-api write path knows
// about the platform surface. Drop the field here and a tenant's PUT is still
// refused, so the runtime slot exists but is unreachable via the only
// supported writer.
func TestMergeTenantConfigCarriesOptionalOverrides(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte(`
defaults:
  mysql_connections: 100
optional_overrides:
  - db2_log_usage_percent
`), 0o600); err != nil {
		t.Fatal(err)
	}
	merged := mergeTenantConfig(dir, ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{"t1": {}},
	})
	if len(merged.OptionalOverrides) != 1 || merged.OptionalOverrides[0] != "db2_log_usage_percent" {
		t.Fatalf("write path lost the platform surface: %v", merged.OptionalOverrides)
	}
	if merged.Defaults["mysql_connections"] != 100 {
		t.Fatalf("defaults regressed: %v", merged.Defaults)
	}
}
