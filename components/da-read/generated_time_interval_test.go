package main

import (
	"testing"
	"time"

	"github.com/prometheus/alertmanager/config"
)

// TestGeneratedTimeIntervalIsActiveAtEveryInstant pins the property the
// route generator's ownership mark rests on (#1211 gap 2): a time interval
// with no constraint contains every instant, so marking a route
// `active_time_intervals: [platform-generated-always]` never mutes it.
// amtool's `config routes test` does not evaluate time intervals, so this is
// the only check of it — against the Alertmanager this module vendors.
func TestGeneratedTimeIntervalIsActiveAtEveryInstant(t *testing.T) {
	cfg, err := config.LoadFile("testdata/platform-generated-always.yaml")
	if err != nil {
		t.Fatal(err)
	}
	var found bool
	for _, ti := range cfg.TimeIntervals {
		if ti.Name != "platform-generated-always" {
			continue
		}
		found = true
		if len(ti.TimeIntervals) != 1 {
			t.Fatalf("want one unconstrained interval, got %d", len(ti.TimeIntervals))
		}
		start := time.Date(1999, 12, 31, 23, 0, 0, 0, time.UTC)
		for i := 0; i < 24*400; i++ { // hourly for 400 days, across a year and a leap day
			at := start.Add(time.Duration(i) * time.Hour)
			for _, loc := range []*time.Location{time.UTC, time.FixedZone("x", -11*3600), time.FixedZone("y", 14*3600)} {
				if !ti.TimeIntervals[0].ContainsTime(at.In(loc)) {
					t.Fatalf("not active at %s", at.In(loc))
				}
			}
		}
	}
	if !found {
		t.Fatal("platform-generated-always is not defined in the fixture")
	}
	// A control: a constrained interval is NOT active everywhere, so the
	// loop above can fail.
	other, err := config.Load("route: {receiver: r}\nreceivers: [{name: r}]\n" +
		"time_intervals:\n  - name: c\n    time_intervals: [{weekdays: [monday]}]\n")
	if err != nil {
		t.Fatal(err)
	}
	sunday := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	if other.TimeIntervals[0].TimeIntervals[0].ContainsTime(sunday) {
		t.Fatal("control: a monday-only interval contained a sunday")
	}
}
