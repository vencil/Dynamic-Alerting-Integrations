package config

// schedule.go — a value's whole UTC day, cut where its schedule changes
// (#2115 (c)).
//
// The resolver reads a ScheduledValue at one instant (resolveValue). A
// reader that must check every part of the day — `da-guard served-values`'s
// `schedules` — needs the value at every minute instead. Rather than a
// second reading of the `overrides:` list, the day is cut at the minutes
// where some window starts or ends (parseTimeWindow, the parser the
// at-instant reading uses) and each piece is read with valueAtMinute, the
// at-instant reading itself. Between two such minutes no window's membership
// changes, so one reading per piece is the reading of every minute in it
// (TestDaySegments_AgreeWithResolveValueEveryMinute checks all 1440).

import "sort"

// MinutesPerDay is the length of the UTC day the schedule windows cut.
const MinutesPerDay = 24 * 60

// ValueSegment is one stretch [From, To) of the UTC day, in minutes
// (0..1440), over which a ScheduledValue resolves to Value, as written
// ("70", "disable", "500:critical", …) — what that value means for a row is
// the resolver's business, not this file's.
type ValueSegment struct {
	From  int
	To    int
	Value string
}

// DaySegments is sv over the whole UTC day: segments in order, covering
// 0..1440 with no gap, adjacent segments with the same value merged. A value
// with no schedule is one segment. Windows that do not parse are skipped, as
// the at-instant reading skips them (silently here: the resolver already
// WARNs about them on every reading).
func (sv ScheduledValue) DaySegments() []ValueSegment {
	cuts := []int{0, MinutesPerDay}
	for _, o := range sv.Overrides {
		if start, end, ok := parseTimeWindow(o.Window, nil); ok {
			cuts = append(cuts, start, end)
		}
	}
	sort.Ints(cuts)
	var out []ValueSegment
	for i := 0; i+1 < len(cuts); i++ {
		from, to := cuts[i], cuts[i+1]
		if from == to {
			continue
		}
		v := sv.valueAtMinute(from, nil)
		if n := len(out); n > 0 && out[n-1].Value == v {
			out[n-1].To = to
			continue
		}
		out = append(out, ValueSegment{From: from, To: to, Value: v})
	}
	return out
}

// ScheduleCuts is the minutes of the UTC day (sorted, 0 always first) at
// which some tenant value of c may resolve differently than the minute
// before: the starts of every DaySegments segment of every tenant value.
// Resolving c at any two instants whose time of day falls between the same
// two cuts (and whose expiry readings agree) gives the same rows.
func (c *ThresholdConfig) ScheduleCuts() []int {
	seen := map[int]bool{0: true}
	for _, overrides := range c.Tenants {
		seen = addScheduleCuts(seen, overrides)
	}
	return sortedCuts(seen)
}

// addScheduleCuts adds to seen the start of every DaySegments segment of
// every value of overrides that has a schedule, and returns it. A nil seen
// is made (holding 0) on the first schedule, so addScheduleCuts(nil, m) is
// nil when no value of one tenant's map m has a schedule.
func addScheduleCuts(seen map[int]bool, overrides map[string]ScheduledValue) map[int]bool {
	for _, sv := range overrides {
		if len(sv.Overrides) == 0 {
			continue
		}
		if seen == nil {
			seen = map[int]bool{0: true}
		}
		for _, seg := range sv.DaySegments() {
			seen[seg.From] = true
		}
	}
	return seen
}

func sortedCuts(seen map[int]bool) []int {
	out := make([]int, 0, len(seen))
	for m := range seen {
		out = append(out, m)
	}
	sort.Ints(out)
	return out
}

// AtMinuteOfDay is a copy of c in which every tenant value with a schedule
// is replaced by the value it resolves to at minute (0..1439) of the UTC day
// — its schedule dropped, its expiry kept. Resolving the copy at an instant t
// gives the rows resolving c at t gives when t's time of day is that minute;
// for any other t it gives the rows c would serve at that minute with t's
// expiry readings. c is not modified; maps without a schedule are shared.
func (c *ThresholdConfig) AtMinuteOfDay(minute int) *ThresholdConfig {
	out := *c
	out.Tenants = make(map[string]map[string]ScheduledValue, len(c.Tenants))
	for tenant, overrides := range c.Tenants {
		var fixed map[string]ScheduledValue
		for k, sv := range overrides {
			if len(sv.Overrides) == 0 {
				continue
			}
			if fixed == nil {
				fixed = make(map[string]ScheduledValue, len(overrides))
				for k2, v2 := range overrides {
					fixed[k2] = v2
				}
			}
			fixed[k] = ScheduledValue{Default: sv.valueAtMinute(minute, nil), Expiry: sv.Expiry}
		}
		if fixed == nil {
			out.Tenants[tenant] = overrides
			continue
		}
		out.Tenants[tenant] = fixed
	}
	return &out
}

// DeprecatedKeyAliases is a copy of the alias table the resolver
// canonicalizes with: retired base key → canonical base key. The
// `_critical`-suffixed and dimensional (`key{…}`) spellings of a retired
// base canonicalize with it (CanonicalKeyFor).
func DeprecatedKeyAliases() map[string]string {
	out := make(map[string]string, len(deprecatedKeyAliases))
	for k, v := range deprecatedKeyAliases {
		out[k] = v
	}
	return out
}
