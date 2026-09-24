package config

// merge_tenant_carrier_test.go — the tenant-api read/write merge reads the
// ROOT carrier the exporter's chain selects (#1674), not a hard-coded
// `_defaults.yaml`. Before, a root holding only `_defaults.yml` or
// `_DEFAULTS.YAML` (both served by the exporter) merged to Defaults=map[], so
// the write gate refused valid keys as unknown (blind review of #1674).
//
// Seams: none — t.TempDir() trees.

import (
	"os"
	"path/filepath"
	"testing"
)

func TestMergeTenantReadsTheSelectedRootCarrier(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name  string
		files map[string]string
		want  float64 // cpu_pct in merged Defaults
	}{
		{"only lower-case .yml", map[string]string{"_defaults.yml": "defaults:\n  cpu_pct: 70\n"}, 70},
		{"only upper-case .YAML", map[string]string{"_DEFAULTS.YAML": "defaults:\n  cpu_pct: 60\n"}, 60},
		{".yaml beside .yml", map[string]string{
			"_defaults.yaml": "defaults:\n  cpu_pct: 50\n",
			"_defaults.yml":  "defaults:\n  cpu_pct: 90\n",
		}, 50},
	}
	body := []byte("tenants:\n  t-m:\n    cpu_pct: \"40\"\n")
	for _, tc := range cases {
		dir := t.TempDir()
		for name, content := range tc.files {
			if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
				t.Fatal(err)
			}
		}
		merged := MergeTenantWithRootDefaults(dir, "t-m", body)
		if got := merged.Defaults["cpu_pct"]; got != tc.want {
			t.Errorf("%s: Defaults[cpu_pct] = %v, want %v", tc.name, got, tc.want)
		}
		if errs := merged.ValidateTenantKeys().Errors; len(errs) != 0 {
			t.Errorf("%s: the write gate refuses a key the platform declares: %v", tc.name, errs)
		}
	}
}
