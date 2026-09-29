package config

// #2397: the tenant-api merge core resolves on every GET, so TenantMerge's
// resolve entry points must not write the resolver's ERROR/WARN lines to the
// process log; /metrics' resolve (a ThresholdConfig from LoadDir) must keep
// writing every one of them, once per resolve.
//
// ⛔ NOT t.Parallel(): these tests swap the process-global log output
// (captureGlobalLog — an idempotent reset to os.Stderr, never a
// save-then-restore; test-map.md).

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// resolveLogTree trips the cardinality cut (root max_metrics_per_tenant 2,
// below the tenant's row count) and every WARN shape the threshold resolve
// writes, all in one tenant. Every phase that reads a scheduled value (base,
// `_critical`, dimensional, declared) gets its own malformed window, named
// after the phase, so each phase's window WARN is its own line.
var resolveLogTree = map[string]string{
	"_defaults.yaml": "max_metrics_per_tenant: 2\n" +
		"defaults:\n  m1_x: 1\n  m2_x: 1\n  m3_x: 1\n  m4_x: 1\n" +
		"optional_overrides: [m9_x]\n",
	"tx.yaml": "tenants:\n  tx:\n" +
		"    m1_x: \"bogus\"\n" +
		"    m2_x:\n      default: \"2\"\n      overrides:\n        - window: \"25:00-26:00\"\n          value: \"5\"\n" +
		"    m2_x_critical:\n      default: \"zz\"\n      overrides:\n        - window: \"badcrit\"\n          value: \"5\"\n" +
		"    nosuch_critical: \"5\"\n" +
		"    m3_x:\n      default: \"3\"\n      overrides:\n        - window: \"badbase\"\n          value: \"5\"\n" +
		"    \"m4_x{q=\\\"a\\\"}\":\n      default: \"zz\"\n      overrides:\n        - window: \"baddim\"\n          value: \"5\"\n" +
		"    \"m4_x{}\": \"7\"\n" +
		"    m9_x:\n      default: \"zz\"\n      overrides:\n        - window: \"baddecl\"\n          value: \"5\"\n" +
		"    _custom_alerts: \"not a list\"\n",
	// A second tenant for the one-spec rejection: a `_custom_alerts` that is
	// not a list returns before any spec is looked at, so tx cannot carry
	// both. ty is also over the cap (four defaults), hence its cut line.
	"ty.yaml": "tenants:\n  ty:\n" +
		"    _custom_alerts:\n      - name: badspec\n        recipe: nosuch_recipe\n",
}

// resolveLogLines is one line per shape resolveLogTree trips, as /metrics
// writes it.
var resolveLogLines = []string{
	`ERROR: tenant=tx produced `, // cardinality cut
	`WARN: unknown value "bogus" for tenant=tx metric=m1_x, using default`,
	`WARN: invalid time window "25:00-26:00": start=`, // range error, base phase
	`WARN: invalid time window format "badbase"`,
	`WARN: invalid time window format "badcrit"`,
	`WARN: invalid critical threshold "zz" for tenant=tx key=m2_x_critical`,
	`WARN: _critical key "nosuch_critical" has no matching default "nosuch", skipping`,
	`WARN: invalid time window format "baddim"`,
	`WARN: invalid dimensional threshold "zz" for tenant=tx key=m4_x{q="a"}, skipping`,
	`WARN: failed to parse dimensional key "m4_x{}" for tenant=tx, skipping`,
	`WARN: invalid time window format "baddecl"`,
	`WARN: invalid declared threshold "zz" for tenant=tx key=m9_x, skipping`,
	`ERROR: tenant=tx: custom alert "<block>" rejected`,
	`ERROR: tenant=ty produced `,
	`ERROR: tenant=ty: custom alert "badspec" rejected`,
}

func TestTenantMergeResolveWritesNoLog(t *testing.T) {
	dir := t.TempDir()
	writeMergeTree(t, dir, resolveLogTree)
	buf := captureGlobalLog(t)
	for _, id := range []string{"tx", "ty"} {
		body, err := os.ReadFile(filepath.Join(dir, id+".yaml"))
		if err != nil {
			t.Fatal(err)
		}
		// Two GETs' worth: the merge the handler builds, then every resolve entry.
		for i := 0; i < 2; i++ {
			m := MergeTenantWithRootDefaults(dir, id, body)
			rows := m.ResolveAt(platformMergeNow)
			_, stats := m.ResolveAtWithStats(platformMergeNow)
			keyed, _, err := m.ResolveAtWithKeys(platformMergeNow)
			if err != nil {
				t.Fatal(err)
			}
			// Resolve() reads the wall clock, so it is checked for writing no
			// log (and for the cut, which does not depend on the time) only —
			// its rows are not compared with a fixed-time resolve.
			nowRows := m.Resolve()
			// Precondition: the cut ran on this merge (the root cap reached it).
			if len(rows) != 2 || len(keyed) != 2 || len(nowRows) != 2 || stats.PerTenantOverLimit[id] == 0 {
				t.Fatalf("%s precondition: rows=%d keyed=%d nowRows=%d over=%d, want the cut to 2 rows",
					id, len(rows), len(keyed), len(nowRows), stats.PerTenantOverLimit[id])
			}
			if buf.Len() != 0 {
				t.Fatalf("%s GET %d: TenantMerge resolve wrote to the global log:\n%s", id, i+1, buf.String())
			}
			// The same answer the embedded (logging) resolve gives at the same
			// instant: rows compared whole, in a canonical order (the resolver's
			// order within a segment follows map iteration).
			loudRows, loudStats := m.ThresholdConfig.ResolveAtWithStats(platformMergeNow)
			if !reflect.DeepEqual(sortedRows(rows), sortedRows(loudRows)) {
				t.Fatalf("%s: quiet rows differ from the embedded resolve's:\n%+v\nvs\n%+v", id, rows, loudRows)
			}
			if !reflect.DeepEqual(stats, loudStats) {
				t.Fatalf("%s: quiet stats differ from the embedded resolve's: %+v vs %+v", id, stats, loudStats)
			}
			if buf.Len() == 0 {
				t.Fatalf("%s precondition: the embedded resolve wrote nothing, so this tree trips no log line", id)
			}
			buf.Reset()
		}
	}
}

func sortedRows(rows []ResolvedThreshold) []ResolvedThreshold {
	out := append([]ResolvedThreshold(nil), rows...)
	sort.Slice(out, func(i, j int) bool { return fmt.Sprintf("%+v", out[i]) < fmt.Sprintf("%+v", out[j]) })
	return out
}

func TestExporterResolveStillLogsEveryLine(t *testing.T) {
	dir := t.TempDir()
	writeMergeTree(t, dir, resolveLogTree)
	cfg, _, err := LoadDir(dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	buf := captureGlobalLog(t)
	for _, resolve := range []struct {
		name string
		run  func()
	}{
		{"ResolveAtWithStats", func() { cfg.ResolveAtWithStats(platformMergeNow) }},
		{"ResolveAt", func() { cfg.ResolveAt(platformMergeNow) }},
		{"ResolveAtWithKeys", func() { _, _, _ = cfg.ResolveAtWithKeys(platformMergeNow) }},
	} {
		buf.Reset()
		resolve.run()
		out := buf.String()
		for _, line := range resolveLogLines {
			if n := strings.Count(out, line); n != 1 {
				t.Errorf("%s: %q written %d times, want 1:\n%s", resolve.name, line, n, out)
			}
		}
		// Entries, not newlines: the custom-alert ERROR embeds the YAML
		// decoder's multi-line message.
		if n := strings.Count(out, " WARN: ") + strings.Count(out, " ERROR: "); n != len(resolveLogLines) {
			t.Errorf("%s: %d log entries, want %d:\n%s", resolve.name, n, len(resolveLogLines), out)
		}
	}
}
