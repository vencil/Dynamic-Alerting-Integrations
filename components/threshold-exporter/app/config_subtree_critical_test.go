package main

// #3378: a subtree `<base>_critical` whose base holds no value in the root
// `defaults:` is undeliverable — the exporter logs it, counts the tenant on
// da_config_subtree_undeliverable_tenants and serves no row for it; with the
// base valued at the root it is served. Measured on the committed config's
// rows (the collector's resolve).

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestSubtreeCritical_UndeliverableUnlessTheBaseIsValued(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name   string
		root   string
		gauge  float64
		served float64 // the tenant's row of the key (either reader), 0 = none
	}{
		{"base only in optional_overrides", "defaults:\n  mysql_connections: 80\noptional_overrides: [redis_memory]\n", 1, 0},
		{"base nowhere at the root", "defaults:\n  mysql_connections: 80\n", 1, 0},
		{"base valued at the root", "defaults:\n  redis_memory: 60\n", 0, 90},
		{"the _critical key itself valued at the root, no base", "defaults:\n  redis_memory_critical: 5\n", 0, 90},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			if err := os.MkdirAll(filepath.Join(dir, "team"), 0o755); err != nil {
				t.Fatal(err)
			}
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), tc.root)
			writeTestYAML(t, filepath.Join(dir, "team", "_defaults.yaml"), "defaults:\n  redis_memory_critical: 90\n")
			writeTestYAML(t, filepath.Join(dir, "team", "tx.yaml"), "tenants:\n  tx: {}\n")
			m, fresh, logBuf := newAuditedManager(t, dir)
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != tc.gauge {
				t.Errorf("da_config_subtree_undeliverable_tenants = %v, want %v\n%s", got, tc.gauge, logBuf.String())
			}
			logged := false
			for _, line := range strings.Split(logBuf.String(), "\n") {
				if strings.Contains(line, "subtree default undeliverable") && strings.Contains(line, "redis_memory_critical") {
					logged = true
				}
			}
			if logged != (tc.gauge > 0) {
				t.Errorf("undeliverable ERROR names redis_memory_critical: %v, want %v\n%s", logged, tc.gauge > 0, logBuf.String())
			}
			keyed, _, err := m.GetConfig().ResolveAtWithKeys(fixedNow)
			if err != nil {
				t.Fatal(err)
			}
			var got float64
			for _, k := range keyed {
				if k.Tenant == "tx" && k.Key == "redis_memory_critical" && !k.LegacyTwin {
					got = k.Value
				}
			}
			if got != tc.served {
				t.Errorf("critical row %v, want %v", got, tc.served)
			}
		})
	}
}
