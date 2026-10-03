package main

// #2544 (owner decision: option b): da-guard's redundant-override advice on
// `<metric>_critical` keys. The metric's severity=critical row comes only from
// the tenant's override map (resolveCriticalRows); a subtree
// `_defaults.yaml`, a platform `tenants:` entry and a profile fill that map,
// the root `_defaults.yaml` does not — its `_critical` key is served as a
// threshold of its own (metric `<metric>_critical`, severity=warning). So a
// tenant key equal to a root-only `_critical` default is not redundant:
// deleting it removes the critical row. The root key is reported by a warn
// finding of its own (root_defaults_critical_key) instead.
//
// The oracle is /metrics at the label level (servedSeries, the exporter's own
// collector), never `served-values`: with the key at the root AND in the
// tenant, served-values exits 2 (TestServedValues_RootAndTenantCriticalExits2),
// so a test that read it would have no oracle for exactly the shape at issue.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// legacyCritical is the retired #1231 spelling of mysql_threads_running's
// `_critical` key, taken from the alias table rather than spelled here.
func legacyCritical(t *testing.T) string {
	t.Helper()
	legacy, ok := config.LegacySpellingFor("mysql_threads_running")
	if !ok {
		t.Fatal("mysql_threads_running has no legacy spelling")
	}
	return legacy + "_critical"
}

// rootCriticalFields is the Field of every root_defaults_critical_key finding.
func rootCriticalFields(t *testing.T, dir string) map[string]bool {
	t.Helper()
	scoped, err := config.ScopeEffective(dir, dir)
	if err != nil {
		t.Fatal(err)
	}
	report, err := guard.CheckDefaultsImpact(buildCheckInput(scoped, &flags{}))
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]bool{}
	for _, f := range report.Findings {
		if f.Kind == guard.FindingRootDefaultsCriticalKey {
			if f.Severity != guard.SeverityWarn || f.TenantID != "" {
				t.Errorf("root_defaults_critical_key finding %+v: want severity warn and no tenant", f)
			}
			out[f.Field] = true
		}
	}
	return out
}

func TestGuard_RedundantOverrideOnCriticalKeys(t *testing.T) {
	t.Parallel()
	const root = "defaults:\n  mysql_connections: 80\n  mysql_threads_running: 80\n"
	const conn = "mysql_connections_critical"
	const threads = "mysql_threads_running_critical"
	legacy := legacyCritical(t)
	cases := []struct {
		name      string
		files     map[string]string // platform files (the tenant file is added)
		tfile     string            // the tenant file
		keep      string            // tx lines on both sides of the oracle
		line      string            // the override under test
		redundant bool
		rootWarn  []string // root_defaults_critical_key Fields expected
		symlink   bool     // read the tree through a symlink to its real root
	}{
		// The measured false advice (redundant before the fix), each spelling.
		{name: "root-only-critical-key-is-not-redundant",
			files: map[string]string{"_defaults.yaml": root + "  " + conn + ": 60\n"},
			tfile: "tx.yaml", keep: "    mysql_connections: 70\n", line: conn + ": 60",
			rootWarn: []string{"_defaults.yaml:defaults." + conn}},
		{name: "root-only-new-spelling-critical-key-is-not-redundant",
			files: map[string]string{"_defaults.yaml": root + "  " + threads + ": 60\n"},
			tfile: "tx.yaml", keep: "    mysql_threads_running: 70\n", line: threads + ": 60",
			rootWarn: []string{"_defaults.yaml:defaults." + threads}},
		{name: "root-only-legacy-spelling-critical-key-is-not-redundant",
			files: map[string]string{"_defaults.yaml": root + "  " + legacy + ": 60\n"},
			tfile: "tx.yaml", keep: "    mysql_threads_running: 70\n", line: legacy + ": 60",
			rootWarn: []string{"_defaults.yaml:defaults." + legacy}},
		{name: "root-only-critical-key-under-a-subtree-without-it-is-not-redundant",
			files: map[string]string{"_defaults.yaml": root + "  " + conn + ": 60\n", "sub/_defaults.yaml": "defaults:\n  mysql_connections: 75\n"},
			tfile: "sub/tx.yaml", keep: "    mysql_connections: 70\n", line: conn + ": 60",
			rootWarn: []string{"_defaults.yaml:defaults." + conn}},
		{name: "root-critical-key-a-subtree-nulls-is-not-redundant",
			files: map[string]string{"_defaults.yaml": root + "  " + conn + ": 60\n", "sub/_defaults.yaml": "defaults:\n  " + conn + ": null\n"},
			tfile: "sub/tx.yaml", keep: "    mysql_connections: 70\n", line: conn + ": 60",
			rootWarn: []string{"_defaults.yaml:defaults." + conn}},
		// The root level is found by PATH, against the scan's resolved root.
		{name: "root-only-critical-key-through-a-symlinked-config-dir-is-not-redundant",
			files: map[string]string{"_defaults.yaml": root + "  " + conn + ": 60\n"},
			tfile: "tx.yaml", keep: "    mysql_connections: 70\n", line: conn + ": 60",
			rootWarn: []string{"_defaults.yaml:defaults." + conn}, symlink: true},
		{name: "root-only-critical-key-in-a-yml-root-carrier-is-not-redundant",
			files: map[string]string{"_defaults.yml": root + "  " + conn + ": 60\n"},
			tfile: "tx.yaml", keep: "    mysql_connections: 70\n", line: conn + ": 60",
			rootWarn: []string{"_defaults.yml:defaults." + conn}},
		// Control (the ticket's): the same shape with no root `_critical` —
		// not redundant before the fix either, and no root warning.
		{name: "no-root-critical-key-is-not-redundant",
			files: map[string]string{"_defaults.yaml": root},
			tfile: "tx.yaml", keep: "    mysql_connections: 70\n", line: conn + ": 60"},
		{name: "no-root-new-spelling-critical-key-is-not-redundant",
			files: map[string]string{"_defaults.yaml": root},
			tfile: "tx.yaml", keep: "    mysql_threads_running: 70\n", line: threads + ": 60"},
		// Controls: the layers that DO fill the tenant's override map stay
		// redundant — the check is narrowed, not switched off.
		{name: "subtree-critical-key-is-redundant",
			files: map[string]string{"_defaults.yaml": root, "sub/_defaults.yaml": "defaults:\n  " + conn + ": 60\n"},
			tfile: "sub/tx.yaml", keep: "    mysql_connections: 70\n", line: conn + ": 60", redundant: true},
		{name: "root-and-subtree-critical-key-is-redundant",
			files: map[string]string{"_defaults.yaml": root + "  " + conn + ": 60\n", "sub/_defaults.yaml": "defaults:\n  " + conn + ": 60\n"},
			tfile: "sub/tx.yaml", keep: "    mysql_connections: 70\n", line: conn + ": 60", redundant: true,
			rootWarn: []string{"_defaults.yaml:defaults." + conn}},
		{name: "platform-entry-critical-key-is-redundant",
			files: map[string]string{"_defaults.yaml": root + "tenants:\n  tx:\n    " + conn + ": 60\n"},
			tfile: "tx.yaml", keep: "    mysql_connections: 70\n", line: conn + ": 60", redundant: true},
		{name: "profile-critical-key-is-redundant",
			files: map[string]string{"_defaults.yaml": root, "_profiles.yaml": "profiles:\n  std:\n    " + conn + ": 60\n"},
			tfile: "tx.yaml", keep: "    _profile: std\n", line: conn + ": 60", redundant: true},
		// Controls: `_critical`-suffixed keys under the `_state_` / `_silent_`
		// reserved prefixes are not critical-row keys (resolveCriticalRows
		// skips them; resolveBaseRows serves no row for them either). The
		// root's stays an inherited value — /metrics is the same with or
		// without the tenant's line, so it is redundant as before — and gets
		// no root_defaults_critical_key warning.
		{name: "root-state-prefixed-critical-key-is-redundant",
			files: map[string]string{"_defaults.yaml": root + "  _state_maintenance_critical: 60\n"},
			tfile: "tx.yaml", keep: "    mysql_connections: 70\n", line: "_state_maintenance_critical: 60", redundant: true},
		{name: "root-silent-prefixed-critical-key-is-redundant",
			files: map[string]string{"_defaults.yaml": root + "  _silent_mode_critical: 60\n"},
			tfile: "tx.yaml", keep: "    mysql_connections: 70\n", line: "_silent_mode_critical: 60", redundant: true},
		// Control: a plain root key is still redundant.
		{name: "root-plain-key-is-redundant",
			files: map[string]string{"_defaults.yaml": root},
			tfile: "tx.yaml", keep: "    mysql_threads_running: 70\n", line: "mysql_connections: 80", redundant: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			if tc.symlink {
				real := filepath.Join(dir, "real")
				link := filepath.Join(dir, "link")
				if err := os.Mkdir(real, 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(real, link); err != nil {
					t.Skipf("symlink unsupported: %v", err)
				}
				dir = link // every read below goes through the link
			}
			testutil.WriteTree(t, dir, tc.files)
			path := filepath.Join(dir, filepath.FromSlash(tc.tfile))
			if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
				t.Fatal(err)
			}
			write := func(s string) {
				t.Helper()
				if err := os.WriteFile(path, []byte(s), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			without := "tenants:\n  tx:\n" + tc.keep
			write(without)
			withoutS := servedSeries(t, dir)
			write(without + "    " + tc.line + "\n") // leaves the tenant file WITH the override
			withS := servedSeries(t, dir)
			unchanged := strings.Join(withS, " ") == strings.Join(withoutS, " ")
			if unchanged != tc.redundant {
				t.Fatalf("premise: /metrics %v with the override, %v without — want redundant=%v", withS, withoutS, tc.redundant)
			}

			field := strings.SplitN(tc.line, ": ", 2)[0]
			if got := redundantFields(t, dir)[field]; got != tc.redundant {
				t.Errorf("redundant_override on %s = %v, want %v (/metrics %v with the override, %v without)", field, got, tc.redundant, withS, withoutS)
			}

			gotWarn := rootCriticalFields(t, dir)
			want := map[string]bool{}
			for _, f := range tc.rootWarn {
				want[f] = true
			}
			if strings.Join(keysOf(gotWarn), " ") != strings.Join(keysOf(want), " ") {
				t.Errorf("root_defaults_critical_key fields = %v, want %v", keysOf(gotWarn), keysOf(want))
			}
		})
	}
}

// TestServedValues_RootAndTenantCriticalExits2 pins the premise the test
// above is built around: with the same `_critical` key at the root and in
// the tenant, `served-values` refuses the tree (the key owns a warning row
// from the root and a critical row from the tenant), while da-guard's
// defaults-impact still reports on it — the root warning, and no
// redundant_override on the tenant's line.
func TestServedValues_RootAndTenantCriticalExits2(t *testing.T) {
	t.Parallel()
	for _, key := range []string{"mysql_connections_critical", "mysql_threads_running_critical"} {
		t.Run(key, func(t *testing.T) {
			t.Parallel()
			base := strings.TrimSuffix(key, "_critical")
			files := map[string]string{
				"_defaults.yaml": "defaults:\n  " + base + ": 80\n  " + key + ": 60\n",
				"tx.yaml":        "tenants:\n  tx:\n    " + base + ": 70\n    " + key + ": 60\n",
			}
			code, doc, dir, stderr := served(t, files, "")
			if code != exitCallerErr {
				t.Fatalf("served-values exit = %d, want %d; stderr=%q", code, exitCallerErr, stderr)
			}
			if doc.Tenants != nil || !strings.Contains(stderr, "owns rows with different values") {
				t.Fatalf("served-values: want no JSON and the two-rows refusal; tenants=%v stderr=%q", doc.Tenants, stderr)
			}

			gcode, stdout, gstderr := runOnce(t, "--config-dir", dir, "--format", "json")
			if gcode != exitOK {
				t.Fatalf("da-guard exit = %d, want %d; stderr=%q", gcode, exitOK, gstderr)
			}
			if !strings.Contains(stdout, `"kind": "root_defaults_critical_key"`) {
				t.Errorf("da-guard: want a root_defaults_critical_key finding; stdout=%s", stdout)
			}
			if strings.Contains(stdout, "redundant_override") {
				t.Errorf("da-guard: the tenant's %s is not redundant; stdout=%s", key, stdout)
			}
		})
	}
}
