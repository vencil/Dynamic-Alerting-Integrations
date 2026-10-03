package main

// served_schedules_test.go — served-values' `schedules` and `aliases`
// (#2115 (c)).

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// scheduleTree carries a schedule on every layer and every row shape: the
// platform `tenants:` block, a profile, a tenant replacing the profile's
// schedule with a scalar, a cross-midnight window, overlapping windows (the
// first wins), a window that does not parse, `disable` and `N:critical`
// windows, a time-boxed override past and before its `expires:`, an
// `expires:` the exporter does not honour (`_critical`), a dimensional key, a
// retired spelling, and keys with no schedule at all.
var scheduleTree = map[string]string{
	"_defaults.yaml": `defaults:
  mysql_connections: 80
  mysql_threads_running: 30
  redis_memory: 70
  container_cpu: 75
  container_memory: 85
tenants:
  tenant-p:
    redis_memory:
      default: "60"
      overrides:
        - window: "02:00-04:00"
          value: "65"
`,
	"_profiles.yaml": `profiles:
  gold:
    container_cpu:
      default: "77"
      overrides:
        - window: "09:00-17:00"
          value: "78"
`,
	"tenant-a.yaml": `tenants:
  tenant-a:
    mysql_connections:
      default: "70"
      overrides:
        - window: "22:00-06:00"
          value: "1000"
        - window: "01:00-03:00"
          value: "5"
        - window: "25:00-26:00"
          value: "9"
        - window: "05:00-07:00"
          value: "1000"
        - window: "12:00-13:00"
          value: "disable"
        - window: "15:00-16:00"
          value: "700:critical"
    mysql_cpu:
      default: "40"
      overrides:
        - window: "08:00-09:00"
          value: "45"
    redis_memory:
      default: "95"
      overrides:
        - window: "10:00-11:00"
          value: "96"
      expires: "2026-06-01T00:00:00Z"
      reason: "incident"
    container_memory:
      default: "88"
      overrides:
        - window: "20:00-21:00"
          value: "89"
      expires: "2026-12-01T00:00:00Z"
    redis_memory_critical:
      default: "disable"
      overrides:
        - window: "19:00-02:00"
          value: "99"
      expires: "2026-06-01T00:00:00Z"
    redis_queue_length{queue="tasks"}:
      default: "5"
      overrides:
        - window: "06:30-06:45"
          value: "6:critical"
`,
	"tenant-b.yaml": "tenants:\n  tenant-b:\n    _profile: gold\n",
	"tenant-c.yaml": "tenants:\n  tenant-c:\n    _profile: gold\n    container_cpu: 50\n",
	"tenant-p.yaml": "tenants:\n  tenant-p:\n    mysql_connections: 81\n",
	// Same number all day, the severity alone changing in a window; and a
	// key switched off all day with an `expires:` still ahead — served in no
	// part of the day, yet listed for its time-box.
	"tenant-d.yaml": `tenants:
  tenant-d:
    mysql_connections:
      default: "700"
      overrides:
        - window: "10:00-11:00"
          value: "700:critical"
    redis_memory:
      default: "disable"
      expires: "2026-12-01T00:00:00Z"
`,
}

const scheduleAt = "2026-07-01T03:00:00Z"

// servedS is served with --schedules.
func servedS(t *testing.T, files map[string]string, at string) (int, servedOut, string, string) {
	t.Helper()
	return servedWith(t, files, at, "--schedules")
}

type seg struct {
	From, To string
	Value    any
	Severity string
}

func segsOf(t *testing.T, doc servedOut, tenant, key string) []seg {
	t.Helper()
	sch, ok := doc.Tenants[tenant].Schedules[key]
	if !ok {
		t.Fatalf("tenant %s: schedules[%q] missing; schedules=%v", tenant, key, keysOf(doc.Tenants[tenant].Schedules))
	}
	out := make([]seg, 0, len(sch.Segments))
	for _, s := range sch.Segments {
		out = append(out, seg{s.From, s.To, s.Value, s.Severity})
	}
	return out
}

// TestServedSchedules_Exact pins the day of each key of scheduleTree. It is
// what fails when neighbouring pieces with one reading are left unmerged, or
// a later window wins over an earlier one.
func TestServedSchedules_Exact(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := servedS(t, scheduleTree, scheduleAt)
	mustOK(t, code, stderr)
	const w, c = "warning", "critical"
	cases := []struct {
		tenant, key string
		want        []seg
	}{
		{"tenant-a", "mysql_connections", []seg{
			{"00:00", "07:00", 1000.0, w}, {"07:00", "12:00", 70.0, w}, {"12:00", "13:00", nil, ""},
			{"13:00", "15:00", 70.0, w}, {"15:00", "16:00", 700.0, c}, {"16:00", "22:00", 70.0, w},
			{"22:00", "24:00", 1000.0, w},
		}},
		// Written as mysql_cpu: keyed by the canonical spelling.
		{"tenant-a", "mysql_threads_running", []seg{
			{"00:00", "08:00", 40.0, w}, {"08:00", "09:00", 45.0, w}, {"09:00", "24:00", 40.0, w},
		}},
		// Past its expires: the platform default all day, schedule and all.
		{"tenant-a", "redis_memory", []seg{{"00:00", "24:00", 70.0, w}}},
		{"tenant-a", "container_memory", []seg{
			{"00:00", "20:00", 88.0, w}, {"20:00", "21:00", 89.0, w}, {"21:00", "24:00", 88.0, w},
		}},
		{"tenant-a", "redis_memory_critical", []seg{
			{"00:00", "02:00", 99.0, c}, {"02:00", "19:00", nil, ""}, {"19:00", "24:00", 99.0, c},
		}},
		{"tenant-a", `redis_queue_length{queue="tasks"}`, []seg{
			{"00:00", "06:30", 5.0, w}, {"06:30", "06:45", 6.0, c}, {"06:45", "24:00", 5.0, w},
		}},
		{"tenant-a", "container_cpu", []seg{{"00:00", "24:00", 75.0, w}}},
		{"tenant-p", "redis_memory", []seg{
			{"00:00", "02:00", 60.0, w}, {"02:00", "04:00", 65.0, w}, {"04:00", "24:00", 60.0, w},
		}},
		{"tenant-p", "mysql_connections", []seg{{"00:00", "24:00", 81.0, w}}},
		{"tenant-b", "container_cpu", []seg{
			{"00:00", "09:00", 77.0, w}, {"09:00", "17:00", 78.0, w}, {"17:00", "24:00", 77.0, w},
		}},
		// The tenant's scalar replaces the profile's schedule whole.
		{"tenant-c", "container_cpu", []seg{{"00:00", "24:00", 50.0, w}}},
		{"tenant-b", "mysql_connections", []seg{{"00:00", "24:00", 80.0, w}}},
		// Same value, other severity: not merged.
		{"tenant-d", "mysql_connections", []seg{
			{"00:00", "10:00", 700.0, w}, {"10:00", "11:00", 700.0, c}, {"11:00", "24:00", 700.0, w},
		}},
		// Never served, listed for its expires.
		{"tenant-d", "redis_memory", []seg{{"00:00", "24:00", nil, ""}}},
	}
	for _, tc := range cases {
		if got := segsOf(t, doc, tc.tenant, tc.key); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s %s:\n got %v\nwant %v", tc.tenant, tc.key, got, tc.want)
		}
	}
	if _, ok := doc.Tenants["tenant-a"].Schedules["mysql_cpu"]; ok {
		t.Error("schedules carries the retired spelling mysql_cpu")
	}
	if _, ok := doc.Tenants["tenant-a"].Schedules["_custom_alerts"]; ok {
		t.Error("schedules carries _custom_alerts")
	}
}

// TestServedSchedules_Expiry: expires / expired only where the exporter
// honours the time-box (a base key), the verdict at --at.
func TestServedSchedules_Expiry(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := servedS(t, scheduleTree, scheduleAt)
	mustOK(t, code, stderr)
	sch := doc.Tenants["tenant-a"].Schedules
	type exp struct {
		Expires string
		Expired *bool
	}
	yes, no := true, false
	for key, want := range map[string]exp{
		"redis_memory":          {"2026-06-01T00:00:00Z", &yes},
		"container_memory":      {"2026-12-01T00:00:00Z", &no},
		"redis_memory_critical": {"", nil}, // not honoured on _critical
		"mysql_connections":     {"", nil},
	} {
		got := exp{sch[key].Expires, sch[key].Expired}
		if got.Expires != want.Expires || (got.Expired == nil) != (want.Expired == nil) ||
			(got.Expired != nil && *got.Expired != *want.Expired) {
			t.Errorf("%s: expires %q expired %v, want %q %v", key, got.Expires, got.Expired, want.Expires, want.Expired)
		}
	}
	// Switched off all day, time-boxed until December: listed, not expired.
	if d, ok := doc.Tenants["tenant-d"].Schedules["redis_memory"]; !ok || d.Expires != "2026-12-01T00:00:00Z" ||
		d.Expired == nil || *d.Expired {
		t.Errorf("tenant-d redis_memory: %+v, want expires 2026-12-01T00:00:00Z, expired false", d)
	}
}

// TestServedSchedules_AgreeWithServedValuesEveryMinute: for each of the 1440
// minutes of the day, what the schedules of one run say is served at that
// minute is exactly what the exporter's /metrics reading (keyedRows, the
// reading `values` comes from) serves at that minute — every threshold key,
// value and severity, and no other key.
func TestServedSchedules_AgreeWithServedValuesEveryMinute(t *testing.T) {
	t.Parallel()
	code, doc, dir, stderr := servedS(t, scheduleTree, scheduleAt)
	mustOK(t, code, stderr)
	cfg, _, err := config.LoadDir(dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	day := time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
	for m := 0; m < config.MinutesPerDay; m++ {
		now := day.Add(time.Duration(m) * time.Minute)
		served, _, _, err := keyedRows(cfg, now)
		if err != nil {
			t.Fatalf("%s: %v", hhmm(m), err)
		}
		for tenant, tv := range doc.Tenants {
			want := map[string]string{}
			for key, rows := range served[tenant] {
				if key == customAlertsKey {
					continue
				}
				r, err := keyReading(tenant, key, rows)
				if err != nil {
					t.Fatal(err)
				}
				want[key] = fmt.Sprintf("%v|%s", r.value, r.severity)
			}
			got := map[string]string{}
			for key, sch := range tv.Schedules {
				for _, s := range sch.Segments {
					if hhmm(m) >= s.From && hhmm(m) < s.To && s.Value != nil {
						got[key] = fmt.Sprintf("%v|%s", s.Value, s.Severity)
					}
				}
			}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("%s tenant %s: schedules say %v, /metrics serves %v", hhmm(m), tenant, got, want)
			}
		}
	}
}

// TestServedSchedules_CoverTheDay: every key's segments run from 00:00 to
// 24:00 with no gap, and every threshold key of values has a schedule.
func TestServedSchedules_CoverTheDay(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := servedS(t, scheduleTree, scheduleAt)
	mustOK(t, code, stderr)
	for tenant, tv := range doc.Tenants {
		for key := range tv.Severities {
			if _, ok := tv.Schedules[key]; !ok {
				t.Errorf("tenant %s: %q is in severities but has no schedule", tenant, key)
			}
		}
		for key, sch := range tv.Schedules {
			s := sch.Segments
			if len(s) == 0 || s[0].From != "00:00" || s[len(s)-1].To != "24:00" {
				t.Errorf("tenant %s %s: segments %v do not cover the day", tenant, key, s)
				continue
			}
			for i := 1; i < len(s); i++ {
				if s[i].From != s[i-1].To {
					t.Errorf("tenant %s %s: gap between %v and %v", tenant, key, s[i-1], s[i])
				}
			}
		}
	}
}

// TestServedSchedules_ScalarTreeIsOneSegment: a tree without schedules gives
// each key one segment, the value at --at.
func TestServedSchedules_ScalarTreeIsOneSegment(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := servedS(t, map[string]string{
		"_defaults.yaml": defaultsOnly,
		"tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_connections: \"70:critical\"\n",
	}, scheduleAt)
	mustOK(t, code, stderr)
	if got, want := segsOf(t, doc, "tenant-a", "mysql_connections"), []seg{{"00:00", "24:00", 70.0, "critical"}}; !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

// TestServedValues_Aliases: the document carries the exporter's alias table.
func TestServedValues_Aliases(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{"_defaults.yaml": defaultsOnly, "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 1\n"}, scheduleAt)
	mustOK(t, code, stderr)
	if !reflect.DeepEqual(doc.Aliases, config.DeprecatedKeyAliases()) || len(doc.Aliases) == 0 {
		t.Errorf("aliases = %v, want %v", doc.Aliases, config.DeprecatedKeyAliases())
	}
}

// ungatherableTree: tenant-a's two keys give one series (severity "critical"
// for mysql_connections) only from 15:00 to 16:00, so /metrics cannot be
// gathered then — for any tenant. redis_memory is switched off from 01:00
// to 02:00, for a not-served segment beside the error one.
var ungatherableTree = map[string]string{
	"_defaults.yaml": defaultsOnly + "  redis_memory: 70\n",
	"tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections_critical: 95\n    mysql_connections:\n" +
		"      default: \"70\"\n      overrides:\n        - window: \"15:00-16:00\"\n          value: \"700:critical\"\n" +
		"    redis_memory:\n      default: \"60\"\n      overrides:\n        - window: \"01:00-02:00\"\n          value: disable\n",
	"tenant-b.yaml": "tenants:\n  tenant-b:\n    mysql_connections: 80\n",
}

// TestServedSchedules_UngatherableSegmentCarriesTheError: a part of the day
// other than --at's in which /metrics cannot be gathered does not fail the
// run; every key of every tenant carries that part as a segment with the
// Gather error and no value or severity. At an --at inside that part the run
// exits 2, as it always did.
func TestServedSchedules_UngatherableSegmentCarriesTheError(t *testing.T) {
	t.Parallel()
	code, _, dir, stderr := servedS(t, ungatherableTree, scheduleAt)
	mustOK(t, code, stderr)
	// Decoded loosely, so a field that is absent can be told from a null one.
	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir, "--at", scheduleAt, "--schedules")
	mustOK(t, code, stderr)
	var doc struct {
		Tenants map[string]struct {
			Schedules map[string]struct {
				Segments []map[string]any `json:"segments"`
			} `json:"schedules"`
		} `json:"tenants"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatal(err)
	}
	row := func(from, to string, v float64, sev string) map[string]any {
		return map[string]any{"from": from, "to": to, "value": v, "severity": sev}
	}
	errSeg := map[string]any{"from": "15:00", "to": "16:00"}
	for _, tc := range []struct {
		tenant, key string
		want        []map[string]any
	}{
		{"tenant-a", "mysql_connections", []map[string]any{
			row("00:00", "15:00", 70, "warning"), errSeg, row("16:00", "24:00", 70, "warning")}},
		{"tenant-a", "mysql_connections_critical", []map[string]any{
			row("00:00", "15:00", 95, "critical"), errSeg, row("16:00", "24:00", 95, "critical")}},
		{"tenant-a", "redis_memory", []map[string]any{
			row("00:00", "01:00", 60, "warning"), {"from": "01:00", "to": "02:00", "value": nil},
			row("02:00", "15:00", 60, "warning"), errSeg, row("16:00", "24:00", 60, "warning")}},
		{"tenant-b", "mysql_connections", []map[string]any{
			row("00:00", "15:00", 80, "warning"), errSeg, row("16:00", "24:00", 80, "warning")}},
	} {
		got := doc.Tenants[tc.tenant].Schedules[tc.key].Segments
		for i, s := range got {
			e, has := s["error"]
			if i < len(tc.want) && reflect.DeepEqual(tc.want[i], errSeg) {
				text, _ := e.(string)
				if !has || text == "" {
					t.Errorf("%s %s: segment %v carries no error", tc.tenant, tc.key, s)
				} else if !strings.Contains(text, "HTTP 500") || !strings.Contains(text, "was collected before with the same name and label values") {
					t.Errorf("%s %s: error %q does not carry the Gather failure", tc.tenant, tc.key, text)
				}
			} else if has {
				t.Errorf("%s %s: segment %v carries an error", tc.tenant, tc.key, s)
			}
			delete(s, "error")
		}
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s %s (error text removed):\n got %v\nwant %v", tc.tenant, tc.key, got, tc.want)
		}
	}

	code, _, _, stderr = servedS(t, ungatherableTree, "2026-07-01T15:30:00Z")
	if code != exitCallerErr || !strings.Contains(stderr, "HTTP 500") {
		t.Errorf("--at inside the window: exit %d, stderr %q, want exit %d", code, stderr, exitCallerErr)
	}
}

// TestServedValues_SchedulesAreOptIn: without --schedules there is no
// `schedules` field at all and the rest of the document is what it is with
// the flag (aliases included); with it, every tenant carries one.
func TestServedValues_SchedulesAreOptIn(t *testing.T) {
	t.Parallel()
	dir := filepath.Join(t.TempDir(), "conf.d")
	tree := map[string]string{}
	for k, v := range scheduleTree {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, filepath.Dir(dir), tree)
	decode := func(extra ...string) map[string]any {
		t.Helper()
		code, stdout, stderr := runOnce(t, append([]string{servedValuesCmd, "--config-dir", dir, "--at", scheduleAt}, extra...)...)
		mustOK(t, code, stderr)
		var doc map[string]any
		if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
			t.Fatal(err)
		}
		return doc
	}
	without, with := decode(), decode("--schedules")
	if _, ok := without["aliases"]; !ok {
		t.Error("aliases missing without --schedules")
	}
	tenantsWith := with["tenants"].(map[string]any)
	for name, tv := range without["tenants"].(map[string]any) {
		if _, ok := tv.(map[string]any)["schedules"]; ok {
			t.Errorf("tenant %s carries schedules without --schedules", name)
		}
		w := tenantsWith[name].(map[string]any)
		if _, ok := w["schedules"]; !ok {
			t.Errorf("tenant %s carries no schedules with --schedules", name)
		}
		delete(w, "schedules")
		if !reflect.DeepEqual(tv, w) {
			t.Errorf("tenant %s: the document differs beyond schedules:\n without %v\n with    %v", name, tv, w)
		}
	}
	delete(with, "tenants")
	delete(without, "tenants")
	if !reflect.DeepEqual(with, without) {
		t.Errorf("top level differs: %v vs %v", without, with)
	}
}
