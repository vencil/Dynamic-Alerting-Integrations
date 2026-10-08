package config

// #2065: the window_invalid verdict, the rank a recorder keeps per key, and
// recordUnparsed's per-tenant reading of the day.

import (
	"fmt"
	"math/rand"
	"reflect"
	"strings"
	"testing"
)

// TestParseTimeWindow_StartEqualsEndRefused: a window whose start equals its
// end matches no minute, so it is refused and told, like any window that
// never applies — and a window one minute long still parses.
func TestParseTimeWindow_StartEqualsEndRefused(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		window string
		ok     bool
		warn   string
	}{
		{"05:00-05:00", false, "start equals end"},
		{"00:00-00:00", false, "start equals end"},
		{"05:00-05:01", true, ""},
		{"22:00-06:00", true, ""},
		{"01:00~09:00", false, "invalid time window format"},
		{"01:00-24:00", false, "invalid hour"},
		{"", false, "invalid time window format"},
	} {
		var lines []string
		logf := func(f string, a ...any) { lines = append(lines, fmt.Sprintf(f, a...)) }
		_, _, ok := parseTimeWindow(tc.window, logf)
		if ok != tc.ok {
			t.Errorf("parseTimeWindow(%q) ok = %v, want %v", tc.window, ok, tc.ok)
		}
		if tc.ok != (len(lines) == 0) || (tc.warn != "" && (len(lines) != 1 || !strings.Contains(lines[0], tc.warn))) {
			t.Errorf("parseTimeWindow(%q) told %q, want one WARN containing %q", tc.window, lines, tc.warn)
		}
	}
}

// TestRejectRecorder_KeepsTheStrongestVerdict: whatever order a key's
// verdicts arrive in, the recorder keeps dropped > unparsed > window_invalid.
func TestRejectRecorder_KeepsTheStrongestVerdict(t *testing.T) {
	t.Parallel()
	all := []string{NotServedWindowInvalid, NotServedValueUnparsed, NotServedValueUnparsedDropped}
	for _, tc := range []struct {
		seq  []string
		want string
	}{
		{[]string{NotServedWindowInvalid}, NotServedWindowInvalid},
		{[]string{NotServedWindowInvalid, NotServedValueUnparsed}, NotServedValueUnparsed},
		{[]string{NotServedValueUnparsed, NotServedWindowInvalid}, NotServedValueUnparsed},
		{[]string{NotServedValueUnparsedDropped, NotServedValueUnparsed, NotServedWindowInvalid}, NotServedValueUnparsedDropped},
		{[]string{NotServedWindowInvalid, NotServedValueUnparsedDropped}, NotServedValueUnparsedDropped},
		{all, NotServedValueUnparsedDropped},
	} {
		rec := &rejectRecorder{}
		for _, r := range tc.seq {
			rec.note("tx", "k", r)
		}
		if got := rec.byTenant["tx"]["k"]; got != tc.want {
			t.Errorf("note %v: kept %q, want %q", tc.seq, got, tc.want)
		}
	}
}

// recordUnparsedUnionOfCuts is recordUnparsed as #2296 shipped it: every
// tenant resolved at every cut of every tenant (ScheduleCuts), plus the
// whole config resolved as written (where the phases read the windows) —
// the reference the per-tenant reading must agree with.
func recordUnparsedUnionOfCuts(cfg *ThresholdConfig) map[string]map[string]string {
	rec := &rejectRecorder{}
	cfg.resolveAtWithStats(scheduleDay, nil, nil, rec)
	for _, m := range cfg.ScheduleCuts() {
		cfg.AtMinuteOfDay(m).resolveAtWithStats(scheduleDay, nil, nil, rec)
	}
	return rec.byTenant
}

// TestRecordUnparsed_PerTenantCutsMatchUnionOfCuts: reading each tenant only
// at its own cuts records exactly what reading every tenant at every
// tenant's cuts records, on random trees whose tenants carry schedules with
// different windows, unparseable values in some windows only, critical,
// dimensional and declared keys, and invalid windows.
func TestRecordUnparsed_PerTenantCutsMatchUnionOfCuts(t *testing.T) {
	t.Parallel()
	values := []string{"70", "abc", "disable", "60:critical", "7O:critical", "", "5"}
	windows := []string{"01:00-09:00", "22:00-06:00", "05:00-05:01", "12:30-13:00",
		"01:00~09:00", "05:00-05:00", "", "00:00-23:59"}
	keys := []string{"mysql_connections", "mysql_connections_critical", "redis_memory",
		`mysql_connections{db="x"}`, "mysql_threads_running", "pg_connections"}
	for seed := int64(1); seed <= 200; seed++ {
		r := rand.New(rand.NewSource(seed))
		pick := func(xs []string) string { return xs[r.Intn(len(xs))] }
		cfg := &ThresholdConfig{
			Defaults:          map[string]float64{"mysql_connections": 30, "mysql_threads_running": 10},
			OptionalOverrides: []string{"redis_memory"},
			Tenants:           map[string]map[string]ScheduledValue{},
		}
		for i := 0; i < 1+r.Intn(6); i++ {
			m := map[string]ScheduledValue{}
			for j := 0; j < r.Intn(4); j++ {
				sv := ScheduledValue{Default: pick(values)}
				for w := 0; w < r.Intn(3); w++ {
					sv.Overrides = append(sv.Overrides, TimeWindowOverride{Window: pick(windows), Value: pick(values)})
				}
				m[pick(keys)] = sv
			}
			cfg.Tenants[fmt.Sprintf("t%d", i)] = m
		}
		got, want := recordUnparsed(cfg, scheduleDay), recordUnparsedUnionOfCuts(cfg)
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("seed %d: per-tenant cuts recorded\n %v\nunion of cuts\n %v\ncfg %+v", seed, got, want, cfg.Tenants)
		}
		if got2 := cfg.ValuesNotServed(scheduleDay); !reflect.DeepEqual(got2, got) {
			t.Fatalf("seed %d: ValuesNotServed %v, recordUnparsed %v", seed, got2, got)
		}
	}
}

// TestRecordUnparsed_ValueInOneWindowOfOneTenant: a value that does not
// parse only inside one tenant's window is found, whatever the other
// tenants' schedules — the case the per-tenant reading must not lose.
func TestRecordUnparsed_ValueInOneWindowOfOneTenant(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 30},
		Tenants: map[string]map[string]ScheduledValue{
			"tx": {"mysql_connections": {Default: "60", Overrides: []TimeWindowOverride{{Window: "13:07-13:08", Value: "abc"}}}},
			"ty": {"mysql_connections": {Default: "60", Overrides: []TimeWindowOverride{{Window: "01:00-02:00", Value: "70"}}}},
		},
	}
	want := map[string]map[string]string{"tx": {"mysql_connections": NotServedValueUnparsed}}
	if got := cfg.ValuesNotServed(scheduleDay); !reflect.DeepEqual(got, want) {
		t.Errorf("ValuesNotServed = %v, want %v", got, want)
	}
}
