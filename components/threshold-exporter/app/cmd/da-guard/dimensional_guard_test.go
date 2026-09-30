package main

// #2419: da-guard's redundant-override advice on DIMENSIONAL keys
// (`pg_connections{env="prod"}`). The labelled series comes only from the
// tenant's override map (resolveDimensionalRows); a subtree `_defaults.yaml`,
// a platform `tenants:` entry and a profile fill that map, the root
// `_defaults.yaml` does not (its key is served as a row of its own, metric
// `connections{env="prod"}`, no labels). So a tenant key equal to a
// root-only dimensional default is not redundant: deleting it removes the
// `env="prod"` series.
//
// The oracle is /metrics at the LABEL level — every user_threshold series of
// tx, label set and value, gathered through the exporter's own collector —
// with and without the key: redundant ⇔ the two sets are equal. Comparing
// metric name and value alone would miss exactly this shape.

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
	scrape.Register(reg, scrape.NewCollectorWithHooks(staticSource{cfg}, scrape.Hooks{Now: func() time.Time { return at }}), scrape.NewConfigMetrics())
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
	const root = "defaults:\n  mysql_connections: 80\n  pg_connections: 100\n"
	const dim = `pg_connections{env="prod"}`
	cases := []struct {
		name      string
		files     map[string]string // platform files (the tenant file is added)
		tfile     string            // the tenant file
		keep      string            // tx lines on both sides of the oracle
		line      string            // the override under test
		redundant bool
	}{
		// The measured false advice (redundant before the fix).
		{"root-only-dimensional-key-is-not-redundant",
			map[string]string{"_defaults.yaml": root + "  " + dim + ": 50\n"},
			"tx.yaml", "    mysql_connections: 80\n", dim + ": 50", false},
		{"root-only-regex-dimensional-key-is-not-redundant",
			map[string]string{"_defaults.yaml": root + "  pg_connections{db=~\"a.*\"}: 50\n"},
			"tx.yaml", "    mysql_connections: 80\n", "pg_connections{db=~\"a.*\"}: 50", false},
		{"root-only-dimensional-key-under-a-subtree-without-it-is-not-redundant",
			map[string]string{"_defaults.yaml": root + "  " + dim + ": 50\n", "sub/_defaults.yaml": "defaults:\n  pg_connections: 60\n"},
			"sub/tx.yaml", "    mysql_connections: 80\n", dim + ": 50", false},
		{"root-dimensional-key-a-subtree-nulls-is-not-redundant",
			map[string]string{"_defaults.yaml": root + "  " + dim + ": 50\n", "sub/_defaults.yaml": "defaults:\n  " + dim + ": null\n"},
			"sub/tx.yaml", "    mysql_connections: 80\n", dim + ": 50", false},
		// Controls: the layers that DO fill the tenant's override map stay
		// redundant — the check is narrowed, not switched off.
		{"subtree-dimensional-key-is-redundant",
			map[string]string{"_defaults.yaml": root, "sub/_defaults.yaml": "defaults:\n  " + dim + ": 50\n"},
			"sub/tx.yaml", "    mysql_connections: 80\n", dim + ": 50", true},
		{"root-and-subtree-dimensional-key-is-redundant",
			map[string]string{"_defaults.yaml": root + "  " + dim + ": 50\n", "sub/_defaults.yaml": "defaults:\n  " + dim + ": 50\n"},
			"sub/tx.yaml", "    mysql_connections: 80\n", dim + ": 50", true},
		{"subtree-dimensional-key-with-no-root-defaults-is-redundant",
			map[string]string{"sub/_defaults.yaml": "defaults:\n  pg_connections: 100\n  " + dim + ": 50\n"},
			"sub/tx.yaml", "    pg_connections: 90\n", dim + ": 50", true},
		{"platform-entry-dimensional-key-is-redundant",
			map[string]string{"_defaults.yaml": root + "tenants:\n  tx:\n    " + dim + ": 50\n"},
			"tx.yaml", "    mysql_connections: 80\n", dim + ": 50", true},
		{"profile-dimensional-key-is-redundant",
			map[string]string{"_defaults.yaml": root, "_profiles.yaml": "profiles:\n  std:\n    " + dim + ": 50\n"},
			"tx.yaml", "    _profile: std\n", dim + ": 50", true},
		// Control: a plain root key is still redundant.
		{"root-plain-key-is-redundant",
			map[string]string{"_defaults.yaml": root},
			"tx.yaml", "    mysql_connections: 70\n", "pg_connections: 100", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
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
		})
	}
}
