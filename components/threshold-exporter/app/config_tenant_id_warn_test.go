package main

// ADR-035 D4 (#2655): a tenant whose id breaks the tenant-id rule is still
// loaded and exported, and every committed reload logs ONE WARN per such id,
// citing tenantid.Description.
//
// "Once per reload" means once per COMMIT: a tick that finds nothing changed
// commits nothing and therefore warns nothing; a reload that does commit
// (any file changed) warns again for every invalid id still present. Both
// halves are asserted below, on each of the three commit paths (hierarchical
// with a `_defaults.yaml`, flat incremental without one, single file).
//
// Logger and metrics go through the manager's seams (test-map.md), so these
// tests run in parallel and never touch the process-global logger.

import (
	"bytes"
	"log"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/pkg/tenantid"
)

// invalidTenantIDAnchor identifies the D4 WARN line and nothing else.
const invalidTenantIDAnchor = "breaks the tenant-id rule"

const tenantIDWarnTenants = `tenants:
  t-ok:
    mysql_connections: "%s"
  Team_A:
    mysql_connections: "71"
  x_y:
    mysql_connections: "72"
`

func tenantIDWarnTenantsWith(okValue string) string {
	return strings.Replace(tenantIDWarnTenants, "%s", okValue, 1)
}

// invalidTenantIDWarns returns, per tenant id quoted in a D4 WARN line, how
// many such lines the log holds, and fails if a line lacks the description.
func invalidTenantIDWarns(t *testing.T, logs string) map[string]int {
	t.Helper()
	counts := map[string]int{}
	for _, line := range logLinesWith(logs, invalidTenantIDAnchor) {
		if !strings.Contains(line, tenantid.Description) {
			t.Errorf("D4 WARN does not cite tenantid.Description %q:\n%s", tenantid.Description, line)
		}
		for _, id := range []string{"t-ok", "Team_A", "x_y"} {
			if strings.Contains(line, `"`+id+`"`) {
				counts[id]++
			}
		}
	}
	return counts
}

// exportedThresholdTenants gathers the collector once and returns the sorted
// tenant label values of its user_threshold series.
func exportedThresholdTenants(t *testing.T, m *ConfigManager) []string {
	t.Helper()
	reg := prometheus.NewRegistry()
	reg.MustRegister(NewThresholdCollector(m))
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	seen := map[string]bool{}
	for _, f := range families {
		if f.GetName() != "user_threshold" {
			continue
		}
		for _, metric := range f.GetMetric() {
			for _, lp := range metric.GetLabel() {
				if lp.GetName() == "tenant" {
					seen[lp.GetValue()] = true
				}
			}
		}
	}
	out := make([]string, 0, len(seen))
	for id := range seen {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

// assertAllThreeLoaded asserts the committed config holds all three tenants
// and, when the tree has defaults to resolve against, that the collector
// serves a user_threshold series for each.
func assertAllThreeLoaded(t *testing.T, m *ConfigManager, exports bool, when string) {
	t.Helper()
	want := []string{"Team_A", "t-ok", "x_y"}
	loaded := make([]string, 0, len(m.GetConfig().Tenants))
	for id := range m.GetConfig().Tenants {
		loaded = append(loaded, id)
	}
	sort.Strings(loaded)
	if strings.Join(loaded, ",") != strings.Join(want, ",") {
		t.Errorf("%s: loaded tenants = %v, want %v", when, loaded, want)
	}
	if !exports {
		return
	}
	if got := exportedThresholdTenants(t, m); strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("%s: user_threshold tenants = %v, want %v", when, got, want)
	}
}

func TestReload_InvalidTenantIDWarnsOncePerCommitAndStillExports(t *testing.T) {
	t.Parallel()

	const defaults = "defaults:\n  mysql_connections: 80\n"
	cases := []struct {
		name string
		// write lays the tree out with t-ok's value; it returns the path
		// the manager watches.
		write func(t *testing.T, dir, okValue string) string
		// exports: the tree has a defaults carrier, so user_threshold
		// series exist to check. A flat tree has none — a `defaults:` block
		// outside `_defaults.yaml` is stripped — so it exports no threshold
		// for any tenant, valid or not, and only the loaded set is checked.
		exports bool
	}{
		{
			name: "hierarchical (_defaults.yaml present)",
			write: func(t *testing.T, dir, okValue string) string {
				writeFile(t, filepath.Join(dir, "_defaults.yaml"), defaults)
				writeFile(t, filepath.Join(dir, "tenants.yaml"), tenantIDWarnTenantsWith(okValue))
				return dir
			},
			exports: true,
		},
		{
			name: "flat incremental (no _defaults.yaml)",
			write: func(t *testing.T, dir, okValue string) string {
				writeFile(t, filepath.Join(dir, "tenants.yaml"), tenantIDWarnTenantsWith(okValue))
				return dir
			},
			exports: false,
		},
		{
			name: "single file",
			write: func(t *testing.T, dir, okValue string) string {
				p := filepath.Join(dir, "config.yaml")
				writeFile(t, p, defaults+tenantIDWarnTenantsWith(okValue))
				return p
			},
			exports: true,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			path := tc.write(t, dir, "70")

			fresh, _ := freshMetrics(t)
			var buf bytes.Buffer
			m := NewConfigManagerWithDebounce(path, 0)
			m.SetMetrics(fresh)
			m.SetLogger(log.New(&buf, "", 0))
			t.Cleanup(m.Close)

			if err := m.Load(); err != nil {
				t.Fatalf("load: %v", err)
			}
			got := invalidTenantIDWarns(t, buf.String())
			if got["Team_A"] != 1 || got["x_y"] != 1 || got["t-ok"] != 0 {
				t.Fatalf("after the first load: D4 WARN count per id = %v, want Team_A:1 x_y:1 t-ok:0; log:\n%s",
					got, buf.String())
			}
			if n := len(logLinesWith(buf.String(), invalidTenantIDAnchor)); n != 2 {
				t.Errorf("after the first load: %d D4 WARN lines, want 2; log:\n%s", n, buf.String())
			}

			// D4: loaded anyway — both invalid ids are still exported.
			assertAllThreeLoaded(t, m, tc.exports, "after the first load")

			// A tick over an unchanged tree commits nothing, so it warns nothing.
			m.tickOnce()
			if got := invalidTenantIDWarns(t, buf.String()); got["Team_A"] != 1 || got["x_y"] != 1 {
				t.Errorf("an unchanged tick re-warned: D4 WARN count per id = %v, want still 1 each; log:\n%s",
					got, buf.String())
			}

			// A reload that commits — t-ok's value changes, the invalid
			// tenants do not — warns again, once per id.
			tc.write(t, dir, "7000")
			if _, _, err := m.diffAndReload(); err != nil {
				t.Fatalf("reload: %v", err)
			}
			got = invalidTenantIDWarns(t, buf.String())
			if got["Team_A"] != 2 || got["x_y"] != 2 || got["t-ok"] != 0 {
				t.Errorf("after the second commit: D4 WARN count per id = %v, want Team_A:2 x_y:2 t-ok:0; log:\n%s",
					got, buf.String())
			}
			assertAllThreeLoaded(t, m, tc.exports, "after the second commit")
		})
	}
}

// TestWarnInvalidTenantIDs_AllValidIsSilent pins the quiet path: a config
// whose ids all pass logs nothing.
func TestWarnInvalidTenantIDs_AllValidIsSilent(t *testing.T) {
	t.Parallel()
	var buf bytes.Buffer
	warnInvalidTenantIDs(log.New(&buf, "", 0), &ThresholdConfig{
		Tenants: map[string]map[string]ScheduledValue{"t-ok": {}, "t-two": {}},
	})
	if buf.Len() != 0 {
		t.Errorf("all-valid config logged:\n%s", buf.String())
	}
}
