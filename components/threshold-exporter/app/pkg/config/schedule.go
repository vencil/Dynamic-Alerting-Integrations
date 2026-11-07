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

import (
	"fmt"
	"sort"
	"strings"
)

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
	cuts := sv.dayCuts()
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

// dayCuts is 0, MinutesPerDay and the start and end of every window of sv
// that parses, sorted (duplicates kept): between two consecutive cuts no
// window's membership changes, so the entry that serves the first minute of
// the piece serves all of it.
func (sv ScheduledValue) dayCuts() []int {
	cuts := []int{0, MinutesPerDay}
	for _, o := range sv.Overrides {
		if start, end, ok := parseTimeWindow(o.Window, nil); ok {
			cuts = append(cuts, start, end)
		}
	}
	sort.Ints(cuts)
	return cuts
}

// DeadEntry is one entry of a threshold value's schedule that /metrics serves
// at no minute of the UTC day (#3502): the windows before it, or the windows
// together, cover every minute it would serve. The key itself is served —
// what never applies is this entry.
type DeadEntry struct {
	// Entry: "default", or "overrides[<i>]" (0-based, the order written).
	Entry string `json:"entry"`
	// Window: the entry's `window:` as written; absent for the default.
	Window string `json:"window,omitempty"`
	// Value: the entry's value as written.
	Value string `json:"value"`
	// Reason: one of the DeadEntry* reasons.
	Reason string `json:"reason"`
}

// Reasons of DeadEntry.Reason — a closed set (DeadEntryReasons).
const (
	// DeadEntryDefaultCovered: the windows together cover the whole UTC
	// day, so the `default` is never served. One window cannot (a window
	// whose start equals its end is refused, window_invalid), so it takes
	// two or more.
	DeadEntryDefaultCovered = "default_covered"
	// DeadEntryWindowCovered: every minute of the window is in a window
	// written before it; the first window that contains a minute serves it.
	DeadEntryWindowCovered = "window_covered"
)

// DeadEntryReasons is the closed set of DeadEntry.Reason, in the order a
// reader lists them.
func DeadEntryReasons() []string {
	return []string{DeadEntryDefaultCovered, DeadEntryWindowCovered}
}

// DeadEntries is every entry of sv that serves no minute of the UTC day, in
// the order written (the default first); nil when every entry serves some
// minute, and for a value with no schedule. Which entry serves a minute is
// entryAtMinute — the reading /metrics serves by — asked once per piece of
// the day between two cuts (dayCuts), as DaySegments reads it.
//
// ⚠️ Not here: a window that does not parse. It serves no minute either,
// but it is already NotServedWindowInvalid on the key (noteInvalidWindows),
// an error of its own; listing it again would report one defect twice. A
// default written as null (`{default: null, …}`) writes no value and is
// not an entry. `expires:` changes nothing here: before it passes the
// schedule is served as written, after it none of it is — no entry is
// reachable after it that was not before (the key's own verdict).
func (sv ScheduledValue) DeadEntries() []DeadEntry {
	if len(sv.Overrides) == 0 {
		return nil
	}
	cuts := sv.dayCuts()
	live := make(map[int]bool, len(sv.Overrides)+1)
	for i := 0; i+1 < len(cuts); i++ {
		if cuts[i] != cuts[i+1] {
			live[sv.entryAtMinute(cuts[i], nil)] = true
		}
	}
	var out []DeadEntry
	if !live[entryDefault] && sv.Default != "" {
		out = append(out, DeadEntry{Entry: "default", Value: sv.Default, Reason: DeadEntryDefaultCovered})
	}
	for i, o := range sv.Overrides {
		if _, _, ok := parseTimeWindow(o.Window, nil); !ok || live[i] {
			continue
		}
		out = append(out, DeadEntry{Entry: fmt.Sprintf("overrides[%d]", i), Window: o.Window, Value: o.Value,
			Reason: DeadEntryWindowCovered})
	}
	return out
}

// DeadEntriesReading is des as one string — entry, window, value and reason
// of each, in order — what makes two dead-entry findings the same one for a
// ratchet (keyverdict's reading): the schedule as written, so a write that
// changes a dead entry, or makes another one dead, is a new finding, and one
// that leaves them as they are is not.
func DeadEntriesReading(des []DeadEntry) string {
	var b strings.Builder
	for i, d := range des {
		if i > 0 {
			b.WriteString("\x00")
		}
		fmt.Fprintf(&b, "%s\x01%s\x01%s\x01%s", d.Entry, d.Window, d.Value, d.Reason)
	}
	return b.String()
}

// deadEntriesOfView is, for every threshold key of view (an effective
// config as shown) written as a schedule, its DeadEntries — read from the
// value as /metrics reads it (scheduledValueFromRaw, the build's one
// decode of a raw value); nil when no key has one. A reserved key's
// schedule is not read (#3269: only its `default` is), so it has none.
func deadEntriesOfView(view map[string]any) map[string][]DeadEntry {
	var out map[string][]DeadEntry
	for k, v := range view {
		m, ok := v.(map[string]any)
		if !ok || strings.HasPrefix(k, "_") {
			continue
		}
		if _, scheduled := m["overrides"]; !scheduled {
			continue
		}
		sv, ok := scheduledValueFromRaw(v)
		if !ok {
			continue
		}
		if des := sv.DeadEntries(); len(des) > 0 {
			if out == nil {
				out = map[string][]DeadEntry{}
			}
			out[k] = des
		}
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
