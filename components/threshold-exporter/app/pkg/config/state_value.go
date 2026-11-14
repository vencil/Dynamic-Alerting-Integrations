package config

// state_value.go (#3620) — the ONE reading of a `_state_<filter>` value.
//
// ⛔ ONE READER. resolveStateFiltersAt (user_state_filter rows),
// resolveMaintenanceExpiriesAt (da_config_event{event="maintenance_expired"}),
// and the write gate's value check (registry `prefixes._state_.value.check`)
// all classify a value here. Before #3620 each reader made its
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
//   - Switch off → off, whatever the other fields' values say — once the
//     mapping reads: an unknown field, a wrong shape, or a recurring entry
//     the reader does not read is a problem even beside a switch that says
//     off (still not on; the exporter WARNs and the write gate refuses).
//   - Switch on → on, until `expires` when given (RFC3339; past → off).
//   - No switch: `expires` → on until it; only `recurring` → on inside a
//     window (#3639); neither → a problem.
//   - `recurring` (maintenance_window.go): weekly UTC windows, on inside
//     any of them whatever the switch's expires says; an entry the reader
//     does not read makes the whole value a problem.
//   - An `expires` that is not RFC3339 is #2000's fail-closed case: not on,
//     and the setting as a whole (recurring included) is ignored.
//
// A problem is never on: fail-closed — more alerts, never fewer.

import (
	"fmt"
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
	// a switch nor expires — the filter is on inside a window (#3639).
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
	// windows: the recurring maintenance windows (#3639), which turn the
	// filter on while now is inside one; nil unless the value is on or
	// recurring-only. Not time-boxed by expires.
	windows []maintenanceWindow
	// problem: why the value is not read (kind stateValueProblem) —
	// tenant-free and built from constants only, never from the value
	// (#3703: it is the key verdict's sentence; an index or a count is the
	// most it carries).
	problem string
	// expiresErr: the parse error of an invalid expires (kind
	// stateValueInvalidExpires), for the reader's own WARN line only — it
	// quotes the value.
	expiresErr string
}

// stateValueOf classifies sv, the value of `_state_<filterName>`.
//
// #3638: whether the value is a mapping is the structure's (structureOf),
// never the text's — a scalar `"target: enable"` is the scalar it was written
// as, not the mapping its text reads like, and every scalar is read without
// a YAML decode. A sequence is its text read as a scalar, as before.
func stateValueOf(filterName string, sv ScheduledValue) stateValueReading {
	node, structured, err := structureOf(sv)
	if !structured {
		return scalarStateValueOf(strings.TrimSpace(sv.Default))
	}
	var doc any
	if err == nil {
		err = node.Decode(&doc)
	}
	if err != nil {
		// Not reachable for a value UnmarshalYAML built; named as a broken
		// mapping — structured, so the expiry reader names it too when the
		// state-filter reader does not reach it (#2426).
		return stateValueReading{kind: stateValueProblem, structured: true,
			problem: "it does not parse as YAML"}
	}
	switch m := doc.(type) {
	case map[string]any:
		return structuredStateValueOf(filterName, node, m)
	case map[any]any:
		return stateValueReading{kind: stateValueProblem, structured: true,
			problem: fmt.Sprintf("the structured form takes only the fields %s", strings.Join(stateValueFields, ", "))}
	}
	return scalarStateValueOf(strings.TrimSpace(sv.Default))
}

// scalarStateValueOf reads raw, a scalar value.
func scalarStateValueOf(raw string) stateValueReading {
	lower := strings.ToLower(raw)
	switch {
	case lower == "enable":
		return stateValueReading{kind: stateValueOn}
	case isDisabled(lower):
		return stateValueReading{kind: stateValueOff}
	}
	return stateValueReading{kind: stateValueProblem,
		problem: fmt.Sprintf("it is neither %q nor a word that switches it off (%s)", "enable", disabledWordList())}
}

// structuredStateValueOf reads a mapping: node, as m its generic decode.
func structuredStateValueOf(filterName string, node *yaml.Node, m map[string]any) stateValueReading {
	bad := func(format string, args ...any) stateValueReading {
		return stateValueReading{kind: stateValueProblem, structured: true, problem: fmt.Sprintf(format, args...)}
	}
	if filterName != maintenanceFilterName {
		return bad("only _state_%s takes the structured form; this filter takes %q or a word that switches it off (%s)",
			maintenanceFilterName, "enable", disabledWordList())
	}
	unknown := 0
	for k := range m {
		if !containsString(stateValueFields, k) {
			unknown++
		}
	}
	if unknown > 0 {
		return bad("%d unknown field(s) (valid: %s)", unknown, strings.Join(stateValueFields, ", "))
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
			return bad("enabled must be true or false (a YAML boolean)")
		}
	}
	if v, ok := m["expires"]; ok && v != nil && !isYAMLScalar(v) {
		return bad("expires must be an RFC3339 timestamp (e.g. 2026-07-01T00:00:00Z)")
	}
	// An `expires:` written empty is #2000's case, not "no expires": a
	// maintenance window must not lose its end to a blank field.
	if v, ok := m["expires"]; ok && (v == nil || strings.TrimSpace(fmt.Sprint(v)) == "") {
		return stateValueReading{kind: stateValueInvalidExpires, structured: true,
			expiresErr: "expires is written but empty"}
	}
	windows, problem := recurringWindowsOf(m["recurring"])
	if problem != "" {
		return bad("%s", problem)
	}
	var parsed maintenanceModeStructured
	if err := node.Decode(&parsed); err != nil {
		return bad("the structured form does not parse")
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
			return bad("target is neither %q nor a word that switches it off (%s)", "enable", disabledWordList())
		}
		hasSwitch = true
	}
	if v, ok := m["enabled"]; ok {
		b := v.(bool) // checked above
		if hasSwitch && b != switchOn {
			return bad("target and enabled disagree")
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
				expiresText: parsed.Expires, expiresErr: err.Error()}
		}
		r.hasExpires, r.expires, r.expiresText = true, t, parsed.Expires
	}
	r.windows = windows
	if !hasSwitch && !r.hasExpires {
		if len(windows) == 0 {
			return bad("it has no switch (target / enabled), no expires and no recurring windows, so it says nothing")
		}
		r.kind = stateValueRecurringOnly
	}
	return r
}

// onAt reports whether r turns its filter on at now: an on value until its
// expires, and — on or recurring-only — inside any recurring window, which
// expires does not limit (#3639).
func (r stateValueReading) onAt(now time.Time) bool {
	if r.kind == stateValueOn && (!r.hasExpires || !now.After(r.expires)) {
		return true
	}
	if r.kind != stateValueOn && r.kind != stateValueRecurringOnly {
		return false
	}
	for _, w := range r.windows {
		if w.contains(now) {
			return true
		}
	}
	return false
}

// warn is the reader's WARN line for r ("" when there is none).
func (r stateValueReading) warn(tenant, filterName, value string) string {
	switch r.kind {
	case stateValueInvalidExpires:
		return maintenanceExpiresIgnoredWarn(tenant, r.expiresText, fmt.Errorf("%s", r.expiresErr))
	case stateValueProblem:
		return fmt.Sprintf("WARN: _state_%s for tenant=%s: value %q is not read: %s — the state filter is NOT switched on while it is set (fail-closed, #3620)",
			filterName, tenant, value, r.problem)
	}
	return ""
}

// writeProblem is r's problem for the write gate and the key verdict (""
// when the reader reads r): a problem, or #2000's invalid expires. Constants
// only (#3703) — the parse error of an expires quotes the value, so it stays
// in the reader's WARN (warn).
func (r stateValueReading) writeProblem() string {
	switch r.kind {
	case stateValueProblem:
		return r.problem
	case stateValueInvalidExpires:
		return expiresNotRFC3339Problem
	}
	return ""
}

// expiresNotRFC3339Problem is the one sentence for an `expires` the reader
// does not read as RFC3339, `_state_maintenance`'s and `_silent_mode`'s.
const expiresNotRFC3339Problem = "expires is not RFC3339 (e.g. 2026-07-01T00:00:00Z)"

// canonical is r's comparable form for KeyValidation.Ratchet: two values
// with the same canonical form are read the same way. A string, never a
// struct holding a slice (== on such an interface value panics).
func (r stateValueReading) canonical() string {
	var b strings.Builder
	fmt.Fprintf(&b, "kind=%d", r.kind)
	switch r.kind {
	case stateValueProblem, stateValueInvalidExpires:
		fmt.Fprintf(&b, "|problem=%q|expires=%q", r.problem+r.expiresErr, r.expiresText)
	case stateValueOn, stateValueRecurringOnly:
		if r.hasExpires {
			fmt.Fprintf(&b, "|expires=%s", r.expires.UTC().Format(time.RFC3339Nano))
		}
		fmt.Fprintf(&b, "|reason=%q", r.reason)
		for _, w := range r.windows {
			fmt.Fprintf(&b, "|recurring=%v,%q,%q", w.days, w.text, w.reason)
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
	r := stateValueOf(strings.TrimPrefix(key, "_state_"), sv)
	if r.kind == stateValueProblem || r.kind == stateValueInvalidExpires {
		// Two values the reader does not read are alike only when they are
		// the same text: otherwise a write could replace one unread value
		// with another and get a NOTICE instead of a refusal.
		return r.canonical() + "|raw=" + strings.TrimSpace(sv.Default)
	}
	return r.canonical()
}
