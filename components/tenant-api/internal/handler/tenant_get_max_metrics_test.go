package handler

// #2369: GET /api/v1/tenants/{id}'s resolved_thresholds cut at the ROOT
// `_defaults.yaml`'s max_metrics_per_tenant — the cap /metrics cuts at —
// not at the built-in 500. The row counts below are what the exporter's
// directory mode serves for the same trees (the config package pins the
// row-for-row parity against LoadDir).

import (
	"fmt"
	"strings"
	"testing"
)

func maxMetricsTree(capLine string, n int) map[string]string {
	var d strings.Builder
	if capLine != "" {
		d.WriteString(capLine + "\n")
	}
	d.WriteString("defaults:\n")
	for i := 1; i <= n; i++ {
		fmt.Fprintf(&d, "  m%d_x: 1\n", i)
	}
	return map[string]string{
		"_defaults.yaml": d.String(),
		"tx.yaml":        "tenants:\n  tx:\n    m1_x: \"2\"\n",
	}
}

func TestGetTenantHonoursRootMaxMetricsPerTenant(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name     string
		capLine  string
		keys     int
		wantRows int
	}{
		{"cap below key count", "max_metrics_per_tenant: 2", 4, 2},
		{"cap above built-in 500", "max_metrics_per_tenant: 1000", 510, 510},
		{"control: no cap", "", 4, 4},
		{"control: no cap, above 500", "", 510, 500},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			configDir := setupConfigDir(t, maxMetricsTree(tc.capLine, tc.keys))
			detail := getTenantDetail(t, &Deps{ConfigDir: configDir}, "tx")
			if got := len(detail.Resolved); got != tc.wantRows {
				t.Errorf("resolved_thresholds has %d rows, want %d (the cap /metrics applies)", got, tc.wantRows)
			}
			// The tenant's own override sorts inside every cut here, so the
			// count is not met by dropping it.
			if v, ok := resolvedValue(detail, "m1", "x", "warning"); !ok || v != 2 {
				t.Errorf("m1_x = %v (present %v), want the tenant's 2", v, ok)
			}
			if len(detail.Warnings) != 0 {
				t.Errorf("validation_warnings = %q: the cap is not a write-gate error", detail.Warnings)
			}
		})
	}
}
