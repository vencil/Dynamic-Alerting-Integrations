package main

// #2031: the main gate refuses a tree whose /metrics cannot be gathered —
// the shapes the decode does not fold into one key — with the verdict
// served-values gives, over the whole tree whatever --scope says.

import (
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
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
		"control": {"    mysql_connections: \"70:critical\"\n    'redis_queue_length{q=~\"a\"}': 2\n", false},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{
				"_defaults.yaml": root,
				"team/tx.yaml":   "tenants:\n  tx:\n" + tc.tenant,
				"other/ty.yaml":  "tenants:\n  ty:\n    mysql_connections: 60\n",
			}
			code, fs := guardFindingsOf(t, files)
			code2, fs2 := guardFindingsOf(t, files, "--scope", "other")
			for _, run := range []struct {
				code int
				fs   []guard.Finding
			}{{code, fs}, {code2, fs2}} {
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
