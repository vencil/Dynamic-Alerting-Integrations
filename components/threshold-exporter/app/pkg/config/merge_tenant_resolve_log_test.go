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
	"os"
	"path/filepath"
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
}

func TestTenantMergeResolveWritesNoLog(t *testing.T) {
	dir := t.TempDir()
	writeMergeTree(t, dir, resolveLogTree)
	body, err := os.ReadFile(filepath.Join(dir, "tx.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	buf := captureGlobalLog(t)
	// Two GETs' worth: the merge the handler builds, then every resolve entry.
	for i := 0; i < 2; i++ {
		m := MergeTenantWithRootDefaults(dir, "tx", body)
		rows := m.ResolveAt(platformMergeNow)
		_, stats := m.ResolveAtWithStats(platformMergeNow)
		keyed, _, err := m.ResolveAtWithKeys(platformMergeNow)
		if err != nil {
			t.Fatal(err)
		}
		nowRows := m.Resolve() // wall-clock variant; the cut does not depend on the time
		// Precondition: the cut ran on this merge (the root cap reached it).
		if len(rows) != 2 || len(keyed) != 2 || len(nowRows) != 2 || stats.PerTenantOverLimit["tx"] == 0 {
			t.Fatalf("precondition: rows=%d keyed=%d over=%d, want the cut to 2 rows",
				len(rows), len(keyed), stats.PerTenantOverLimit["tx"])
		}
		if buf.Len() != 0 {
			t.Fatalf("GET %d: TenantMerge resolve wrote to the global log:\n%s", i+1, buf.String())
		}
		// The same answer the embedded (logging) resolve gives.
		loudRows, loudStats := m.ThresholdConfig.ResolveAtWithStats(platformMergeNow)
		if len(loudRows) != len(rows) || loudStats.PerTenantOverLimit["tx"] != stats.PerTenantOverLimit["tx"] ||
			loudStats.PerTenantCustomAlertErrors["tx"] != stats.PerTenantCustomAlertErrors["tx"] {
			t.Fatalf("quiet resolve differs from the embedded one: rows %d vs %d, stats %+v vs %+v",
				len(rows), len(loudRows), stats, loudStats)
		}
		if buf.Len() == 0 {
			t.Fatal("precondition: the embedded resolve wrote nothing, so this tree trips no log line")
		}
		buf.Reset()
	}
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
