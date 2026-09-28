package config

// ResolveAtWithKeys (#2115) must be the public resolve plus the keys, nothing
// else: pairing a row with its key cannot change a row, add one, drop one, or
// move the cardinality cut — and the key must be the key of THAT row.

import (
	"fmt"
	"math"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// keyedTree reaches every row source the resolver has — base, `:severity`,
// `_critical`, dimensional (exact and regex), declared, a #1231 alias (and so
// a legacy twin), custom alerts, a schedule — with tenant-a over the
// cardinality cap, so the sorted-and-cut path runs with keys too.
var keyedTree = map[string]string{
	"conf.d/_defaults.yaml": `defaults:
  mysql_connections: 80
  mysql_threads_running: 30
  redis_memory: 70
  container_memory: 85
optional_overrides:
  - kafka_lag
max_metrics_per_tenant: 6
`,
	"conf.d/tenant-a.yaml": `tenants:
  tenant-a:
    mysql_connections: "70:critical"
    mysql_cpu: 44
    mysql_cpu{version="v2"}: 7
    kafka_lag: 1000
    redis_queue_length{queue="tasks"}: "5"
    redis_queue_length{queue=~"t.*"}: "6:critical"
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

func loadKeyedTree(t *testing.T) *ThresholdConfig {
	t.Helper()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, keyedTree)
	cfg, _, err := LoadDir(filepath.Join(tmp, "conf.d"), nil)
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

func rowStrings(rows []ResolvedThreshold) []string {
	out := make([]string, len(rows))
	for i, r := range rows {
		out[i] = fmt.Sprintf("%#v", r)
	}
	sort.Strings(out)
	return out
}

func TestResolveAtWithKeys_SameRowsAndStatsAsPublicResolve(t *testing.T) {
	t.Parallel()
	cfg := loadKeyedTree(t)
	for _, at := range []string{"2026-07-01T03:00:00Z", "2026-07-01T12:00:00Z"} {
		now, _ := time.Parse(time.RFC3339, at)
		pub, pubStats := cfg.ResolveAtWithStats(now)
		keyed, keyedStats, err := cfg.ResolveAtWithKeys(now)
		if err != nil {
			t.Fatalf("%s: %v", at, err)
		}
		rows := make([]ResolvedThreshold, len(keyed))
		for i, k := range keyed {
			rows[i] = k.ResolvedThreshold
		}
		// Tenants are walked in map order, so compare as sets of rows.
		if got, want := rowStrings(rows), rowStrings(pub); !reflect.DeepEqual(got, want) {
			t.Errorf("%s: keyed rows differ from ResolveAtWithStats:\n got %v\nwant %v", at, got, want)
		}
		if !reflect.DeepEqual(keyedStats.PerTenantOverLimit, pubStats.PerTenantOverLimit) ||
			!reflect.DeepEqual(keyedStats.PerTenantCustomAlertErrors, pubStats.PerTenantCustomAlertErrors) ||
			!reflect.DeepEqual(keyedStats.PerTenantDeprecatedKeys, pubStats.PerTenantDeprecatedKeys) {
			t.Errorf("%s: stats differ: keyed %+v, public %+v", at, keyedStats, pubStats)
		}
		if pubStats.PerTenantOverLimit["tenant-a"] == 0 {
			t.Errorf("%s: tenant-a should be over the cap so the cut runs with keys", at)
		}
	}
}

// expectedIdentity is the test's oracle: what a row served for key must look
// like, derived from the key with the resolver's own parsers.
func expectedIdentity(key string) (component, metric string, custom, regex map[string]string, critical bool) {
	if key == customAlertsKey {
		return "custom", "", nil, nil, false
	}
	base, custom, regex := parseKeyWithLabels(key)
	if b, ok := strings.CutSuffix(base, criticalSuffix); ok && len(custom) == 0 && len(regex) == 0 {
		base, critical = b, true
	}
	component, metric = parseMetricKey(base)
	return component, metric, custom, regex, critical
}

// TestResolveAtWithKeys_EveryKeyDescribesItsRow checks, row by row on every
// tenant (the over-cap one included), that the key the resolver paired a row
// with parses to that row's component/metric/labels — so a key that slid onto
// a neighbouring row is caught even when the values happen to line up.
func TestResolveAtWithKeys_EveryKeyDescribesItsRow(t *testing.T) {
	t.Parallel()
	cfg := loadKeyedTree(t)
	for _, at := range []string{"2026-07-01T03:00:00Z", "2026-07-01T12:00:00Z"} {
		now, _ := time.Parse(time.RFC3339, at)
		keyed, _, err := cfg.ResolveAtWithKeys(now)
		if err != nil {
			t.Fatal(err)
		}
		checkedOverCap := 0
		for _, k := range keyed {
			component, metric, custom, regex, critical := expectedIdentity(k.Key)
			if k.legacyTwinOf != "" {
				// A #1231 twin carries its key's value under the legacy name.
				legacy, ok := LegacySpellingFor(k.legacyTwinOf)
				if !ok {
					t.Errorf("%s: twin of %q has no legacy spelling", at, k.legacyTwinOf)
					continue
				}
				component, metric = parseMetricKey(legacy)
			}
			switch {
			case k.Key == customAlertsKey:
				if k.Component != "custom" {
					t.Errorf("%s: %q paired with a non-custom row %+v", at, k.Key, k.ResolvedThreshold)
				}
			default:
				if k.Component != component || k.Metric != metric ||
					!reflect.DeepEqual(nilIfEmpty(k.CustomLabels), nilIfEmpty(custom)) ||
					!reflect.DeepEqual(nilIfEmpty(k.RegexLabels), nilIfEmpty(regex)) ||
					(critical && k.Severity != "critical") {
					t.Errorf("%s: key %q paired with row %+v", at, k.Key, k.ResolvedThreshold)
				}
			}
			if k.Tenant == "tenant-a" {
				checkedOverCap++
			}
		}
		if checkedOverCap == 0 {
			t.Errorf("%s: no row of the over-cap tenant was checked", at)
		}
	}
}

func nilIfEmpty(m map[string]string) map[string]string {
	if len(m) == 0 {
		return nil
	}
	return m
}

// TestResolveAtWithKeys_ReportsTheServedKey pins what the keys are: the whole
// canonical key, the legacy twin under its canonical key, and custom alerts
// under `_custom_alerts`.
func TestResolveAtWithKeys_ReportsTheServedKey(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults:            map[string]float64{"mysql_connections": 80, "mysql_threads_running": 30},
		OptionalOverrides:   []string{"kafka_lag"},
		MaxMetricsPerTenant: -1,
		Tenants: map[string]map[string]ScheduledValue{
			"tenant-a": {
				"mysql_connections_critical":        {Default: "95"},
				"mysql_cpu":                         {Default: "44"},
				"kafka_lag":                         {Default: "1000"},
				`redis_queue_length{queue="tasks"}`: {Default: "5"},
				"_custom_alerts":                    {Default: "- {recipe: threshold, name: q_high, metric: qd, op: \">\", window: 5m, threshold: \"100:warning\"}\n"},
			},
		},
	}
	keyed, _, err := cfg.ResolveAtWithKeys(time.Now())
	if err != nil {
		t.Fatal(err)
	}
	counts := map[string]int{}
	for _, k := range keyed {
		counts[k.Key]++
	}
	want := map[string]int{
		"mysql_connections":                 1,
		"mysql_connections_critical":        1,
		"mysql_threads_running":             2, // the row and its legacy twin
		"kafka_lag":                         1,
		`redis_queue_length{queue="tasks"}`: 1,
		"_custom_alerts":                    1,
	}
	if !reflect.DeepEqual(counts, want) {
		t.Errorf("keys %v, want %v", counts, want)
	}
}

// --- checkKeyed: the guard ResolveAtWithKeys runs before returning ----------

func sampleRows() []ResolvedThreshold {
	return []ResolvedThreshold{
		{Tenant: "tenant-a", Component: "mysql", Metric: "connections", Value: 70, Severity: "warning"},
		{Tenant: "tenant-a", Component: "redis", Metric: "memory", Value: 55, Severity: "critical"},
	}
}

func pairUp(rows []ResolvedThreshold, keys ...string) []KeyedThreshold {
	out := make([]KeyedThreshold, len(rows))
	for i := range rows {
		out[i] = KeyedThreshold{Key: keys[i], ResolvedThreshold: rows[i]}
	}
	return out
}

func TestCheckKeyed_AcceptsOnePairPerRow(t *testing.T) {
	t.Parallel()
	rows := sampleRows()
	if err := checkKeyed(rows, pairUp(rows, "mysql_connections", "redis_memory")); err != nil {
		t.Fatal(err)
	}
}

func TestCheckKeyed_RejectsAMissingPair(t *testing.T) {
	t.Parallel()
	rows := sampleRows()
	err := checkKeyed(rows, pairUp(rows[:1], "mysql_connections"))
	if err == nil || !strings.Contains(err.Error(), "named the key of 1 rows but returned 2") {
		t.Fatalf("err = %v, want the count mismatch", err)
	}
}

func TestCheckKeyed_RejectsAPairForAnotherRow(t *testing.T) {
	t.Parallel()
	rows := sampleRows()
	swapped := []ResolvedThreshold{rows[1], rows[0]}
	err := checkKeyed(rows, pairUp(swapped, "redis_memory", "mysql_connections"))
	if err == nil || !strings.Contains(err.Error(), "is not the row returned in its place") {
		t.Fatalf("err = %v, want the row mismatch", err)
	}
	// Value alone differs: still a different row.
	other := sampleRows()
	other[1].Value = 56
	if err := checkKeyed(rows, pairUp(other, "mysql_connections", "redis_memory")); err == nil {
		t.Fatal("a pair whose value differs was accepted")
	}
}

func TestCheckKeyed_NaNRowEqualsItself(t *testing.T) {
	t.Parallel()
	rows := sampleRows()
	rows[0].Value = math.NaN()
	if err := checkKeyed(rows, pairUp(rows, "mysql_connections", "redis_memory")); err != nil {
		t.Fatalf("a NaN threshold made its own row unequal: %v", err)
	}
}

func TestResolveAtWithKeys_NaNThreshold(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants:  map[string]map[string]ScheduledValue{"tenant-a": {"mysql_connections": {Default: "NaN"}}},
	}
	keyed, _, err := cfg.ResolveAtWithKeys(time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if len(keyed) != 1 || !math.IsNaN(keyed[0].Value) || keyed[0].Key != "mysql_connections" {
		t.Fatalf("keyed = %+v, want one NaN row for mysql_connections", keyed)
	}
}
