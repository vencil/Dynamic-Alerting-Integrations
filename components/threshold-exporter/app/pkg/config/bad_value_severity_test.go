package config

// #2377: an override whose value half does not parse falls through to State 2
// (platform default). The `:severity` suffix belongs to that discarded value,
// so it must not survive onto the default row — "7O:critical" has to resolve
// exactly like the bare "7O" (default/warning). Keeping it produced mixed
// rows (80/critical, 80/foo) and, next to `<metric>_critical`, two rows with
// the same identity, which makes the whole /metrics scrape fail.

import (
	"fmt"
	"path/filepath"
	"sort"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

func TestResolveBaseRows_BadValueDropsSeveritySuffix(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name string
		body string
		want []string // "value/severity", sorted
	}{
		{"control_valid_suffix", `    mysql_connections: "70:critical"`, []string{"70/critical"}},
		{"control_bare_bad_value", `    mysql_connections: "7O"`, []string{"80/warning"}},
		{"bad_value_critical_suffix", `    mysql_connections: "7O:critical"`, []string{"80/warning"}},
		{"bad_value_arbitrary_suffix", `    mysql_connections: "abc:foo"`, []string{"80/warning"}},
		{"bad_value_with_critical_tier", `    mysql_connections: "7O:critical"
    mysql_connections_critical: "95"`, []string{"80/warning", "95/critical"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tmp := t.TempDir()
			testutil.WriteTree(t, tmp, map[string]string{
				"conf.d/_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
				"conf.d/tx.yaml":        "tenants:\n  tx:\n" + tc.body + "\n",
			})
			cfg, _, err := LoadDir(filepath.Join(tmp, "conf.d"), nil)
			if err != nil {
				t.Fatal(err)
			}
			var got []string
			for _, r := range cfg.Resolve() {
				if r.Tenant == "tx" && r.Component == "mysql" && r.Metric == "connections" {
					got = append(got, fmt.Sprintf("%g/%s", r.Value, r.Severity))
				}
			}
			sort.Strings(got)
			if fmt.Sprint(got) != fmt.Sprint(tc.want) {
				t.Errorf("rows(value/severity) = %v, want %v", got, tc.want)
			}
		})
	}
}
