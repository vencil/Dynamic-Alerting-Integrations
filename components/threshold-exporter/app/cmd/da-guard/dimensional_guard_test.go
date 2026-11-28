package main

// #2419: da-guard's redundant-override advice on DIMENSIONAL keys
// (`pg_connections{env="prod"}`). Before #2031 the root `_defaults.yaml`'s
// dimensional key was served as a row of its own (metric
// `connections{env="prod"}`, no labels), so a tenant key equal to it was not
// redundant. Since #2031 every layer's dimensional key — the root's too — is
// the labelled series of a tenant that does not write it, so such a tenant
// key is redundant like any other.
//
// The oracle is /metrics at the LABEL level — every user_threshold series of
// tx, label set and value, gathered through the exporter's own collector —
// with and without the key: redundant ⇔ the two sets are equal. Comparing
// metric name and value alone would miss exactly this shape.
//
// Since ADR-038 §11 a dimensional key is served only when every label of its
// selector is declared under `dimensions:` (a regex label as `<label>_re`),
// so each case declares its labels: without them neither arm serves a
// labelled series, the oracle's two sets are trivially equal, and the case
// tests nothing (#4329). The premise therefore also requires the labelled
// series to be on /metrics with the override.

import (
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/internal/scrape"
	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// servedSeries is tx's user_threshold series on /metrics, each as its full
// label set and value, sorted.
func servedSeries(t *testing.T, dir string) []string {
	t.Helper()
	cfg, _, err := config.LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	at := time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)
	reg := prometheus.NewRegistry()
	scrape.Register(reg, scrape.NewCollectorWithHooks(staticSource{cfg, at}, scrape.Hooks{}), scrape.NewConfigMetrics())
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, mf := range mfs {
		if mf.GetName() != "user_threshold" {
			continue
		}
		for _, m := range mf.GetMetric() {
			var ls []string
			mine := false
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "tenant" && lp.GetValue() == "tx" {
					mine = true
				}
				ls = append(ls, lp.GetName()+"="+lp.GetValue())
			}
			if mine {
				out = append(out, "{"+strings.Join(ls, ",")+"}="+fmt.Sprint(m.GetGauge().GetValue()))
			}
		}
	}
	sort.Strings(out)
	return out
}

func TestGuard_RedundantOverrideOnDimensionalKeys(t *testing.T) {
	t.Parallel()
	const dims = "dimensions:\n  pg_connections: [env, db_re]\n"
	const root = dims + "defaults:\n  mysql_connections: 80\n  pg_connections: 100\n"
	const dim = `pg_connections{env="prod"}`
	cases := []struct {
		name      string
		files     map[string]string // platform files (the tenant file is added)
		tfile     string            // the tenant file
		keep      string            // tx lines on both sides of the oracle
		line      string            // the override under test
		redundant bool
		symlink   bool // read the tree through a symlink to its real root
	}{
		// A root dimensional default (#2419 measured the false advice; #2031
		// made the root a fallback, so these flipped to redundant).
		{"root-only-dimensional-key-is-redundant",
			map[string]string{"_defaults.yaml": root + "  " + dim + ": 50\n"},
			"tx.yaml", "    mysql_connections: 80\n", dim + ": 50", true, false},
		{"root-only-regex-dimensional-key-is-redundant",
			map[string]string{"_defaults.yaml": root + "  pg_connections{db=~\"a.*\"}: 50\n"},
			"tx.yaml", "    mysql_connections: 80\n", "pg_connections{db=~\"a.*\"}: 50", true, false},
		{"root-only-dimensional-key-under-a-subtree-without-it-is-redundant",
			map[string]string{"_defaults.yaml": root + "  " + dim + ": 50\n", "sub/_defaults.yaml": "defaults:\n  pg_connections: 60\n"},
			"sub/tx.yaml", "    mysql_connections: 80\n", dim + ": 50", true, false},
		{"root-dimensional-key-a-subtree-nulls-is-redundant",
			map[string]string{"_defaults.yaml": root + "  " + dim + ": 50\n", "sub/_defaults.yaml": "defaults:\n  " + dim + ": null\n"},
			"sub/tx.yaml", "    mysql_connections: 80\n", dim + ": 50", true, false},
		// The root level is found by PATH, against the scan's resolved root:
		// a config dir that is a symlink, and a root carrier spelled
		// `_defaults.yml`, are still the root.
		{"root-only-dimensional-key-through-a-symlinked-config-dir-is-redundant",
			map[string]string{"_defaults.yaml": root + "  " + dim + ": 50\n"},
			"tx.yaml", "    mysql_connections: 80\n", dim + ": 50", true, true},
		{"root-only-dimensional-key-in-a-yml-root-carrier-is-redundant",
			map[string]string{"_defaults.yml": root + "  " + dim + ": 50\n"},
			"tx.yaml", "    mysql_connections: 80\n", dim + ": 50", true, false},
		// The layers that fill the tenant's override map.
		{"subtree-dimensional-key-is-redundant",
			map[string]string{"_defaults.yaml": root, "sub/_defaults.yaml": "defaults:\n  " + dim + ": 50\n"},
			"sub/tx.yaml", "    mysql_connections: 80\n", dim + ": 50", true, false},
		{"root-and-subtree-dimensional-key-is-redundant",
			map[string]string{"_defaults.yaml": root + "  " + dim + ": 50\n", "sub/_defaults.yaml": "defaults:\n  " + dim + ": 50\n"},
			"sub/tx.yaml", "    mysql_connections: 80\n", dim + ": 50", true, false},
		// `dimensions:` is read from the root carrier only, so the root
		// carrier declares the labels and writes no `defaults:`.
		{"subtree-dimensional-key-with-no-root-defaults-is-redundant",
			map[string]string{"_defaults.yaml": dims, "sub/_defaults.yaml": "defaults:\n  pg_connections: 100\n  " + dim + ": 50\n"},
			"sub/tx.yaml", "    pg_connections: 90\n", dim + ": 50", true, false},
		{"platform-entry-dimensional-key-is-redundant",
			map[string]string{"_defaults.yaml": root + "tenants:\n  tx:\n    " + dim + ": 50\n"},
			"tx.yaml", "    mysql_connections: 80\n", dim + ": 50", true, false},
		{"profile-dimensional-key-is-redundant",
			map[string]string{"_defaults.yaml": root, "_profiles.yaml": "profiles:\n  std:\n    " + dim + ": 50\n"},
			"tx.yaml", "    _profile: std\n", dim + ": 50", true, false},
		// Control: a plain root key is still redundant.
		{"root-plain-key-is-redundant",
			map[string]string{"_defaults.yaml": root},
			"tx.yaml", "    mysql_connections: 70\n", "pg_connections: 100", true, false},
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
			if !hasDimensionalSeries(withS, tc.line) {
				t.Fatalf("premise: /metrics %v with the override serves no series labelled by %s — the case tests nothing (#4329)", withS, tc.line)
			}
			if unchanged != tc.redundant {
				t.Fatalf("premise: /metrics %v with the override, %v without — want redundant=%v", withS, withoutS, tc.redundant)
			}

			field := strings.SplitN(tc.line, ": ", 2)[0]
			if got := redundantFields(t, dir)[field]; got != tc.redundant {
				t.Errorf("redundant_override on %s = %v, want %v (/metrics %v with the override, %v without)", field, got, tc.redundant, withS, withoutS)
			}
		})
	}
}

// hasDimensionalSeries reports whether series holds a user_threshold series
// carrying a label of line's dimensional selector (`env=prod`, or a regex
// label as `<label>_re`), or line is not dimensional (a control case).
func hasDimensionalSeries(series []string, line string) bool {
	key := strings.SplitN(line, ": ", 2)[0]
	i := strings.Index(key, "{")
	if i < 0 {
		return true
	}
	sel := strings.TrimSuffix(key[i+1:], "}")
	label := sel
	for _, op := range []string{"=~", "="} {
		if j := strings.Index(sel, op); j > 0 {
			label = sel[:j]
			if op == "=~" {
				label += config.RegexLabelSuffix
			}
			break
		}
	}
	for _, s := range series {
		if strings.Contains(s, ","+label+"=") || strings.Contains(s, "{"+label+"=") {
			return true
		}
	}
	return false
}
