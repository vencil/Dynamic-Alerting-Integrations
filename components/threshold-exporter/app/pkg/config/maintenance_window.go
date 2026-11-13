package config

import (
	"fmt"
	"strings"
	"time"
)

// A recurring maintenance window (#3639): the weekdays it opens on and a UTC
// "HH:MM-HH:MM" window — the same window dialect, parsed by the same
// parseTimeWindow / windowContains, as a threshold's time-windowed override,
// so the platform has one time syntax and one implementation of it.
//
//	_state_maintenance:
//	  recurring:
//	    - weekdays: [sun]
//	      window: "02:00-06:00"
//	      reason: weekly patching
//
// The exporter evaluates it on every scrape and turns the maintenance filter
// on inside the window, the same way a one-shot `_state_maintenance` does
// (owner ruling R1, #1200 Q2): rules do not fire, nothing is silenced.
//
// Fail-closed: an entry the reader does not read — an unknown field (a typo
// such as `weekday:` must not read as "every day"), no weekdays, a weekday it
// does not know, a window that does not parse — makes the whole value
// unread (stateValueProblem): maintenance is NOT on, the write gate refuses
// it, and the exporter WARNs.

// recurringFields are the fields of one recurring entry, sorted.
var recurringFields = []string{"reason", "weekdays", "window"}

// weekdayNames are the weekday spellings recurring takes, Sunday first as
// time.Weekday counts.
var weekdayNames = [7]string{"sun", "mon", "tue", "wed", "thu", "fri", "sat"}

// maintenanceWindow is one recurring entry as the reader reads it.
type maintenanceWindow struct {
	days       [7]bool // by time.Weekday
	start, end int     // minute of the UTC day; start > end crosses midnight
	text       string  // the window as written, for canonical()
	reason     string
}

// contains reports whether now (in UTC) is inside w. A window that crosses
// midnight belongs to the weekday it starts on: `[sun] 22:00-02:00` is open
// from Sunday 22:00 to Monday 02:00.
func (w maintenanceWindow) contains(now time.Time) bool {
	utc := now.UTC()
	minute := utc.Hour()*60 + utc.Minute()
	day := utc.Weekday()
	if w.start < w.end {
		return w.days[day] && windowContains(w.start, w.end, minute)
	}
	if minute >= w.start {
		return w.days[day]
	}
	if minute < w.end {
		return w.days[(day+6)%7]
	}
	return false
}

// recurringWindowsOf reads the `recurring` field (its generic decode): the
// windows, or why the field is not read — a sentence of constants and the
// entry's index, never a field name or a value the author wrote (#3703).
func recurringWindowsOf(generic any) ([]maintenanceWindow, string) {
	if generic == nil {
		return nil, ""
	}
	entries, ok := generic.([]any)
	if !ok {
		return nil, "recurring must be a list of {weekdays, window[, reason]} entries"
	}
	if len(entries) == 0 {
		// The schema's minItems: 1 — an empty list is a recurring that says
		// nothing, most likely an edit that dropped its entries.
		return nil, "recurring must list at least one {weekdays, window[, reason]} entry"
	}
	out := make([]maintenanceWindow, 0, len(entries))
	for i, e := range entries {
		m, ok := e.(map[string]any)
		if !ok {
			return nil, fmt.Sprintf("recurring[%d] must be a mapping with weekdays and window", i)
		}
		unknown := 0
		for k := range m {
			if !containsString(recurringFields, k) {
				unknown++
			}
		}
		if unknown > 0 {
			return nil, fmt.Sprintf("recurring[%d] has %d unknown field(s) (valid: %s)",
				i, unknown, strings.Join(recurringFields, ", "))
		}
		var w maintenanceWindow
		days, _ := m["weekdays"].([]any)
		if len(days) == 0 {
			return nil, fmt.Sprintf("recurring[%d] needs weekdays: a non-empty list of %s",
				i, strings.Join(weekdayNames[:], ", "))
		}
		for _, d := range days {
			name, _ := d.(string)
			idx := -1
			for j, n := range weekdayNames {
				if n == name {
					idx = j
				}
			}
			if idx < 0 {
				return nil, fmt.Sprintf("recurring[%d] has a weekday that is not one of %s",
					i, strings.Join(weekdayNames[:], ", "))
			}
			if w.days[idx] {
				// The schema's uniqueItems: the reader refuses what the
				// schema refuses, so the write gate and a schema check of
				// the same file never disagree.
				return nil, fmt.Sprintf("recurring[%d] lists weekday %s more than once", i, weekdayNames[idx])
			}
			w.days[idx] = true
		}
		text, _ := m["window"].(string)
		start, end, ok := parseTimeWindow(text, nil)
		if !ok || !isCanonicalWindow(text) {
			return nil, fmt.Sprintf("recurring[%d] window is not a UTC \"HH:MM-HH:MM\" window with start != end", i)
		}
		if r, ok := m["reason"]; ok {
			s, isString := r.(string)
			if !isString {
				return nil, fmt.Sprintf("recurring[%d] reason must be a string", i)
			}
			w.reason = s
		}
		w.start, w.end, w.text = start, end, text
		out = append(out, w)
	}
	return out, ""
}

// isCanonicalWindow reports whether text is spelled exactly "HH:MM-HH:MM",
// two digits each — the schema's window pattern. parseTimeWindow alone also
// takes "2:00-6:00", " 02:00 - 06:00 " and "+2:00-06:00" (a threshold's
// time-windowed override has always been read that leniently); a recurring
// window is new, so it takes only the spelling the schema documents, and the
// write gate never accepts a file a schema check of it would refuse.
// The ranges (hour <= 23, minute <= 59) are parseTimeWindow's.
func isCanonicalWindow(text string) bool {
	if len(text) != len("00:00-00:00") {
		return false
	}
	for i := 0; i < len(text); i++ {
		c := text[i]
		switch i {
		case 2, 8:
			if c != ':' {
				return false
			}
		case 5:
			if c != '-' {
				return false
			}
		default:
			if c < '0' || c > '9' {
				return false
			}
		}
	}
	return true
}
