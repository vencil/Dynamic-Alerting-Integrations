package config

// state_value_test.go (#3620) — the one reading of a `_state_<filter>`
// value (stateValueOf), every row of its rules, and the readers that use it:
// resolveStateFiltersAt, resolveMaintenanceExpiriesAt, and the recurring windows (#3639).

import (
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"
)

var stateValueNow = time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)

const (
	svFuture = "2099-01-01T00:00:00Z"
	svPast   = "2020-01-01T00:00:00Z"
)

// The rules, row by row: what each value reads as, whether it turns the
// filter on at stateValueNow (Friday 12:00 UTC), and how many recurring
// windows it carries.
func TestStateValueOf_Rules(t *testing.T) {
	t.Parallel()
	// A Sunday window: closed at stateValueNow.
	rec := "recurring:\n- weekdays: [sun]\n  window: \"02:00-06:00\"\n  reason: backup\n"
	// A Friday window around noon: open at stateValueNow.
	open := "recurring:\n- weekdays: [fri]\n  window: \"11:00-13:00\"\n"
	cases := []struct {
		name, filter, val string
		kind              stateValueKind
		on                bool
		recurring         int
		problemHas        string
	}{
		// Scalars, on any filter.
		{"enable", "maintenance", "enable", stateValueOn, true, 0, ""},
		{"enable any case, trimmed", "crashloop", "\"  EnAbLe \"", stateValueOn, true, 0, ""},
		{"disable", "maintenance", "disable", stateValueOff, false, 0, ""},
		{"disabled", "crashloop", "disabled", stateValueOff, false, 0, ""},
		{"off", "crashloop", "off", stateValueOff, false, 0, ""},
		{"false", "maintenance", "false", stateValueOff, false, 0, ""},
		{"DISABLE", "crashloop", "DISABLE", stateValueOff, false, 0, ""},
		{"yes", "maintenance", "yes", stateValueProblem, false, 0, `it is neither`},
		{"true", "maintenance", "true", stateValueProblem, false, 0, `it is neither`},
		{"1", "crashloop", "1", stateValueProblem, false, 0, `it is neither`},
		{"garbage", "maintenance", "garbage", stateValueProblem, false, 0, `it is neither`},
		{"empty", "maintenance", "", stateValueProblem, false, 0, "is neither"},
		{"enabled (not the word)", "crashloop", "enabled", stateValueProblem, false, 0, "is neither"},

		// A mapping on another filter.
		{"mapping on another filter", "crashloop", "target: enable\n", stateValueProblem, false, 0,
			"only _state_maintenance takes the structured form"},

		// The switch: target.
		{"target enable", "maintenance", "target: enable\n", stateValueOn, true, 0, ""},
		// #3638: a scalar that reads like the mapping is the scalar it was
		// written as — not read, not on.
		{"a scalar that reads like the mapping", "maintenance", "\"target: enable\"", stateValueProblem, false, 0, `it is neither`},
		{"a scalar that reads like a list", "maintenance", "\"[enable]\"", stateValueProblem, false, 0, `it is neither`},
		// A sequence is its text read as a scalar (as before #3638): not a
		// mapping, not a structured problem.
		{"a sequence", "maintenance", "[enable]", stateValueProblem, false, 0, `it is neither`},
		{"target disable", "maintenance", "target: disable\n", stateValueOff, false, 0, ""},
		{"target off (a disable word)", "maintenance", "target: \"off\"\n", stateValueOff, false, 0, ""},
		{"target false (YAML boolean)", "maintenance", "target: false\n", stateValueOff, false, 0, ""},
		{"target all", "maintenance", "target: all\n", stateValueProblem, false, 0, `target is neither`},
		{"target empty", "maintenance", "target: \"\"\n", stateValueProblem, false, 0, "is neither"},
		{"target a list", "maintenance", "target: [enable]\n", stateValueProblem, false, 0, "target must be"},

		// The switch: enabled.
		{"enabled true", "maintenance", "enabled: true\n", stateValueOn, true, 0, ""},
		{"enabled false", "maintenance", "enabled: false\n", stateValueOff, false, 0, ""},
		{"enabled off (a string to yaml.v3)", "maintenance", "enabled: off\n", stateValueProblem, false, 0,
			"enabled must be true or false"},
		{"enabled \"true\" quoted", "maintenance", "enabled: \"true\"\n", stateValueProblem, false, 0,
			"enabled must be true or false"},
		{"expires written empty", "maintenance", "target: enable\nexpires: \"\"\n", stateValueInvalidExpires, false, 0, "expires is written but empty"},
		{"expires null", "maintenance", "target: enable\nexpires:\n", stateValueInvalidExpires, false, 0, "expires is written but empty"},
		{"target and enabled agree on", "maintenance", "target: enable\nenabled: true\n", stateValueOn, true, 0, ""},
		{"target and enabled agree off", "maintenance", "target: disable\nenabled: false\n", stateValueOff, false, 0, ""},
		{"target enable, enabled false", "maintenance", "target: enable\nenabled: false\n", stateValueProblem, false, 0,
			"disagree"},
		{"target disable, enabled true", "maintenance", "target: disable\nenabled: true\n", stateValueProblem, false, 0,
			"disagree"},

		// Switch off wins over everything else.
		{"switch off with a future expires", "maintenance", "target: disable\nexpires: \"" + svFuture + "\"\n",
			stateValueOff, false, 0, ""},
		{"switch off with an unparseable expires", "maintenance", "enabled: false\nexpires: tomorrow\n",
			stateValueOff, false, 0, ""},
		{"switch off with recurring", "maintenance", "enabled: false\n" + rec, stateValueOff, false, 0, ""},

		// Switch on: until expires.
		{"switch on, expires future", "maintenance", "target: enable\nexpires: \"" + svFuture + "\"\n",
			stateValueOn, true, 0, ""},
		{"switch on, expires past", "maintenance", "enabled: true\nexpires: \"" + svPast + "\"\n",
			stateValueOn, false, 0, ""},
		{"switch on, expires unparseable", "maintenance", "target: enable\nexpires: \"2026-07-01\"\n",
			stateValueInvalidExpires, false, 0, ""},
		{"switch on with recurring", "maintenance", "enabled: true\n" + rec, stateValueOn, true, 1, ""},

		// No switch.
		{"expires future", "maintenance", "expires: \"" + svFuture + "\"\n", stateValueOn, true, 0, ""},
		{"expires past", "maintenance", "expires: \"" + svPast + "\"\nreason: r\n", stateValueOn, false, 0, ""},
		{"expires unparseable", "maintenance", "expires: next week\n", stateValueInvalidExpires, false, 0, ""},
		{"expires unquoted YAML timestamp", "maintenance", "expires: 2099-12-31T23:59:59.50+08:00\n",
			stateValueOn, true, 0, ""},
		{"recurring only", "maintenance", rec, stateValueRecurringOnly, false, 1, ""},
		{"expires + recurring", "maintenance", "expires: \"" + svFuture + "\"\n" + rec, stateValueOn, true, 1, ""},
		{"expires past + recurring", "maintenance", "expires: \"" + svPast + "\"\n" + rec, stateValueOn, false, 1, ""},
		{"expires unparseable + recurring", "maintenance", "expires: soon\n" + rec, stateValueInvalidExpires, false, 0, ""},
		{"reason only", "maintenance", "reason: planned\n", stateValueProblem, false, 0, "says nothing"},
		{"empty recurring", "maintenance", "recurring: []\n", stateValueProblem, false, 0, "at least one"},
		{"empty recurring beside a switch", "maintenance", "target: enable\nrecurring: []\n", stateValueProblem, false, 0, "at least one"},
		{"duplicate weekday", "maintenance", "recurring:\n- weekdays: [sun, sun]\n  window: \"02:00-06:00\"\n", stateValueProblem, false, 0, "more than once"},
		{"one-digit hour window", "maintenance", "recurring:\n- weekdays: [sun]\n  window: \"2:00-6:00\"\n", stateValueProblem, false, 0, "HH:MM-HH:MM"},
		{"reason that says expires:", "maintenance", "reason: \"expires: 2099-01-01T00:00:00Z\"\n",
			stateValueProblem, false, 0, "says nothing"},
		{"reason that says expires:, with a switch", "maintenance",
			"target: enable\nreason: \"until expires: never\"\n", stateValueOn, true, 0, ""},

		// Shapes.
		{"unknown field", "maintenance", "targt: disable\n", stateValueProblem, false, 0, "1 unknown field(s) (valid: "},
		{"recurring entry not a mapping", "maintenance", "recurring: [x]\n", stateValueProblem, false, 0, "must be a mapping"},

		// #3639: the exporter evaluates the windows itself.
		{"recurring only, in its window", "maintenance", open, stateValueRecurringOnly, true, 1, ""},
		{"expires past, in a window", "maintenance", "expires: \"" + svPast + "\"\n" + open, stateValueOn, true, 1, ""},
		{"switch off, in a window", "maintenance", "enabled: false\n" + open, stateValueOff, false, 0, ""},
		{"window crossing midnight, started Thursday", "maintenance",
			"recurring:\n- weekdays: [thu]\n  window: \"22:00-13:00\"\n", stateValueRecurringOnly, true, 1, ""},
		{"window crossing midnight, started Friday", "maintenance",
			"recurring:\n- weekdays: [fri]\n  window: \"22:00-13:00\"\n", stateValueRecurringOnly, false, 1, ""},
		{"misspelt field (fail-closed, not every day)", "maintenance",
			"recurring:\n- weekday: [fri]\n  window: \"11:00-13:00\"\n", stateValueProblem, false, 0, "recurring[0] has 1 unknown field(s) (valid: "},
		{"no weekdays", "maintenance", "recurring:\n- window: \"11:00-13:00\"\n", stateValueProblem, false, 0, "needs weekdays"},
		{"unknown weekday", "maintenance", "recurring:\n- weekdays: [friday]\n  window: \"11:00-13:00\"\n",
			stateValueProblem, false, 0, "recurring[0] has a weekday that is not one of"},
		{"window not HH:MM-HH:MM", "maintenance", "recurring:\n- weekdays: [fri]\n  window: 4h\n",
			stateValueProblem, false, 0, "is not a UTC"},
		{"empty window", "maintenance", "recurring:\n- weekdays: [fri]\n  window: \"11:00-11:00\"\n",
			stateValueProblem, false, 0, "is not a UTC"},
		{"the old cron form", "maintenance", "recurring:\n- cron: \"0 2 * * 0\"\n  duration: 4h\n",
			stateValueProblem, false, 0, "recurring[0] has 2 unknown field(s) (valid: "},
		{"recurring not a list", "maintenance", "recurring: weekly\n", stateValueProblem, false, 0, "must be a list"},
		{"non-string keys", "maintenance", "1: x\n", stateValueProblem, false, 0, "takes only the fields"},
	}
	for _, c := range cases {
		r := stateValueOf(c.filter, valueAsWritten(c.val))
		if r.kind != c.kind {
			t.Errorf("%s: kind = %d, want %d (problem %q)", c.name, r.kind, c.kind, r.problem)
			continue
		}
		if got := r.onAt(stateValueNow); got != c.on {
			t.Errorf("%s: onAt = %v, want %v", c.name, got, c.on)
		}
		if got := len(r.windows); got != c.recurring {
			t.Errorf("%s: recurring windows = %d, want %d", c.name, got, c.recurring)
		}
		// expiresErr: an invalid expires' own text, the reader's WARN only.
		if c.problemHas != "" && !strings.Contains(r.problem+r.expiresErr, c.problemHas) {
			t.Errorf("%s: problem = %q, want it to contain %q", c.name, r.problem+r.expiresErr, c.problemHas)
		}
		// The write gate refuses exactly what the reader does not read.
		wp := stateFilterValueProblem("_state_"+c.filter, valueAsWritten(c.val))
		wantRefused := c.kind == stateValueProblem || c.kind == stateValueInvalidExpires
		if (wp != "") != wantRefused {
			t.Errorf("%s: write problem = %q, want refused=%v", c.name, wp, wantRefused)
		}
		// Every reading is comparable (Ratchet's ==).
		_ = any(stateFilterValueReading("_state_"+c.filter, valueAsWritten(c.val))) ==
			any(stateFilterValueReading("_state_"+c.filter, valueAsWritten(c.val)))
	}
}

// A window that crosses midnight belongs to the weekday it starts on, and
// each minute of the week is in or out by [start, end) in UTC.
func TestMaintenanceWindow_Contains(t *testing.T) {
	t.Parallel()
	ws, problem := recurringWindowsOf([]any{map[string]any{"weekdays": []any{"sun"}, "window": "22:00-02:00"}})
	if problem != "" || len(ws) != 1 {
		t.Fatalf("windows %v, problem %q", ws, problem)
	}
	w := ws[0]
	sun := time.Date(2026, 11, 8, 0, 0, 0, 0, time.UTC) // a Sunday
	for _, c := range []struct {
		at   time.Time
		want bool
	}{
		{sun.Add(21*time.Hour + 59*time.Minute), false},
		{sun.Add(22 * time.Hour), true},
		{sun.Add(25*time.Hour + 59*time.Minute), true},                      // Monday 01:59
		{sun.Add(26 * time.Hour), false},                                    // Monday 02:00: end is open
		{sun.Add(1 * time.Hour), false},                                     // Sunday 01:00 belongs to Saturday's window
		{sun.Add(22 * time.Hour).In(time.FixedZone("UTC+8", 8*3600)), true}, // the zone of now does not matter
	} {
		if got := w.contains(c.at); got != c.want {
			t.Errorf("contains(%s) = %v, want %v", c.at, got, c.want)
		}
	}
}

// The words a message names are isDisabled's.
func TestDisabledWordList_IsTheReadersWords(t *testing.T) {
	t.Parallel()
	got := strings.Split(disabledWordList(), ", ")
	slices.Sort(got)
	want := slices.Clone(disabledWords(t)) // a copy: disabledWords' slice is shared
	slices.Sort(want)
	if !slices.Equal(got, want) {
		t.Errorf("disabledWordList() = %q, want isDisabled's words %q", got, want)
	}
}

// The four counterexamples of #3620, through the readers the exporter
// serves from: none of them turns maintenance on, and none has an expiry
// entry; `enable` still does.
func TestResolveStateFilters_The3620Counterexamples(t *testing.T) {
	t.Parallel()
	tenants := map[string]string{
		"t1": "target: disable\n",
		"t2": "yes",
		"t3": "true",
		"t4": "enabled: false\nexpires: \"" + svFuture + "\"\n",
		"t5": "recurring:\n- weekdays: [sun]\n  window: \"02:00-06:00\"\n",
		"t6": "garbage",
		"ok": "enable",
		"tb": "target: enable\nexpires: \"" + svFuture + "\"\n",
	}
	c := ThresholdConfig{
		StateFilters: map[string]StateFilter{"maintenance": {Severity: "info"}},
		Tenants:      map[string]map[string]ScheduledValue{},
	}
	for tn, v := range tenants {
		c.Tenants[tn] = map[string]ScheduledValue{"_state_maintenance": valueAsWritten(v)}
	}
	var warns []string
	logf := func(format string, args ...any) { warns = append(warns, fmt.Sprintf(format, args...)) }
	var on []string
	for _, sf := range c.resolveStateFiltersAt(stateValueNow, logf) {
		on = append(on, sf.Tenant)
	}
	slices.Sort(on)
	if !slices.Equal(on, []string{"ok", "tb"}) {
		t.Errorf("maintenance on for %v, want [ok tb]", on)
	}
	// A WARN naming tenant, key and value for every value not read.
	for _, tn := range []string{"t2", "t3", "t6"} {
		want := fmt.Sprintf("_state_maintenance for tenant=%s: value %q is not read", tn, tenants[tn])
		if !slices.ContainsFunc(warns, func(w string) bool { return strings.Contains(w, want) }) {
			t.Errorf("no WARN %q in %q", want, warns)
		}
	}
	// No WARN for a value switched off.
	for _, w := range warns {
		if strings.Contains(w, "tenant=t1") || strings.Contains(w, "tenant=t4") || strings.Contains(w, "tenant=ok") {
			t.Errorf("unexpected WARN %q", w)
		}
	}

	var exp []string
	for _, e := range c.resolveMaintenanceExpiriesAt(stateValueNow, true, nil) {
		exp = append(exp, e.Tenant)
	}
	if !slices.Equal(exp, []string{"tb"}) {
		t.Errorf("maintenance expiries for %v, want [tb]", exp)
	}
}

// A non-maintenance filter reads the same scalars, and takes no mapping.
func TestResolveStateFilters_OtherFilterStrict(t *testing.T) {
	t.Parallel()
	c := ThresholdConfig{
		StateFilters: map[string]StateFilter{"crashloop": {Severity: "critical", DefaultState: "enable"}},
		Tenants: map[string]map[string]ScheduledValue{
			"yes":     {"_state_crashloop": valueAsWritten("yes")},
			"one":     {"_state_crashloop": valueAsWritten("1")},
			"mapping": {"_state_crashloop": valueAsWritten("target: enable\n")},
			"enable":  {"_state_crashloop": valueAsWritten("enable")},
			"absent":  {"cpu": {Default: "80"}},
		},
	}
	var on []string
	for _, sf := range c.resolveStateFiltersAt(stateValueNow, nil) {
		on = append(on, sf.Tenant)
	}
	slices.Sort(on)
	// A problem value is NOT the default_state: never on.
	if !slices.Equal(on, []string{"absent", "enable"}) {
		t.Errorf("crashloop on for %v, want [absent enable]", on)
	}
}

// The expiry reader logs a structured value's WARN only when asked to (the
// maintenance filter undeclared), and never a scalar's.
func TestResolveMaintenanceExpiries_WarnsOnlyForStructured(t *testing.T) {
	t.Parallel()
	c := ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{
		"bad-exp": {"_state_maintenance": valueAsWritten("expires: soon\n")},
		"bad-map": {"_state_maintenance": valueAsWritten("target: all\n")},
		"scalar":  {"_state_maintenance": valueAsWritten("yes")},
	}}
	var warns []string
	logf := func(format string, args ...any) { warns = append(warns, fmt.Sprintf(format, args...)) }
	if got := c.ResolveMaintenanceExpiriesAfterStateFiltersAtLogf(stateValueNow, logf); len(got) != 0 {
		t.Errorf("expiries = %+v, want none", got)
	}
	if len(warns) != 2 || !strings.Contains(strings.Join(warns, "\n"), "IGNORED") ||
		!strings.Contains(strings.Join(warns, "\n"), "tenant=bad-map") {
		t.Errorf("WARNs = %q, want the invalid-expires and the bad-map one", warns)
	}
}

// The write gate (KeyValidation) refuses a `_state_<filter>` value the
// reader does not read, through the registry's `_state_` prefix check — for
// `_state_maintenance` and for any other filter — and Ratchet makes an
// unchanged value already on disk a notice.
func TestKeyValidation_StateFilterValues(t *testing.T) {
	t.Parallel()
	refused := []struct{ key, val string }{
		{"_state_maintenance", "garbage"},
		{"_state_maintenance", "yes"},
		{"_state_maintenance", "\"1\""},
		{"_state_maintenance", "\n      target: all"},
		{"_state_maintenance", "\n      enabled: \"off\""},
		{"_state_maintenance", "\n      target: enable\n      enabled: false"},
		{"_state_maintenance", "\n      reason: planned"},
		{"_state_maintenance", "\n      expires: \"2026-07-01\""},
		// #3639: the retired cron form, with or without a switch, is refused
		// at the gate — not only unread by the exporter.
		{"_state_maintenance", "\n      recurring:\n        - cron: \"0 2 * * 0\"\n          duration: 4h"},
		{"_state_maintenance", "\n      target: enable\n      recurring:\n        - cron: \"0 2 * * 0\"\n          duration: 4h"},
		{"_state_maintenance", "\n      target: disable\n      recurring:\n        - cron: \"0 2 * * 0\"\n          duration: 4h"},
		{"_state_maintenance", "\n      recurring:\n        - weekdays: [Sun]\n          window: \"02:00-06:00\""},
		// What the schema refuses, the gate refuses (uniqueItems, minItems,
		// the window pattern).
		{"_state_maintenance", "\n      recurring:\n        - weekdays: [sun, sun]\n          window: \"02:00-06:00\""},
		{"_state_maintenance", "\n      target: enable\n      recurring: []"},
		{"_state_maintenance", "\n      recurring:\n        - weekdays: [sun]\n          window: \"2:00-6:00\""},
		{"_state_maintenance", "\n      recurring:\n        - weekdays: [sun]\n          window: \" 02:00 - 06:00 \""},
		{"_state_maintenance", "\n      recurring:\n        - weekdays: [sun]\n          window: \"+2:00-06:00\""},
		{"_state_crashloop", "\"true\""},
		{"_state_crashloop", "\n      target: enable"},
	}
	for _, c := range refused {
		v := judge(t, "", "tenants:\n  t1:\n    "+c.key+": "+c.val+"\n").Tenants["t1"]
		if countWith(v.Errors, c.key) != 1 || len(v.Values) != 1 ||
			!strings.Contains(v.Errors[0], "is not a value the exporter reads") ||
			!strings.Contains(v.Errors[0], "the state filter is not switched on while it is set") {
			t.Errorf("%s: %s → errors %q values %+v, want one refusal", c.key, c.val, v.Errors, v.Values)
		}
	}
	read := []struct{ key, val string }{
		{"_state_maintenance", "enable"},
		{"_state_maintenance", "disable"},
		{"_state_maintenance", "\n      target: disable"},
		{"_state_maintenance", "\n      enabled: false\n      recurring:\n        - weekdays: [sun]\n          window: \"02:00-06:00\""},
		{"_state_maintenance", "\n      recurring:\n        - weekdays: [sun]\n          window: \"02:00-06:00\""},
		{"_state_maintenance", "\n      expires: \"2020-01-01T00:00:00Z\""},
		{"_state_crashloop", "off"},
		{"_state_crashloop", "ENABLE"},
	}
	for _, c := range read {
		v := judge(t, "", "tenants:\n  t1:\n    "+c.key+": "+c.val+"\n").Tenants["t1"]
		if countWith(v.Errors, c.key)+len(v.Values) != 0 {
			t.Errorf("%s: %s → errors %q, want none", c.key, c.val, v.Errors)
		}
	}

	// A tenant-side null (the load's reservedNullMarker) is no key: nothing
	// to judge.
	if p := CheckReservedValue("_state_maintenance", reservedNullMarker); p != "" {
		t.Errorf("null marker: problem %q", p)
	}

	// Ratchet: unchanged on disk → a notice; changed to another bad value →
	// still refused.
	const head = "tenants:\n  t1:\n"
	for _, key := range []string{"_state_maintenance", "_state_crashloop"} {
		base := head + "    " + key + ": \"yes\"\n    cpu: \"80\"\n"
		same := judge(t, base, head+"    "+key+": yes\n    cpu: \"90\"\n").Tenants["t1"]
		if countWith(same.Errors, key) != 0 || countWith(same.Notices, key) != 1 || len(same.Values) != 0 {
			t.Errorf("%s unchanged: errors %q notices %q, want a notice", key, same.Errors, same.Notices)
		}
		other := judge(t, base, head+"    "+key+": \"no\"\n    cpu: \"80\"\n").Tenants["t1"]
		if countWith(other.Errors, key) != 1 || countWith(other.Notices, key) != 0 {
			t.Errorf("%s changed: errors %q notices %q, want a refusal", key, other.Errors, other.Notices)
		}
	}
	// Two structured values the reader does not read, for the same reason:
	// a write that swaps one for the other is refused, not a notice.
	swapBase := head + "    _state_maintenance:\n      reason: a\n"
	swapped := judge(t, swapBase, head+"    _state_maintenance:\n      reason: b\n").Tenants["t1"]
	if countWith(swapped.Errors, "_state_maintenance") != 1 || countWith(swapped.Notices, "_state_maintenance") != 0 {
		t.Errorf("swapped unread value: errors %q notices %q, want a refusal", swapped.Errors, swapped.Notices)
	}
	// SelfRatchet (what is on disk, GET): a notice.
	self := gateDecode(t, head+"    _state_maintenance: {target: disable, enabled: true}\n").ValidateTenantKeysByTenant().SelfRatchet()
	if v := self.Tenants["t1"]; len(v.Errors) != 0 || countWith(v.Notices, "_state_maintenance") != 1 {
		t.Errorf("SelfRatchet = %+v", v)
	}
}

// #3639: a passed expires does not end the recurring windows, and the
// exporter says so; a future one, or no windows, is not warned about.
func TestMaintenanceExpiries_WarnWhenExpiredButRecurring(t *testing.T) {
	t.Parallel()
	win := "recurring:\n- weekdays: [sun]\n  window: \"02:00-06:00\"\n"
	c := ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{
		"past-rec": {"_state_maintenance": valueAsWritten("expires: \"" + svPast + "\"\n" + win)},
		"past":     {"_state_maintenance": valueAsWritten("expires: \"" + svPast + "\"\n")},
		"future":   {"_state_maintenance": valueAsWritten("expires: \"" + svFuture + "\"\n" + win)},
	}}
	// Both collector paths: the maintenance filter declared (what ships —
	// the only config where the windows switch anything on, and where the
	// parse WARNs are deduped away from this reader) and not declared.
	for _, declared := range []bool{true, false} {
		c := c
		if declared {
			c.StateFilters = map[string]StateFilter{"maintenance": {Severity: "info"}}
		}
		var warns []string
		c.ResolveMaintenanceExpiriesAfterStateFiltersAtLogf(stateValueNow, func(f string, a ...any) { warns = append(warns, fmt.Sprintf(f, a...)) })
		if len(warns) != 1 || !strings.Contains(warns[0], "tenant=past-rec") || !strings.Contains(warns[0], "1 recurring window(s)") {
			t.Errorf("declared=%v: warns = %q, want one for past-rec", declared, warns)
		}
	}
}
