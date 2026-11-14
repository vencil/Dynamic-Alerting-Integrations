package config

// layer_schedules_test.go (#3834) — a reserved key written with a schedule
// its readers ignore (#3269) is judged in the file and layer that writes it,
// by the same files, layers and "read in that layer" rule as an unread
// value (LayerValueProblems), and read as written, schedule included.

import (
	"reflect"
	"testing"
)

func TestLayerScheduleProblems_JudgedWhereWritten(t *testing.T) {
	t.Parallel()
	sched := func(key string) string {
		return "    " + key + ":\n      default: disable\n      overrides:\n" +
			"        - {window: \"01:00-02:00\", value: enable}\n"
	}
	rep := loadForValues(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\nprofiles:\n  gold:\n" +
			"    _silent_mode:\n      default: disable\n      overrides:\n" +
			"        - {window: \"01:00-02:00\", value: all}\n" +
			// Not read in a profile (#2914): the verdict's unread notice,
			// no schedule problem on top of it.
			sched("_severity_dedup"),
		// A profile and a tenant entry of one name in one file: each is
		// judged in its own section — the profile's schedule is no
		// platform-entry problem of the tenant ta.
		"_platform.yaml": "tenants:\n  ta:\n" + sched("_severity_dedup") + "    _silent_mode: disable\n" +
			"profiles:\n  ta:\n" + sched("_silent_mode"),
		"ta.yaml": "tenants:\n  ta:\n    _profile: gold\n" +
			"    _state_maintenance: {default: disable, expires: \"2030-01-01T00:00:00Z\"}\n",
		// A threshold key's schedule is read: no problem.
		"tb.yaml": "tenants:\n  tb:\n    mysql_connections:\n      default: \"70\"\n" +
			"      overrides:\n        - {window: \"01:00-02:00\", value: \"90\"}\n",
		// `{default: x}` alone is no schedule (#2433).
		"tc.yaml": "tenants:\n  tc:\n    _silent_mode: {default: disable}\n",
		// Read by a reader other than the exporter (the generator, the
		// annotation), which reads the `default` alone as well.
		"td.yaml": "tenants:\n  td:\n" +
			"    _routing_profile: {default: p1, overrides: [{window: \"01:00-02:00\", value: p2}]}\n" +
			"    _namespaces: {default: ns1, overrides: [{window: \"01:00-02:00\", value: ns2}]}\n",
	})
	got := layerValueRows(rep.ScheduleProblems)
	want := []layerValueRow{
		{"_defaults.yaml", ValueLayerProfile, "gold", "_silent_mode"},
		{"_platform.yaml", ValueLayerPlatform, "ta", "_severity_dedup"},
		{"_platform.yaml", ValueLayerProfile, "ta", "_silent_mode"},
		{"ta.yaml", ValueLayerTenant, "ta", "_state_maintenance"},
		{"td.yaml", ValueLayerTenant, "td", "_namespaces"},
		{"td.yaml", ValueLayerTenant, "td", "_routing_profile"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("schedule problems:\n got %v\nwant %v", got, want)
	}
	for _, p := range rep.ScheduleProblems {
		if !p.Schedule || p.Problem != scheduleProblem || (p.Value != "disable" && p.Owner != "td") {
			t.Errorf("%s %s: Schedule=%v Problem=%q Value=%q", p.File, p.Key, p.Schedule, p.Problem, p.Value)
		}
	}
	// Not a value problem: the value problems stay the reader's own.
	for _, p := range rep.ValueProblems {
		if p.Schedule {
			t.Errorf("ValueProblems holds a schedule problem: %+v", p)
		}
	}
}

// A schedule is read as written: moving a window or changing an expiry is
// a different value (the Ratchet's sameSchedule), so a write doing so makes
// a new problem, not the old one.
func TestLayerScheduleProblems_ReadingIsTheScheduleAsWritten(t *testing.T) {
	t.Parallel()
	reading := func(body string) string {
		rep := loadForValues(t, map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"ta.yaml":        "tenants:\n  ta:\n    _silent_mode:\n" + body,
		})
		if len(rep.ScheduleProblems) != 1 {
			t.Fatalf("%q: %d schedule problems, want 1", body, len(rep.ScheduleProblems))
		}
		return rep.ScheduleProblems[0].Reading
	}
	base := reading("      default: disable\n      overrides:\n        - {window: \"01:00-02:00\", value: all}\n")
	for name, body := range map[string]string{
		"window moved":  "      default: disable\n      overrides:\n        - {window: \"03:00-04:00\", value: all}\n",
		"value changed": "      default: disable\n      overrides:\n        - {window: \"01:00-02:00\", value: warning}\n",
		"window added": "      default: disable\n      overrides:\n        - {window: \"01:00-02:00\", value: all}\n" +
			"        - {window: \"05:00-06:00\", value: all}\n",
		"expiry instead": "      default: disable\n      expires: \"2030-01-01T00:00:00Z\"\n",
	} {
		if r := reading(body); r == base {
			t.Errorf("%s: reading %q equals the original's", name, r)
		}
	}
	// An expiry is compared field for field too: only its date, or only its
	// reason, changed is a different value.
	base = reading("      default: disable\n      expires: \"2030-01-01T00:00:00Z\"\n      reason: r1\n")
	for name, body := range map[string]string{
		"expires changed": "      default: disable\n      expires: \"2031-01-01T00:00:00Z\"\n      reason: r1\n",
		"reason changed":  "      default: disable\n      expires: \"2030-01-01T00:00:00Z\"\n      reason: r2\n",
	} {
		if r := reading(body); r == base {
			t.Errorf("%s: reading %q equals the original's", name, r)
		}
	}
	base = reading("      default: disable\n      overrides:\n        - {window: \"01:00-02:00\", value: all}\n")
	if r := reading("      default: disable\n      overrides:\n        - {window: \"01:00-02:00\", value: all}\n"); r != base {
		t.Errorf("same schedule read twice: %q vs %q", r, base)
	}
}
