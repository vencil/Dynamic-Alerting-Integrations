package config

// The keyed resolve (#2115) must be the public resolve plus the key reports,
// nothing else: the sink cannot change a row, add one, drop one, or move the
// cardinality cut.

import (
	"fmt"
	"path/filepath"
	"reflect"
	"sort"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/servedrows"
	"github.com/vencil/threshold-exporter/internal/testutil"
)

// keyedTree reaches every row source the resolver has — base, `:severity`,
// `_critical`, dimensional, declared, a #1231 alias (and so a legacy twin),
// custom alerts, a schedule — plus one tenant over the cardinality cap, so
// the sorted-and-cut path runs with a sink too.
var keyedTree = map[string]string{
	"conf.d/_defaults.yaml": `defaults:
  mysql_connections: 80
  mysql_threads_running: 30
  redis_memory: 70
  container_memory: 85
optional_overrides:
  - kafka_lag
max_metrics_per_tenant: 4
`,
	"conf.d/tenant-a.yaml": `tenants:
  tenant-a:
    mysql_connections: "70:critical"
    mysql_cpu: 44
    kafka_lag: 1000
    redis_queue_length{queue="tasks"}: "5"
    container_memory:
      default: "disable"
      overrides:
        - window: "01:00-09:00"
          value: "88"
    _custom_alerts:
      - {recipe: threshold, name: q_high, metric: qd, op: ">", window: 5m, threshold: "100:warning"}
`,
	"conf.d/tenant-b.yaml": `tenants:
  tenant-b:
    mysql_connections_critical: 95
    redis_memory: disable
`,
}

func rowStrings(rows []ResolvedThreshold) []string {
	out := make([]string, len(rows))
	for i, r := range rows {
		out[i] = fmt.Sprintf("%#v", r)
	}
	sort.Strings(out)
	return out
}

func TestKeyedResolve_SameRowsAndStatsAsPublicResolve(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, keyedTree)
	cfg, _, err := LoadDir(filepath.Join(tmp, "conf.d"), nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, at := range []string{"2026-07-01T03:00:00Z", "2026-07-01T12:00:00Z"} {
		now, _ := time.Parse(time.RFC3339, at)
		pub, pubStats := cfg.ResolveAtWithStats(now)
		var reported []ResolvedThreshold
		keyed, keyedStats := cfg.resolveAtWithStats(now, func(_ string, r ResolvedThreshold) {
			reported = append(reported, r)
		})
		// Tenants are walked in map order, so compare as sets of rows.
		if got, want := rowStrings(keyed), rowStrings(pub); !reflect.DeepEqual(got, want) {
			t.Errorf("%s: keyed resolve rows differ from ResolveAtWithStats:\n got %v\nwant %v", at, got, want)
		}
		if !reflect.DeepEqual(keyedStats.PerTenantOverLimit, pubStats.PerTenantOverLimit) ||
			!reflect.DeepEqual(keyedStats.PerTenantCustomAlertErrors, pubStats.PerTenantCustomAlertErrors) ||
			!reflect.DeepEqual(keyedStats.PerTenantDeprecatedKeys, pubStats.PerTenantDeprecatedKeys) {
			t.Errorf("%s: stats differ: keyed %+v, public %+v", at, keyedStats, pubStats)
		}
		// Every returned row is reported, in its place.
		if !reflect.DeepEqual(reported, keyed) {
			t.Errorf("%s: the sink saw %d rows, the resolve returned %d (or in another order)", at, len(reported), len(keyed))
		}
		if pubStats.PerTenantOverLimit["tenant-a"] == 0 {
			t.Errorf("%s: tenant-a should be over the cap so the cut runs with a sink", at)
		}
	}
}

// TestKeyedResolve_ReportsTheServedKey pins what the sink says: the whole
// canonical key, the legacy twin under its canonical key, and custom alerts
// under `_custom_alerts`.
func TestKeyedResolve_ReportsTheServedKey(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults:            map[string]float64{"mysql_connections": 80, "mysql_threads_running": 30},
		OptionalOverrides:   []string{"kafka_lag"},
		MaxMetricsPerTenant: -1,
		Tenants: map[string]map[string]ScheduledValue{
			"tenant-a": {
				"mysql_connections_critical":        sv("95"),
				"mysql_cpu":                         sv("44"),
				"kafka_lag":                         sv("1000"),
				`redis_queue_length{queue="tasks"}`: sv("5"),
				"_custom_alerts":                    sv("- {recipe: threshold, name: q_high, metric: qd, op: \">\", window: 5m, threshold: \"100:warning\"}\n"),
			},
		},
	}
	counts := map[string]int{}
	cfg.resolveAtWithStats(time.Now(), func(key string, _ ResolvedThreshold) { counts[key]++ })
	want := map[string]int{
		"mysql_connections":                 1,
		"mysql_connections_critical":        1,
		"mysql_threads_running":             2, // the row and its legacy twin
		"kafka_lag":                         1,
		`redis_queue_length{queue="tasks"}`: 1,
		"_custom_alerts":                    1,
	}
	if !reflect.DeepEqual(counts, want) {
		t.Errorf("reported keys %v, want %v", counts, want)
	}
}

func sv(v string) ScheduledValue { return ScheduledValue{Default: v} }

func TestServedRowsBridgeIsTheKeyedResolve(t *testing.T) {
	t.Parallel()
	if _, ok := servedrows.ResolveAt.(func(*ThresholdConfig, time.Time, func(string, ResolvedThreshold)) []ResolvedThreshold); !ok {
		t.Fatalf("servedrows.ResolveAt is %T", servedrows.ResolveAt)
	}
}
