package config

import (
	"fmt"
	"reflect"
	"sort"
	"testing"
	"time"
)

// scheduleDay is the UTC midnight every minute-of-day test reads from.
var scheduleDay = time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)

func win(window, value string) TimeWindowOverride {
	return TimeWindowOverride{Window: window, Value: value}
}

// scheduleFixtures are the schedule shapes the whole-day reading must agree
// with the at-instant reading on.
var scheduleFixtures = map[string]ScheduledValue{
	"scalar":         {Default: "70"},
	"same-day":       {Default: "70", Overrides: []TimeWindowOverride{win("01:00-09:00", "1000")}},
	"cross-midnight": {Default: "70", Overrides: []TimeWindowOverride{win("22:00-06:00", "1000")}},
	// The first window that holds a minute wins: 03:00-05:00 is inside the
	// first window, so it never shows; 05:00-08:00 shows only after 06:00.
	"overlap-first-wins": {Default: "70", Overrides: []TimeWindowOverride{
		win("22:00-06:00", "1000"), win("03:00-05:00", "5"), win("05:00-08:00", "7"),
	}},
	"invalid-skipped": {Default: "70", Overrides: []TimeWindowOverride{
		win("25:00-26:00", "9"), win("bad", "8"), win("10:00", "6"), win("02:00-03:00", "1"),
	}},
	"disable":          {Default: "70", Overrides: []TimeWindowOverride{win("12:00-13:00", "disable")}},
	"critical-suffix":  {Default: "70", Overrides: []TimeWindowOverride{win("15:00-16:00", "700:critical")}},
	"empty-window":     {Default: "70", Overrides: []TimeWindowOverride{win("05:00-05:00", "1")}},
	"whole-day-window": {Default: "70", Overrides: []TimeWindowOverride{win("00:00-23:59", "1"), win("23:59-00:00", "2")}},
	"same-value-twice": {Default: "70", Overrides: []TimeWindowOverride{
		win("01:00-02:00", "5"), win("02:00-03:00", "5"), win("04:00-05:00", "70"),
	}},
	"expiry-kept": {Default: "95", Overrides: []TimeWindowOverride{win("08:00-09:00", "99")},
		Expiry: &ExpiryMeta{Expires: "2026-01-01T00:00:00Z"}},
}

func segmentAt(segs []ValueSegment, minute int) (ValueSegment, bool) {
	for _, s := range segs {
		if minute >= s.From && minute < s.To {
			return s, true
		}
	}
	return ValueSegment{}, false
}

// TestDaySegments_AgreeWithResolveValueEveryMinute: for every one of the
// 1440 minutes, the segment holding it carries what the at-instant reading
// (resolveValue, the resolver's) gives at that minute; the segments cover
// the day in order with no gap, and no two neighbours carry the same value.
func TestDaySegments_AgreeWithResolveValueEveryMinute(t *testing.T) {
	t.Parallel()
	for name, sv := range scheduleFixtures {
		name, sv := name, sv
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			segs := sv.DaySegments()
			if len(segs) == 0 || segs[0].From != 0 || segs[len(segs)-1].To != MinutesPerDay {
				t.Fatalf("segments do not cover the day: %+v", segs)
			}
			for i := 1; i < len(segs); i++ {
				if segs[i].From != segs[i-1].To {
					t.Fatalf("gap or overlap between %+v and %+v", segs[i-1], segs[i])
				}
				if segs[i].Value == segs[i-1].Value {
					t.Fatalf("neighbours %+v and %+v carry the same value: not merged", segs[i-1], segs[i])
				}
			}
			for m := 0; m < MinutesPerDay; m++ {
				want := sv.resolveValue(scheduleDay.Add(time.Duration(m)*time.Minute), nil)
				got, ok := segmentAt(segs, m)
				if !ok || got.Value != want {
					t.Fatalf("minute %02d:%02d: segment %+v, the resolver reads %q", m/60, m%60, got, want)
				}
			}
		})
	}
}

// TestDaySegments_Exact pins the segments of the overlap and same-value
// shapes, so a reading where the LAST window wins, or neighbours are left
// unmerged, cannot pass.
func TestDaySegments_Exact(t *testing.T) {
	t.Parallel()
	cases := map[string][]ValueSegment{
		"overlap-first-wins": {
			{0, 360, "1000"}, {360, 480, "7"}, {480, 1320, "70"}, {1320, 1440, "1000"},
		},
		"same-value-twice": {
			{0, 60, "70"}, {60, 180, "5"}, {180, 1440, "70"},
		},
		"cross-midnight": {
			{0, 360, "1000"}, {360, 1320, "70"}, {1320, 1440, "1000"},
		},
		"invalid-skipped": {
			{0, 120, "70"}, {120, 180, "1"}, {180, 1440, "70"},
		},
		"scalar": {{0, 1440, "70"}},
	}
	for name, want := range cases {
		if got := scheduleFixtures[name].DaySegments(); !reflect.DeepEqual(got, want) {
			t.Errorf("%s: DaySegments = %+v, want %+v", name, got, want)
		}
	}
}

// rowsText renders resolved rows in a stable order for comparison.
func rowsText(rows []ResolvedThreshold) []string {
	out := make([]string, 0, len(rows))
	for _, r := range rows {
		out = append(out, fmt.Sprintf("%s|%s|%s|%s|%v|%s|%s", r.Tenant, r.Component, r.Metric, r.Severity, r.Value,
			canonicalLabelKey(r.CustomLabels, r.RegexLabels), r.legacyTwinOf))
	}
	sort.Strings(out)
	return out
}

// TestAtMinuteOfDay_ResolvesAsTheScheduleEveryMinute: for every minute,
// resolving the tree fixed at the cut that holds that minute gives the rows
// resolving the tree itself at that minute gives — so the cuts miss no change
// of any value and the fixed tree reads like the scheduled one.
func TestAtMinuteOfDay_ResolvesAsTheScheduleEveryMinute(t *testing.T) {
	t.Parallel()
	tenants := map[string]map[string]ScheduledValue{}
	i := 0
	for _, sv := range scheduleFixtures {
		tenants[fmt.Sprintf("t%02d", i)] = map[string]ScheduledValue{
			"mysql_connections": sv,
			// The same schedule on the _critical, dimensional and retired
			// spellings, read by their own resolve phases.
			"mysql_connections_critical":             {Default: "900", Overrides: sv.Overrides},
			`mysql_connections{db="a"}`:              sv,
			"mysql_cpu":                              sv,
			"redis_memory":                           {Default: "50", Overrides: []TimeWindowOverride{win("07:00-07:30", "disable")}},
			"redis_memory_critical":                  {Default: "disable", Overrides: []TimeWindowOverride{win("19:00-02:00", "60")}},
			`redis_memory{shard=~"s.*"}`:             {Default: "40:critical"},
			"mysql_threads_running" + criticalSuffix: {Default: "88", Overrides: []TimeWindowOverride{win("10:00-11:00", "x")}},
			"redis_unknown_value":                    {Default: "1"},
		}
		i++
	}
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80, "mysql_threads_running": 30, "redis_memory": 90},
		Tenants:  tenants,
	}
	cuts := cfg.ScheduleCuts()
	if cuts[0] != 0 || !sort.IntsAreSorted(cuts) {
		t.Fatalf("cuts %v: not sorted from 0", cuts)
	}
	fixed := make([][]string, len(cuts))
	for j, c := range cuts {
		rows, _ := cfg.AtMinuteOfDay(c).resolveAtWithStats(scheduleDay, nil, nil)
		fixed[j] = rowsText(rows)
	}
	for m := 0; m < MinutesPerDay; m++ {
		at := scheduleDay.Add(time.Duration(m) * time.Minute)
		rows, _ := cfg.resolveAtWithStats(at, nil, nil)
		want := rowsText(rows)
		j := sort.SearchInts(cuts, m+1) - 1
		if !reflect.DeepEqual(fixed[j], want) {
			t.Fatalf("minute %02d:%02d (cut %d): the fixed tree resolves\n%v\nthe tree resolves\n%v", m/60, m%60, cuts[j], fixed[j], want)
		}
		if got := rowsText(func() []ResolvedThreshold {
			r, _ := cfg.AtMinuteOfDay(m).resolveAtWithStats(at, nil, nil)
			return r
		}()); m%37 == 0 && !reflect.DeepEqual(got, want) {
			t.Fatalf("minute %02d:%02d: AtMinuteOfDay(m) resolves %v, want %v", m/60, m%60, got, want)
		}
	}
}

// TestAtMinuteOfDay_LeavesTheTreeAlone: the copy does not write through to
// the tree it was made from, and keeps the expiry of a value it fixes.
func TestAtMinuteOfDay_LeavesTheTreeAlone(t *testing.T) {
	t.Parallel()
	sv := scheduleFixtures["expiry-kept"]
	cfg := &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"t": {"redis_memory": sv, "x": {Default: "1"}}}}
	got := cfg.AtMinuteOfDay(8 * 60)
	if v := got.Tenants["t"]["redis_memory"]; v.Default != "99" || len(v.Overrides) != 0 || v.Expiry != sv.Expiry {
		t.Errorf("fixed value = %+v, want default 99, no schedule, the same expiry", v)
	}
	if v := cfg.Tenants["t"]["redis_memory"]; len(v.Overrides) != 1 || v.Default != "95" {
		t.Errorf("the original tree changed: %+v", v)
	}
}

// TestDeprecatedKeyAliases_IsACopyOfTheTable: the exported table is the one
// the resolver canonicalizes with, and writing to it changes nothing.
func TestDeprecatedKeyAliases_IsACopyOfTheTable(t *testing.T) {
	t.Parallel()
	got := DeprecatedKeyAliases()
	if !reflect.DeepEqual(got, deprecatedKeyAliases) {
		t.Fatalf("DeprecatedKeyAliases() = %v, want %v", got, deprecatedKeyAliases)
	}
	for legacy, canon := range got {
		if c, ok := CanonicalKeyFor(legacy); !ok || c != canon {
			t.Errorf("CanonicalKeyFor(%q) = %q, %v; the table says %q", legacy, c, ok, canon)
		}
	}
	got["zz_test"] = "x"
	if _, leaked := deprecatedKeyAliases["zz_test"]; leaked {
		t.Fatal("writing to the copy changed the resolver's table")
	}
}
