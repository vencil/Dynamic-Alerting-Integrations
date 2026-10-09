package main

// #2031: the main gate refuses a tree whose /metrics cannot be gathered —
// the shapes the decode does not fold into one key — over the whole tree
// whatever --scope says.

import (
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

func TestGuard_MetricsNotGatherable(t *testing.T) {
	t.Parallel()
	const root = "defaults:\n  mysql_connections: 80\n  redis_queue_length: 10\n"
	for name, tc := range map[string]struct {
		tenant string
		want   bool
	}{
		"value with :critical beside the _critical key": {
			"    mysql_connections: \"70:critical\"\n    mysql_connections_critical: 95\n", true},
		"regex label beside an exact label named like its export": {
			"    'redis_queue_length{q=~\"a\"}': 2\n    'redis_queue_length{q_re=\"a\"}': 3\n", true},
		// left as written (the parser cuts at the comma), so not merged:
		// both serve q_re="A"
		"keys the parser cuts at a comma": {
			"    'redis_queue_length{q=~\"A,B\"}': 1\n    'redis_queue_length{q=~\"A\"}': 2\n", true},
		"control": {"    mysql_connections: \"70:critical\"\n    'redis_queue_length{q=~\"a\"}': 2\n" + windowedQRe("b"), false},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{
				"_defaults.yaml": root,
				"team/tx.yaml":   "tenants:\n  tx:\n" + tc.tenant,
				"other/ty.yaml":  "tenants:\n  ty:\n    mysql_connections: 60\n",
				"empty/README":   "no tenant here\n",
			}
			code, fs := guardFindingsOf(t, files)
			code2, fs2 := guardFindingsOf(t, files, "--scope", "other")
			code3, fs3 := guardFindingsOf(t, files, "--scope", "empty")
			for _, run := range []struct {
				code int
				fs   []guard.Finding
			}{{code, fs}, {code2, fs2}, {code3, fs3}} {
				var got []guard.Finding
				for _, f := range run.fs {
					if f.Kind == guard.FindingMetricsNotGatherable {
						got = append(got, f)
					}
				}
				if !tc.want {
					if len(got) != 0 || run.code != exitOK {
						t.Errorf("exit %d, findings %+v: want none", run.code, got)
					}
					continue
				}
				if len(got) != 1 || got[0].Severity != guard.SeverityError || run.code != exitFindings ||
					!strings.Contains(got[0].Message, "HTTP 500") || !strings.Contains(got[0].Message, "tenant tx") {
					t.Errorf("exit %d, findings %+v: want one metrics_not_gatherable error naming tenant tx", run.code, got)
				}
			}
		})
	}
}

// windowedQRe is a tenant line: `redis_queue_length{q_re="<v>"}` switched off
// but in the 12:00-14:00 window.
func windowedQRe(v string) string {
	return "    'redis_queue_length{q_re=\"" + v + "\"}':\n      default: disable\n" +
		"      overrides:\n        - window: \"12:00-14:00\"\n          value: \"3\"\n"
}

// The verdict covers every schedule cut of the day: the same at any time it
// is read.
func TestGatherVerdict_EveryCutOfTheDay(t *testing.T) {
	t.Parallel()
	for v, want := range map[string]bool{"a": true, "b": false} {
		dir := t.TempDir()
		testutil.WriteTree(t, dir, map[string]string{
			"_defaults.yaml": "defaults:\n  redis_queue_length: 10\n",
			"tx.yaml":        "tenants:\n  tx:\n    'redis_queue_length{q=~\"a\"}': 2\n" + windowedQRe(v),
		})
		cfg, _, err := config.LoadDir(dir, nil)
		if err != nil {
			t.Fatal(err)
		}
		for _, hm := range []string{"00:00", "12:30", "23:59"} {
			at, _ := time.Parse(time.RFC3339, "2026-07-01T"+hm+":00Z")
			if got := gatherVerdict(cfg, at, func(_, k string) string { return k }); (got != "") != want {
				t.Errorf("q_re=%q at %s: verdict %q, want one: %v", v, hm, got, want)
			}
		}
	}
}
