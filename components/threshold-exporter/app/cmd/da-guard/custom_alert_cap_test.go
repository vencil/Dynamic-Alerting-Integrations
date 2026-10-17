package main

import (
	"fmt"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// recipes is n threshold recipes, each of its own shape, as a YAML list
// indented under a `_custom_alerts:` key at the given depth.
func recipes(n int, indent string) string {
	var b strings.Builder
	for i := 0; i < n; i++ {
		fmt.Fprintf(&b, "%s- {recipe: threshold, name: r%d, metric: m%d, op: \">\", window: 5m, threshold: \"10:warning\"}\n", indent, i, i)
	}
	return b.String()
}

// #3042: a tenant's own `_custom_alerts` serving more rows than the cap is
// an error, wherever the tenant owns it (its file, a root platform file's
// `tenants:`); a profile's recipes are not counted.
func TestGuard_CustomAlertOverCap(t *testing.T) {
	t.Parallel()
	over := config.MaxCustomRecipesDefault + 1
	for name, tc := range map[string]struct {
		files map[string]string
		want  bool
	}{
		"tenant file, over": {map[string]string{
			"tx.yaml": "tenants:\n  tx:\n    _custom_alerts:\n" + recipes(over, "      "),
		}, true},
		"tenant file, at the cap": {map[string]string{
			"tx.yaml": "tenants:\n  tx:\n    _custom_alerts:\n" + recipes(config.MaxCustomRecipesDefault, "      "),
		}, false},
		"root platform file, over": {map[string]string{
			"_platform.yaml": "tenants:\n  tx:\n    _custom_alerts:\n" + recipes(over, "      "),
			"tx.yaml":        "tenants:\n  tx:\n    pg_connections: 50\n",
		}, true},
		"profile, over": {map[string]string{
			"_profiles.yaml": "profiles:\n  p1:\n    _custom_alerts:\n" + recipes(over, "      "),
			"tx.yaml":        "tenants:\n  tx:\n    _profile: p1\n",
		}, false},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{"_defaults.yaml": spellingRoot}
			for k, v := range tc.files {
				files[k] = v
			}
			code, fs := guardFindingsOf(t, files)
			var got []string
			for _, f := range fs {
				if f.Kind == findingkind.FindingCustomAlertOverCap.String() && f.Severity == guard.SeverityError {
					got = append(got, f.TenantID+" "+f.Field)
				}
			}
			if want := map[bool]string{true: "tx _custom_alerts", false: ""}[tc.want]; strings.Join(got, ",") != want {
				t.Errorf("custom_alert_over_cap on %v, want %q (all %+v)", got, want, fs)
			}
			if want := map[bool]int{true: exitFindings, false: exitOK}[tc.want]; code != want {
				t.Errorf("exit = %d, want %d", code, want)
			}
		})
	}
}
