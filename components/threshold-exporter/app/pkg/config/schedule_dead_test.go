package config

import (
	"fmt"
	"reflect"
	"testing"
	"time"
)

// #3502: the entries of a schedule /metrics serves at no minute of the day.

func TestDeadEntries_Exact(t *testing.T) {
	cases := []struct {
		name string
		sv   ScheduledValue
		want []DeadEntry
	}{
		{"scalar", ScheduledValue{Default: "70"}, nil},
		{"one window", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{win("01:00-09:00", "1000")}}, nil},
		{"partial overlap is live", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
			win("22:00-06:00", "1000"), win("05:00-08:00", "7"),
		}}, nil},
		{"window inside an earlier one", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
			win("22:00-06:00", "1000"), win("01:00-03:00", "5"),
		}}, []DeadEntry{{Entry: "overrides[1]", Window: "01:00-03:00", Value: "5", Reason: DeadEntryWindowCovered}}},
		{"window covered by two earlier ones together", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
			win("01:00-02:00", "1"), win("02:00-03:00", "2"), win("01:30-02:30", "3"),
		}}, []DeadEntry{{Entry: "overrides[2]", Window: "01:30-02:30", Value: "3", Reason: DeadEntryWindowCovered}}},
		{"same value, later window still dead", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
			win("00:00-12:00", "5"), win("06:00-07:00", "5"),
		}}, []DeadEntry{{Entry: "overrides[1]", Window: "06:00-07:00", Value: "5", Reason: DeadEntryWindowCovered}}},
		{"default covered by two windows", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
			win("00:00-12:00", "1"), win("12:00-00:00", "2"),
		}}, []DeadEntry{{Entry: "default", Value: "70", Reason: DeadEntryDefaultCovered}}},
		{"default and a window both dead", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
			win("22:00-10:00", "1"), win("10:00-22:00", "2"), win("23:00-01:00", "3"),
		}}, []DeadEntry{
			{Entry: "default", Value: "70", Reason: DeadEntryDefaultCovered},
			{Entry: "overrides[2]", Window: "23:00-01:00", Value: "3", Reason: DeadEntryWindowCovered},
		}},
		{"an invalid window is window_invalid's, not listed", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
			win("bogus", "9"), win("03:00-03:00", "8"),
		}}, nil},
		{"a null default is no entry", ScheduledValue{Default: "", Overrides: []TimeWindowOverride{
			win("00:00-12:00", "1"), win("12:00-00:00", "2"),
		}}, nil},
		{"order matters: the wider window written second", ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{
			win("01:00-03:00", "5"), win("22:00-06:00", "1000"),
		}}, nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := c.sv.DeadEntries(); !reflect.DeepEqual(got, c.want) {
				t.Errorf("DeadEntries() = %+v, want %+v", got, c.want)
			}
		})
	}
}

// TestDeadEntries_AgreeWithMatchTimeWindowEveryMinute is the oracle: an
// entry is dead exactly when, read minute by minute with the at-instant
// window reader (MatchTimeWindow, first match wins — not entryAtMinute), no
// minute of the day is served by it.
func TestDeadEntries_AgreeWithMatchTimeWindowEveryMinute(t *testing.T) {
	fixtures := map[string]ScheduledValue{}
	for name, sv := range scheduleFixtures {
		fixtures[name] = sv
	}
	windows := []string{"00:00-12:00", "12:00-00:00", "22:00-06:00", "01:00-03:00", "05:00-08:00",
		"23:59-00:01", "06:00-22:00", "bad", "03:00-03:00"}
	// Every ordered pair and triple of windows.
	for _, a := range windows {
		for _, b := range windows {
			fixtures[a+"|"+b] = ScheduledValue{Default: "70", Overrides: []TimeWindowOverride{win(a, "1"), win(b, "2")}}
			for _, c := range windows {
				fixtures[a+"|"+b+"|"+c] = ScheduledValue{Default: "70",
					Overrides: []TimeWindowOverride{win(a, "1"), win(b, "2"), win(c, "3")}}
			}
		}
	}
	for name, sv := range fixtures {
		served := map[string]bool{}
		for m := 0; m < MinutesPerDay; m++ {
			at := scheduleDay.Add(time.Duration(m) * time.Minute)
			entry := "default"
			for i, o := range sv.Overrides {
				if MatchTimeWindow(o.Window, at) {
					entry = fmt.Sprintf("overrides[%d]", i)
					break
				}
			}
			served[entry] = true
		}
		dead := map[string]bool{}
		for _, d := range sv.DeadEntries() {
			dead[d.Entry] = true
		}
		entries := []string{}
		if sv.Default != "" {
			entries = append(entries, "default")
		}
		for i, o := range sv.Overrides {
			if _, _, ok := parseTimeWindow(o.Window, nil); ok {
				entries = append(entries, fmt.Sprintf("overrides[%d]", i))
			}
		}
		if len(sv.Overrides) == 0 {
			entries = nil
		}
		for _, e := range entries {
			if dead[e] == served[e] {
				t.Errorf("%s: %s served=%v but DeadEntries lists it=%v", name, e, served[e], dead[e])
			}
		}
	}
}

func TestDeadEntryReasons_IsTheClosedSet(t *testing.T) {
	want := []string{DeadEntryDefaultCovered, DeadEntryWindowCovered}
	if got := DeadEntryReasons(); !reflect.DeepEqual(got, want) {
		t.Errorf("DeadEntryReasons() = %v, want %v", got, want)
	}
}

func TestDeadEntriesReading_TellsEntriesApart(t *testing.T) {
	a := []DeadEntry{{Entry: "overrides[1]", Window: "01:00-03:00", Value: "5", Reason: DeadEntryWindowCovered}}
	b := []DeadEntry{{Entry: "overrides[1]", Window: "01:00-03:00", Value: "6", Reason: DeadEntryWindowCovered}}
	if DeadEntriesReading(a) == DeadEntriesReading(b) {
		t.Error("a dead entry whose value changed reads the same")
	}
	if DeadEntriesReading(a) != DeadEntriesReading(append([]DeadEntry(nil), a...)) {
		t.Error("the same dead entries read differently")
	}
}
