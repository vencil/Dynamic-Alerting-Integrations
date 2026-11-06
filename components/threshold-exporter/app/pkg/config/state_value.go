package config

// state_value.go (#3620) — the ONE reading of a `_state_<filter>` value.
//
// ⛔ ONE READER. resolveStateFiltersAt (user_state_filter rows),
// resolveMaintenanceExpiriesAt (da_config_event{event="maintenance_expired"}),
// the write gate's value check (registry `prefixes._state_.value.check`) and
// da-guard's maintenance_recurring (what maintenance-scheduler creates
// silences from) all classify a value here. Before #3620 each reader made its
// own guess — any value that was not a disable word switched the filter on,
// so `{target: disable}`, "yes" and garbage all muted alerts.
//
// The reading (owner-approved semantics, #3620):
//
//   - Scalar: `enable` (trimmed, any case) is on; a disable word (isDisabled)
//     is off; anything else is a problem.
//   - Mapping: only `_state_maintenance` takes one (on any other filter it is
//     a problem). Its fields are target / enabled / expires / reason /
//     recurring; any other field is a problem.
//   - The switch is `target` (enable or a disable word) and/or `enabled` (a
//     YAML boolean). A target that is neither, a non-boolean `enabled`, or a
//     target and enabled that disagree, is a problem.
//   - Switch off → off, whatever else the mapping says.
//   - Switch on → on, until `expires` when given (RFC3339; past → off).
//   - No switch: `expires` → on until it; only `recurring` → not on here (the
//     maintenance-scheduler silences the windows); neither → a problem.
//   - An `expires` that is not RFC3339 is #2000's fail-closed case: not on,
//     and the setting as a whole (recurring included) is ignored.
//
// A problem is never on: fail-closed — more alerts, never fewer.

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// stateValueKind is what a `_state_<filter>` value says.
type stateValueKind int

const (
	// stateValueOff: a disable word, or a switch that says off.
	stateValueOff stateValueKind = iota
	// stateValueOn: `enable`, or a mapping whose switch or expires turns it
	// on — until expires, when it has one (stateValueReading.onAt).
	stateValueOn
	// stateValueRecurringOnly: a mapping with recurring windows and neither
	// a switch nor expires — the filter is not on; the scheduler silences
	// the windows.
	stateValueRecurringOnly
	// stateValueInvalidExpires: #2000 — an expires that is not RFC3339; the
	// whole setting is ignored.
	stateValueInvalidExpires
	// stateValueProblem: a value the reader does not read; not on.
	stateValueProblem
)

// stateValueFields are the fields of _state_maintenance's structured form,
// sorted. TestMaintenanceSchemaMatchesReader pins them to the schema's.
var stateValueFields = []string{"enabled", "expires", "reason", "recurring", "target"}

// stateValueReading is one `_state_<filter>` value as the reader reads it.
type stateValueReading struct {
	kind stateValueKind
	// structured: the value is a mapping.
	structured bool
	// hasExpires / expires / expiresText: the time-box of an on value;
	// expiresText is also the text of an invalid one.
	hasExpires  bool
	expires     time.Time
	expiresText string
	reason      string
	// recurring: the windows the maintenance-scheduler acts on; nil unless
	// the value is on or recurring-only.
	recurring []RecurringSchedule
	// problem: why the value is not read (kind stateValueProblem), or the
	// parse error of an invalid expires — tenant-free.
	problem string
}

// stateValueOf classifies sv, the value of `_state_<filterName>`.
func stateValueOf(filterName string, sv ScheduledValue) stateValueReading {
	raw := strings.TrimSpace(sv.Default)
	// A mapping reaches here as YAML text (ScheduledValue.UnmarshalYAML
	// re-marshals a mapping without a `default` key), so the value is
	// decoded and its kind looked at — never a substring of it: a `reason`
	// that says "expires:" is not a time-box.
	var doc any
	if err := yaml.Unmarshal([]byte(raw), &doc); err != nil {
		// Not YAML at all (`expires: [`): a broken mapping as far as the
		// reader can tell — structured, so the expiry reader names it too
		// when the state-filter reader does not reach it (#2426).
		return stateValueReading{kind: stateValueProblem, structured: true,
			problem: fmt.Sprintf("it does not parse as YAML: %v", err)}
	}
	switch m := doc.(type) {
	case map[string]any:
		return structuredStateValueOf(filterName, raw, m)
	case map[any]any:
		return stateValueReading{kind: stateValueProblem, structured: true,
			problem: fmt.Sprintf("the structured form takes only the fields %s", strings.Join(stateValueFields, ", "))}
	}
	lower := strings.ToLower(raw)
	switch {
	case lower == "enable":
		return stateValueReading{kind: stateValueOn}
	case isDisabled(lower):
		return stateValueReading{kind: stateValueOff}
	}
	return stateValueReading{kind: stateValueProblem,
		problem: fmt.Sprintf("%q is neither %q nor a word that switches it off (%s)", raw, "enable", disabledWordList())}
}

// structuredStateValueOf reads a mapping.
func structuredStateValueOf(filterName, raw string, m map[string]any) stateValueReading {
	bad := func(format string, args ...any) stateValueReading {
		return stateValueReading{kind: stateValueProblem, structured: true, problem: fmt.Sprintf(format, args...)}
	}
	if filterName != maintenanceFilterName {
		return bad("only _state_%s takes the structured form; this filter takes %q or a word that switches it off (%s)",
			maintenanceFilterName, "enable", disabledWordList())
	}
	var unknown []string
	for k := range m {
		if !containsString(stateValueFields, k) {
			unknown = append(unknown, k)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return bad("unknown field(s) %s (valid: %s)", strings.Join(unknown, ", "), strings.Join(stateValueFields, ", "))
	}
	// Shapes first, from the generic decode, so a wrong shape is named as
	// such rather than as the typed decode's error.
	if v, ok := m["target"]; ok && !isYAMLScalar(v) {
		return bad("target must be %q or a word that switches it off (%s)", "enable", disabledWordList())
	}
	if v, ok := m["enabled"]; ok {
		// Read from the generic decode: decoded into a bool, yaml.v3 takes
		// `off` / `no` / `yes` as booleans, while everywhere else in a
		// conf.d they are strings.
		if _, isBool := v.(bool); !isBool {
			return bad("enabled must be true or false (a YAML boolean), not %v", v)
		}
	}
	if v, ok := m["expires"]; ok && v != nil && !isYAMLScalar(v) {
		return bad("expires must be an RFC3339 timestamp (e.g. 2026-07-01T00:00:00Z)")
	}
	var parsed maintenanceModeStructured
	if err := yaml.Unmarshal([]byte(raw), &parsed); err != nil {
		return bad("the structured form does not parse: %v", err)
	}

	// The switch: target and/or enabled.
	var switchOn, hasSwitch bool
	if _, ok := m["target"]; ok {
		t := strings.ToLower(strings.TrimSpace(parsed.Target))
		switch {
		case t == "enable":
			switchOn = true
		case isDisabled(t):
			switchOn = false
		default:
			return bad("target %q is neither %q nor a word that switches it off (%s)", parsed.Target, "enable", disabledWordList())
		}
		hasSwitch = true
	}
	if v, ok := m["enabled"]; ok {
		b := v.(bool) // checked above
		if hasSwitch && b != switchOn {
			return bad("target %q and enabled %v disagree", parsed.Target, b)
		}
		switchOn, hasSwitch = b, true
	}
	if hasSwitch && !switchOn {
		return stateValueReading{kind: stateValueOff, structured: true}
	}

	r := stateValueReading{kind: stateValueOn, structured: true, reason: parsed.Reason}
	if parsed.Expires != "" {
		t, err := time.Parse(time.RFC3339, parsed.Expires)
		if err != nil {
			return stateValueReading{kind: stateValueInvalidExpires, structured: true,
				expiresText: parsed.Expires, problem: err.Error()}
		}
		r.hasExpires, r.expires, r.expiresText = true, t, parsed.Expires
	}
	r.recurring = recurringAsWritten(parsed.Recurring, m["recurring"])
	if !hasSwitch && !r.hasExpires {
		if len(parsed.Recurring) == 0 {
			return bad("it has no switch (target / enabled), no expires and no recurring windows, so it says nothing")
		}
		r.kind = stateValueRecurringOnly
	}
	return r
}

// recurringAsWritten is typed, with a cron or duration the file does not
// write as a YAML string left empty: the typed decode turns `duration: 3600`
// into "3600", which the scheduler would read as minutes. An empty field is
// one the scheduler skips with a WARN, as it did before the windows came
// from here.
func recurringAsWritten(typed []RecurringSchedule, generic any) []RecurringSchedule {
	entries, _ := generic.([]any)
	if len(typed) == 0 || len(entries) != len(typed) {
		return typed
	}
	out := make([]RecurringSchedule, len(typed))
	for i, r := range typed {
		e, _ := entries[i].(map[string]any)
		if _, ok := e["cron"].(string); !ok {
			r.Cron = ""
		}
		if _, ok := e["duration"].(string); !ok {
			r.Duration = ""
		}
		out[i] = r
	}
	return out
}

// onAt reports whether r turns its filter on at now.
func (r stateValueReading) onAt(now time.Time) bool {
	return r.kind == stateValueOn && !(r.hasExpires && now.After(r.expires))
}

// warn is the reader's WARN line for r ("" when there is none).
func (r stateValueReading) warn(tenant, filterName, value string) string {
	switch r.kind {
	case stateValueInvalidExpires:
		return maintenanceExpiresIgnoredWarn(tenant, r.expiresText, fmt.Errorf("%s", r.problem))
	case stateValueProblem:
		return fmt.Sprintf("WARN: _state_%s for tenant=%s: value %q is not read: %s — the state filter is NOT switched on while it is set (fail-closed, #3620)",
			filterName, tenant, value, r.problem)
	}
	return ""
}

// writeProblem is r's problem for the write gate ("" when the reader reads
// r): a problem, or #2000's invalid expires.
func (r stateValueReading) writeProblem() string {
	switch r.kind {
	case stateValueProblem:
		return r.problem
	case stateValueInvalidExpires:
		return fmt.Sprintf("expires %q is not RFC3339 (e.g. 2026-07-01T00:00:00Z): %s", r.expiresText, r.problem)
	}
	return ""
}

// canonical is r's comparable form for KeyValidation.Ratchet: two values
// with the same canonical form are read the same way. A string, never a
// struct holding a slice (== on such an interface value panics).
func (r stateValueReading) canonical() string {
	var b strings.Builder
	fmt.Fprintf(&b, "kind=%d", r.kind)
	switch r.kind {
	case stateValueProblem, stateValueInvalidExpires:
		fmt.Fprintf(&b, "|problem=%q|expires=%q", r.problem, r.expiresText)
	case stateValueOn, stateValueRecurringOnly:
		if r.hasExpires {
			fmt.Fprintf(&b, "|expires=%s", r.expires.UTC().Format(time.RFC3339Nano))
		}
		fmt.Fprintf(&b, "|reason=%q", r.reason)
		for _, s := range r.recurring {
			fmt.Fprintf(&b, "|recurring=%q,%q,%q", s.Cron, s.Duration, s.Reason)
		}
	}
	return b.String()
}

// isYAMLScalar reports whether v, from a generic yaml.v3 decode, is a
// scalar (not a mapping, a sequence or null).
func isYAMLScalar(v any) bool {
	switch v.(type) {
	case nil, map[string]any, map[any]any, []any:
		return false
	}
	return true
}

func containsString(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// disabledWordList is the words isDisabled takes, for a message.
func disabledWordList() string {
	return "disable, disabled, off, false"
}

// stateFilterValueProblem is the registry check `state_filter`
// (prefixes._state_.value.check): "" when the reader reads sv as the value
// of key, otherwise why it does not.
// A tenant-side null is no key (tenantReserved, #3341): nothing to judge.
func stateFilterValueProblem(key string, sv ScheduledValue) string {
	if sv.deleted {
		return ""
	}
	return stateValueOf(strings.TrimPrefix(key, "_state_"), sv).writeProblem()
}

// stateFilterValueReading is the check's reading, for Ratchet.
func stateFilterValueReading(key string, sv ScheduledValue) any {
	if sv.deleted {
		return "deleted"
	}
	return stateValueOf(strings.TrimPrefix(key, "_state_"), sv).canonical()
}

// MaintenanceRecurringOfValue is MaintenanceRecurringOf for a value as a
// generic decode holds it (an effective_config entry: a scalar or a
// mapping); nil is no key — no windows.
func MaintenanceRecurringOfValue(v any) []RecurringSchedule {
	if v == nil {
		return nil
	}
	out, err := yaml.Marshal(v)
	if err != nil {
		return nil
	}
	var sv ScheduledValue
	if err := yaml.Unmarshal(out, &sv); err != nil {
		return nil
	}
	return MaintenanceRecurringOf(sv)
}

// MaintenanceRecurringOf is the recurring maintenance windows the
// maintenance-scheduler creates silences for, from a tenant's
// `_state_maintenance` value: the windows of a value that is on (whether or
// not its expires has passed — the windows are not time-boxed by it) or
// recurring-only; none for a value switched off, one with an invalid
// expires, or one the reader does not read (#3620). da-guard serves it as
// a tenant's maintenance_recurring, so the scheduler applies the reader's
// switch instead of re-deriving it.
func MaintenanceRecurringOf(sv ScheduledValue) []RecurringSchedule {
	r := stateValueOf(maintenanceFilterName, sv)
	if r.kind != stateValueOn && r.kind != stateValueRecurringOnly {
		return nil
	}
	return r.recurring
}
