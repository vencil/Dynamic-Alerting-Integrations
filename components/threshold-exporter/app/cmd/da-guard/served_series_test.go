package main

// served_series_test.go — served-values' `series` (#2750): which /metrics
// series each threshold key is.
//
// The oracle is production /metrics itself: the exporter's collector
// (scrape.NewCollectorWithHooks with only the clock set, so it resolves with
// ResolveAtWithStats and builds with thresholdmetric.Emit, unreported),
// registered by scrape.Register and gathered. TestServedSeries_AreTheGather
// holds served-values' identities to that Gather in both directions;
// TestServedSeries_Shapes pins the identity of each key shape.

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/internal/scrape"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// seriesOut is the `series` part of the document as a reader decodes it.
type seriesOut struct {
	Tenants map[string]struct {
		Values     map[string]any                 `json:"values"`
		Severities map[string]string              `json:"severities"`
		Unserved   map[string]any                 `json:"unserved"`
		Series     map[string][]seriesIdentityOut `json:"series"`
		Schedules  map[string]struct {
			Segments []struct {
				From     string              `json:"from"`
				To       string              `json:"to"`
				Value    any                 `json:"value"`
				Severity string              `json:"severity"`
				Series   []seriesIdentityOut `json:"series"`
				Error    string              `json:"error"`
			} `json:"segments"`
		} `json:"schedules"`
	} `json:"tenants"`
}

type seriesIdentityOut struct {
	Name            string            `json:"name"`
	Labels          map[string]string `json:"labels"`
	MetricKey       string            `json:"metric_key"`
	Dimensions      map[string]string `json:"dimensions"`
	DimensionsRegex map[string]string `json:"dimensions_regex"`
}

// id is a series' identity on /metrics: its name and full label set.
func (s seriesIdentityOut) id() string {
	return identity(s.Name, s.Labels)
}

func identity(name string, labels map[string]string) string {
	pairs := make([]string, 0, len(labels))
	for k, v := range labels {
		pairs = append(pairs, fmt.Sprintf("%s=%q", k, v))
	}
	sort.Strings(pairs)
	return name + "{" + strings.Join(pairs, ",") + "}"
}

// seriesDoc runs served-values (with extra flags) over files and decodes it.
func seriesDoc(t *testing.T, files map[string]string, at string, extra ...string) (seriesOut, string) {
	t.Helper()
	code, _, dir, stderr := servedWith(t, files, at, extra...)
	mustOK(t, code, stderr)
	args := append([]string{servedValuesCmd, "--config-dir", dir, "--at", at}, extra...)
	code, stdout, stderr := runOnce(t, args...)
	mustOK(t, code, stderr)
	var doc seriesOut
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("stdout is not JSON: %v\n%s", err, stdout)
	}
	return doc, dir
}

// gatheredThresholds is production /metrics over the tree at dir at `at`:
// every series of the family the exporter's user_threshold builder emits,
// per tenant, as identity → the series' labels.
func gatheredThresholds(t *testing.T, dir string, at time.Time) map[string]map[string]map[string]string {
	t.Helper()
	cfg, _, err := config.LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	reg := prometheus.NewRegistry()
	scrape.Register(reg, scrape.NewCollectorWithHooks(staticSource{cfg}, scrape.Hooks{Now: func() time.Time { return at }}),
		scrape.NewConfigMetrics())
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("production /metrics cannot be gathered at %s: %v", at, err)
	}
	out := map[string]map[string]map[string]string{}
	for _, mf := range mfs {
		if mf.GetName() != "user_threshold" {
			continue
		}
		for _, m := range mf.GetMetric() {
			labels := map[string]string{}
			for _, lp := range m.GetLabel() {
				labels[lp.GetName()] = lp.GetValue()
			}
			tenant := labels["tenant"]
			if out[tenant] == nil {
				out[tenant] = map[string]map[string]string{}
			}
			out[tenant][identity(mf.GetName(), labels)] = labels
		}
	}
	return out
}

// checkAgainstGather: served (tenant → key → series served at `at`) and the
// Gather are the same series, both ways. A Gather series served-values does
// not name must be one of the tenant's `_custom_alerts` rows (custom, the
// rows each tenant's values list) — matched by metric, severity and labels,
// one for one.
func checkAgainstGather(t *testing.T, what string, served map[string]map[string][]seriesIdentityOut,
	custom map[string][]map[string]any, gathered map[string]map[string]map[string]string,
) {
	t.Helper()
	named := 0
	for tenant, keys := range served {
		for key, list := range keys {
			if len(list) == 0 {
				t.Errorf("%s: tenant %s: key %q has an empty series list", what, tenant, key)
			}
			for _, s := range list {
				named++
				if _, ok := gathered[tenant][s.id()]; !ok {
					t.Errorf("%s: tenant %s: key %q names %s, which production /metrics does not serve", what, tenant, key, s.id())
				}
				if s.Labels["tenant"] != tenant {
					t.Errorf("%s: tenant %s: key %q names %s, another tenant's series", what, tenant, key, s.id())
				}
			}
		}
	}
	if named == 0 {
		t.Fatalf("%s: served-values names no series (vacuous)", what)
	}
	for tenant, ids := range gathered {
		rest := map[string]map[string]string{}
		for id, labels := range ids {
			found := false
			for _, list := range served[tenant] {
				for _, s := range list {
					found = found || s.id() == id
				}
			}
			if !found {
				rest[id] = labels
			}
		}
		rows := custom[tenant]
		if len(rest) != len(rows) {
			t.Errorf("%s: tenant %s: production /metrics serves %d series served-values names under no key, it lists %d custom-alert rows: %v",
				what, tenant, len(rest), len(rows), keysOf(rest))
			continue
		}
		for id, labels := range rest {
			match := 0
			for _, r := range rows {
				ok := labels["metric"] == r["metric"] && labels["severity"] == r["severity"]
				for k, v := range r["labels"].(map[string]any) {
					ok = ok && labels[k] == v
				}
				if ok {
					match++
				}
			}
			if match != 1 {
				t.Errorf("%s: tenant %s: production series %s is %d of the custom-alert rows, want 1", what, tenant, id, match)
			}
		}
	}
}

// TestServedSeries_AreTheGather is the contrast test: for each tree, at
// `--at` and at the first and last minute of every schedule segment, every
// series served-values names is a series production /metrics serves at that
// instant, and every user_threshold series production /metrics serves is one
// served-values names (or a custom-alert row).
func TestServedSeries_AreTheGather(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name  string
		files map[string]string
		at    string
	}{
		{"shapes", seriesTree, seriesAt},
		{"schedule tree", scheduleTree, scheduleAt},
		{"consistency tree", consistencyTree, "2026-07-01T03:00:00Z"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			doc, dir := seriesDoc(t, tc.files, tc.at, "--schedules")
			at, _ := time.Parse(time.RFC3339, tc.at)
			custom := map[string][]map[string]any{}
			atSeries := map[string]map[string][]seriesIdentityOut{}
			for tenant, tv := range doc.Tenants {
				if !reflect.DeepEqual(keysOf(tv.Series), keysOf(tv.Severities)) {
					t.Errorf("tenant %s: series keys %v, severities keys %v", tenant, keysOf(tv.Series), keysOf(tv.Severities))
				}
				atSeries[tenant] = tv.Series
				if list, ok := tv.Values[customAlertsKey].([]any); ok {
					for _, e := range list {
						custom[tenant] = append(custom[tenant], e.(map[string]any))
					}
				}
			}
			checkAgainstGather(t, "--at "+tc.at, atSeries, custom, gatheredThresholds(t, dir, at))

			day := time.Date(at.Year(), at.Month(), at.Day(), 0, 0, 0, 0, time.UTC)
			minutes := map[int]bool{}
			for _, tv := range doc.Tenants {
				for _, sch := range tv.Schedules {
					for _, s := range sch.Segments {
						minutes[minuteOf(t, s.From)] = true
						minutes[minuteOf(t, s.To)-1] = true
					}
				}
			}
			if len(minutes) < 3 {
				t.Fatalf("schedules cut the day at %d minutes (vacuous)", len(minutes))
			}
			for m := range minutes {
				segSeries := map[string]map[string][]seriesIdentityOut{}
				for tenant, tv := range doc.Tenants {
					segSeries[tenant] = map[string][]seriesIdentityOut{}
					for key, sch := range tv.Schedules {
						for _, s := range sch.Segments {
							if minuteOf(t, s.From) <= m && m < minuteOf(t, s.To) && s.Value != nil {
								segSeries[tenant][key] = s.Series
							}
						}
					}
				}
				instant := day.Add(time.Duration(m) * time.Minute)
				checkAgainstGather(t, "segment minute "+hhmm(m), segSeries, custom, gatheredThresholds(t, dir, instant))
			}
		})
	}
}

func minuteOf(t *testing.T, s string) int {
	t.Helper()
	var h, m int
	if _, err := fmt.Sscanf(s, "%d:%d", &h, &m); err != nil {
		t.Fatalf("not HH:MM: %q", s)
	}
	return h*60 + m
}

// seriesTree has one key of each shape the identity must tell apart.
var seriesTree = map[string]string{
	"_defaults.yaml": `defaults:
  mysql_connections: 80
  mysql_slow: 90
  mysql_threads_running: 30
  redis_memory: 70
  oracle_tablespace: 90
`,
	"tx.yaml": `tenants:
  tx:
    mysql_connections: "70"
    mysql_connections_critical: "99"
    mysql_slow:
      default: "75"
      overrides:
        - window: "01:00-09:00"
          value: "75:critical"
    mysql_connections{schema="s1"}: "60"
    oracle_tablespace{tablespace=~"SYS.*"}: "85:critical"
    mysql_threads_running: "44"
    redis_memory: disable
`,
}

// seriesAt is inside mysql_slow's critical window.
const seriesAt = "2026-07-01T03:00:00Z"

func ident(severity, component, metric, metricKey string, extra map[string]string, dims, dimsRe map[string]string) seriesIdentityOut {
	labels := map[string]string{"tenant": "tx", "component": component, "metric": metric, "severity": severity}
	for k, v := range extra {
		labels[k] = v
	}
	return seriesIdentityOut{Name: "user_threshold", Labels: labels, MetricKey: metricKey,
		Dimensions: dims, DimensionsRegex: dimsRe}
}

var none = map[string]string{}

// TestServedSeries_Shapes pins each key shape's identity at `--at`, and the
// identity of each segment of a key whose severity a window changes.
func TestServedSeries_Shapes(t *testing.T) {
	t.Parallel()
	doc, _ := seriesDoc(t, seriesTree, seriesAt, "--schedules")
	tx := doc.Tenants["tx"]
	for _, tc := range []struct {
		shape, key string
		want       []seriesIdentityOut // nil: no entry
	}{
		{"plain key, warning", "mysql_connections",
			[]seriesIdentityOut{ident("warning", "mysql", "connections", "mysql_connections", nil, none, none)}},
		{"X_critical: X's series, severity critical", "mysql_connections_critical",
			[]seriesIdentityOut{ident("critical", "mysql", "connections", "mysql_connections", nil, none, none)}},
		{"window serving N:critical, at --at", "mysql_slow",
			[]seriesIdentityOut{ident("critical", "mysql", "slow", "mysql_slow", nil, none, none)}},
		{"dimensional key", `mysql_connections{schema="s1"}`,
			[]seriesIdentityOut{ident("warning", "mysql", "connections", "mysql_connections",
				map[string]string{"schema": "s1"}, map[string]string{"schema": "s1"}, none)}},
		{"regex dimensional key, :critical", `oracle_tablespace{tablespace=~"SYS.*"}`,
			[]seriesIdentityOut{ident("critical", "oracle", "tablespace", "oracle_tablespace",
				map[string]string{"tablespace_re": "SYS.*"}, none, map[string]string{"tablespace": "SYS.*"})}},
		{"alias target: its row, then its legacy twin", "mysql_threads_running",
			[]seriesIdentityOut{
				ident("warning", "mysql", "threads_running", "mysql_threads_running", nil, none, none),
				ident("warning", "mysql", "cpu", "mysql_cpu", nil, none, none)}},
		{"unserved (disable): no entry", "redis_memory", nil},
	} {
		got, ok := tx.Series[tc.key]
		if tc.want == nil {
			if ok {
				t.Errorf("%s: series[%q] = %v, want no entry", tc.shape, tc.key, got)
			}
			if _, unserved := tx.Unserved[tc.key]; !unserved {
				t.Errorf("%s: %q is not in unserved (vacuous)", tc.shape, tc.key)
			}
			continue
		}
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s: series[%q]\n got %+v\nwant %+v", tc.shape, tc.key, got, tc.want)
		}
	}

	// The segments of mysql_slow: the window is another series of the same
	// metric; the rest of the day is the warning one.
	type segID struct {
		From, To string
		Series   []seriesIdentityOut
	}
	var got []segID
	for _, s := range tx.Schedules["mysql_slow"].Segments {
		got = append(got, segID{s.From, s.To, s.Series})
	}
	warn := []seriesIdentityOut{ident("warning", "mysql", "slow", "mysql_slow", nil, none, none)}
	crit := []seriesIdentityOut{ident("critical", "mysql", "slow", "mysql_slow", nil, none, none)}
	want := []segID{{"00:00", "01:00", warn}, {"01:00", "09:00", crit}, {"09:00", "24:00", warn}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("mysql_slow segments\n got %+v\nwant %+v", got, want)
	}
	if s, ok := tx.Schedules["redis_memory"]; ok {
		for _, seg := range s.Segments {
			if seg.Series != nil {
				t.Errorf("redis_memory (disabled all day): segment %s-%s carries series %v", seg.From, seg.To, seg.Series)
			}
		}
	}
}
