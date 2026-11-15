package config

// #3227 (ADR-038 decision 12): a reserved key's VALUE is judged at the write
// gate by the exporter's own reading of it (CheckReservedValue) — and, #3267,
// a threshold key's value by its row resolvers' (thresholdRefusalIn) —
// and a value
// already on disk that a write leaves as the exporter reads it does not
// block that write — it is reported as a notice instead (Ratchet).

import (
	"fmt"
	"strings"
)

// ValueFinding is one KeyValidation.Errors entry about a reserved key's
// value: the exporter's reader ignores it, or ignores part of it.
type ValueFinding struct {
	Tenant string
	Key    string // canonical spelling
	// Value is the value as written. Ratchet compares it with the value the
	// key had as the finding's kind says (unchanged): the exporter's reading
	// of it (readAlike) for a value the reader ignores, the value itself,
	// schedule included (sameSchedule), for a schedule.
	Value ScheduledValue
	// Problem is the tenant-free reason: the reader's (CheckReservedValue),
	// or scheduleProblem.
	Problem string
	// Message is the Errors entry, word for word.
	Message string
	// schedule marks the #3269 kind (newScheduleFinding): a reserved key
	// written with a schedule its readers ignore.
	schedule bool
	// threshold marks the #3267 kind (newThresholdFinding): a threshold
	// key's value the row resolvers refuse. Value is compared by the
	// resolver's reading (readAlike), like a reserved value.
	threshold bool
	// refusal is the threshold kind's thresholdRefusal.
	refusal thresholdRefusal
	// dead is the #3502 kind's entries (newDeadEntryFinding): a threshold
	// key's schedule with entries /metrics never serves. nil for any other
	// kind.
	dead []DeadEntry
	// invalid is the #3683 kind's entries (newInvalidWindowFinding): a
	// threshold key's schedule with a `window:` parseTimeWindow refuses.
	// nil for any other kind.
	invalid []invalidWindow
}

// newThresholdFinding is the finding for sv, the value of threshold key key
// (canonical spelling), when the row resolvers of a tree whose root
// `defaults:` (canonical) is defaults refuse it (thresholdRefusalIn).
func newThresholdFinding(tenant string, defaults map[string]float64, key string, sv ScheduledValue) (ValueFinding, bool) {
	r, ok := thresholdRefusalIn(defaults, key, sv)
	if !ok {
		return ValueFinding{}, false
	}
	return ValueFinding{
		Tenant:    tenant,
		Key:       key,
		Value:     sv,
		Problem:   strings.TrimPrefix(r.Message, key+": "),
		Message:   "WARN: tenant=" + tenant + ": " + r.Message,
		threshold: true,
		refusal:   r,
	}, true
}

// newDeadEntryFinding is the finding for sv, the value of threshold key key
// (canonical spelling), when its schedule has entries /metrics serves at no
// minute of the day (ScheduledValue.DeadEntries, #3502).
func newDeadEntryFinding(tenant, key string, sv ScheduledValue) (ValueFinding, bool) {
	des := sv.DeadEntries()
	if len(des) == 0 {
		return ValueFinding{}, false
	}
	return ValueFinding{
		Tenant:  tenant,
		Key:     key,
		Value:   sv,
		Problem: deadEntryProblem,
		Message: fmt.Sprintf("WARN: tenant=%s: %s writes a schedule %s: %s. Remove %s, or reorder the windows "+
			"(the first window that contains a minute serves it)",
			tenant, key, deadEntryProblem, DeadEntriesText(des), deadEntryWhat(des)),
		dead: des,
	}, true
}

// invalidWindow is one override of a schedule whose `window:` the exporter
// cannot read (parseTimeWindow refuses it; the not_served verdict
// window_invalid): it applies at no minute, so its value is never served.
type invalidWindow struct {
	entry, window, value string
}

// invalidWindowsOf is every override of sv whose `window:` parseTimeWindow
// refuses, in the order written; nil when there is none.
func invalidWindowsOf(sv ScheduledValue) []invalidWindow {
	var out []invalidWindow
	for i, o := range sv.Overrides {
		if _, _, ok := parseTimeWindow(o.Window, nil); !ok {
			out = append(out, invalidWindow{entry: overrideEntry(i), window: o.Window, value: o.Value})
		}
	}
	return out
}

// invalidWindowsReading is ws as one string — entry, window and value of
// each, in order — what makes two window_invalid findings the same one for
// the ratchet: as written, so a write that changes the window or the value
// of such an entry, or adds another one, is a new finding (as
// DeadEntriesReading is for a dead entry).
func invalidWindowsReading(ws []invalidWindow) string {
	var b strings.Builder
	for i, w := range ws {
		if i > 0 {
			b.WriteString("\x00")
		}
		// Each field quoted: a window or value can hold any byte, so a
		// bare separator would let two different entries read alike.
		fmt.Fprintf(&b, "%q\x01%q\x01%q", w.entry, w.window, w.value)
	}
	return b.String()
}

// invalidWindowsText is ws in words, for a message.
func invalidWindowsText(ws []invalidWindow) string {
	parts := make([]string, len(ws))
	for i, w := range ws {
		parts[i] = fmt.Sprintf("%s `%s` → %q", w.entry, w.window, w.value)
	}
	return strings.Join(parts, "; ")
}

// invalidWindowProblem is a window_invalid finding's tenant-free reason.
const invalidWindowProblem = "with a `window:` that is not a UTC HH:MM-HH:MM with start different from end, " +
	"so that entry never applies"

// newInvalidWindowFinding is the finding for sv, the value of threshold key
// key (canonical spelling), when a `window:` of its schedule is one
// parseTimeWindow refuses (#3683) — the not_served verdict window_invalid,
// refused where it is written as a dead entry is.
func newInvalidWindowFinding(tenant, key string, sv ScheduledValue) (ValueFinding, bool) {
	ws := invalidWindowsOf(sv)
	if len(ws) == 0 {
		return ValueFinding{}, false
	}
	return ValueFinding{
		Tenant:  tenant,
		Key:     key,
		Value:   sv,
		Problem: invalidWindowProblem,
		Message: fmt.Sprintf("WARN: tenant=%s: %s writes a schedule %s: %s. Write each window as a UTC "+
			"HH:MM-HH:MM whose start differs from its end, or remove %s",
			tenant, key, invalidWindowProblem, invalidWindowsText(ws), invalidWindowWhat(ws)),
		invalid: ws,
	}, true
}

// invalidWindowWhat names what to remove: "that entry" for one, "those
// entries" for more.
func invalidWindowWhat(ws []invalidWindow) string {
	if len(ws) == 1 {
		return "that entry"
	}
	return "those entries"
}

// deadEntriesJudged reports whether the write gate judges the dead entries
// — and, #3683, the windows it cannot read (newInvalidWindowFinding) — of
// sv, written under key (canonKey once #1231-canonicalized): only a value
// /metrics may serve, in the spelling the tenant's map serves (normalized:
// of two spellings of one dimensional key, the one normalizeKeys keeps). A
// key no reader reads is the shared key verdict's unread, and a losing
// spelling is spelling_duplicate; neither serves its schedule, so neither
// has a dead entry to report (#3502).
//
// "May serve" is the resolver's own entry test for the key's shape (#3718),
// the question the shared key verdict asks of the served reading:
//   - a dimensional key: dimensionalRowRead on the root `dimensions:` dims —
//     every label declared under its base (#3684), whether or not the base
//     has a platform value (ADR-038 §11: `dimensions:` is the declaration);
//   - a `<key>_critical` key: criticalRowRead — its base valued in the root
//     `defaults:` (`optional_overrides:` declares no critical tier) — or, as
//     the base phase reads it, the `<key>_critical` itself valued there;
//   - any other key: its platform key (PlatformKeyFor) valued in the root
//     `defaults:` or declared under `optional_overrides:`.
//
// cmd/da-guard's TestDeadEntries_WriteGateAndKeyVerdictAgree holds this to
// the shared key verdict over every layout × key shape.
func deadEntriesJudged(key, canonKey string, sv ScheduledValue, normalized map[string]ScheduledValue,
	canonDefaults map[string]float64, canonOptional map[string]struct{}, dims map[string][]string) bool {
	if len(sv.Overrides) == 0 {
		return false
	}
	base, ok := PlatformKeyFor(canonKey)
	if !ok {
		return false
	}
	switch {
	case strings.Contains(canonKey, "{"):
		if !dimensionalRowRead(canonKey, dims) {
			return false
		}
	case strings.HasSuffix(canonKey, criticalSuffix):
		// The critical phase reads it when its base is valued; the base
		// phase reads it as a plain key when the root `defaults:` values the
		// `<key>_critical` itself.
		_, direct := canonDefaults[canonKey]
		if _, read := criticalRowRead(canonKey, canonDefaults); !read && !direct {
			return false
		}
	default:
		_, valued := canonDefaults[base]
		_, declared := canonOptional[base]
		if !valued && !declared {
			return false
		}
	}
	if c, dim := canonicalDimensionalKey(key); dim {
		if served, has := normalized[c]; has && !sameSchedule(served, sv) {
			return false
		}
	}
	return true
}

// deadEntryProblem is a dead-entry finding's tenant-free reason.
const deadEntryProblem = "with entries /metrics serves at no minute of the day"

// DeadEntriesText is des in words, for a message: each entry, its window and
// value as written, and why it never serves.
func DeadEntriesText(des []DeadEntry) string {
	parts := make([]string, len(des))
	for i, d := range des {
		switch d.Reason {
		case DeadEntryDefaultCovered:
			parts[i] = fmt.Sprintf("its default %q (the windows cover the whole UTC day)", d.Value)
		default:
			parts[i] = fmt.Sprintf("%s `%s` → %q (the windows written before it cover every minute of it)",
				d.Entry, d.Window, d.Value)
		}
	}
	return strings.Join(parts, "; ")
}

// deadEntryWhat names what to remove: "it" for one entry, "them" for more.
func deadEntryWhat(des []DeadEntry) string {
	if len(des) == 1 {
		return "it"
	}
	return "them"
}

func newValueFinding(tenant, key string, sv ScheduledValue, problem string) ValueFinding {
	return ValueFinding{
		Tenant:  tenant,
		Key:     key,
		Value:   sv,
		Problem: problem,
		Message: fmt.Sprintf("WARN: tenant=%s: %s %q is not a value the exporter reads (%s) — %s",
			tenant, key, strings.TrimSpace(sv.Default), problem, valueEffect(key)),
	}
}

// scheduleProblem is a schedule finding's tenant-free reason.
const scheduleProblem = "a reserved key does not support a time window or expires"

// newScheduleFinding is the finding for sv, the value of reserved key key
// written with a schedule (scheduledReserved, #3269).
func newScheduleFinding(tenant, key string, sv ScheduledValue) ValueFinding {
	return ValueFinding{
		Tenant:  tenant,
		Key:     key,
		Value:   sv,
		Problem: scheduleProblem,
		Message: fmt.Sprintf("WARN: tenant=%s: %s writes %s (%s) — only its default %q is read, the rest is ignored. "+
			"Remove %s%s",
			tenant, key, scheduleFields(sv), scheduleProblem,
			strings.TrimSpace(sv.Default), scheduleFields(sv), scheduleHint(key)),
		schedule: true,
	}
}

// unchanged reports whether prev, the value f.Key had before the write, is
// the value f is about as far as f's kind is concerned.
func (f ValueFinding) unchanged(prev ScheduledValue) bool {
	if f.schedule {
		// As written, not as read: the reading of a schedule is its
		// `default`, so comparing readings would let a write that moves
		// or adds a window through as "unchanged".
		return sameSchedule(prev, f.Value)
	}
	if f.dead != nil {
		// #3502: the dead entries as written — entry, window, value,
		// reason (DeadEntriesReading, the reading da-guard write's ratchet
		// compares too), so a write that changes one or makes another dead
		// is not "unchanged".
		return DeadEntriesReading(prev.DeadEntries()) == DeadEntriesReading(f.dead)
	}
	if f.invalid != nil {
		// #3683: as a dead entry — the entries as written, value included:
		// the value under such a window is not judged as a threshold
		// (thresholdRefusalIn), so a write that changes it is new.
		return invalidWindowsReading(invalidWindowsOf(prev)) == invalidWindowsReading(f.invalid)
	}
	return readAlike(f.Key, prev, f.Value)
}

// notice is the advisory Ratchet puts in the finding's place.
func (f ValueFinding) notice() string {
	if f.threshold {
		return f.refusal.notice(f.Tenant)
	}
	if f.dead != nil {
		return fmt.Sprintf("NOTICE: tenant=%s: %s writes a schedule %s: %s. "+
			"It is already in the tenant file, so it does not block a write that leaves it exactly as it is; "+
			"remove %s the next time %s is changed",
			f.Tenant, f.Key, f.Problem, DeadEntriesText(f.dead), deadEntryWhat(f.dead), f.Key)
	}
	if f.invalid != nil {
		return fmt.Sprintf("NOTICE: tenant=%s: %s writes a schedule %s: %s. "+
			"It is already in the tenant file, so it does not block a write that leaves it exactly as it is; "+
			"fix or remove %s the next time %s is changed",
			f.Tenant, f.Key, f.Problem, invalidWindowsText(f.invalid), invalidWindowWhat(f.invalid), f.Key)
	}
	if f.schedule {
		return fmt.Sprintf("NOTICE: tenant=%s: %s writes %s (%s) — only its default %q is read, the rest is ignored. "+
			"It is already in the tenant file, so it does not block a write that leaves it exactly as it is; "+
			"remove %s the next time %s is changed%s",
			f.Tenant, f.Key, scheduleFields(f.Value), f.Problem,
			strings.TrimSpace(f.Value.Default), scheduleFields(f.Value), f.Key, scheduleHint(f.Key))
	}
	return fmt.Sprintf("NOTICE: tenant=%s: %s %q is not a value the exporter reads (%s) — %s. "+
		"It is already in the tenant file, so it does not block a write that leaves it as it is; "+
		"fix it the next time %s is changed",
		f.Tenant, f.Key, strings.TrimSpace(f.Value.Default), f.Problem, valueEffect(f.Key), f.Key)
}

// Ratchet moves each value finding whose key prev had with a value the
// finding counts as unchanged (ValueFinding.unchanged: for a value the
// reader ignores, one the exporter reads the same way — readAlike, the
// reader's own normalized reading, so a re-quoted or re-cased spelling
// counts as unchanged; for a schedule, the same value as written) from
// Errors to Notices. Every other entry of Errors stays where it is.
//
// prev is the value the key had before this write, in the canonical
// spelling: (sv, true) when it was set, (_, false) when it was not or nothing
// is known about it. A caller with no trustworthy base answers false for
// everything — or does not call Ratchet — and stays strict.
func (v KeyValidation) Ratchet(prev func(tenant, key string) (ScheduledValue, bool)) KeyValidation {
	if len(v.Values) == 0 || prev == nil {
		return v
	}
	out := KeyValidation{
		Errors:  append([]string(nil), v.Errors...),
		Notices: append([]string(nil), v.Notices...),

		KeyChecks: v.KeyChecks,
	}
	for _, f := range v.Values {
		pv, ok := prev(f.Tenant, f.Key)
		if !ok || !f.unchanged(pv) {
			out.Values = append(out.Values, f)
			continue
		}
		out.Errors = removeFirst(out.Errors, f.Message)
		out.Notices = append(out.Notices, f.notice())
	}
	return out
}

// Ratchet is KeyValidation.Ratchet for every tenant (a flat file declares
// several). The platform part names no tenant and carries no value finding.
func (kv KeyValidationByTenant) Ratchet(prev func(tenant, key string) (ScheduledValue, bool)) KeyValidationByTenant {
	out := KeyValidationByTenant{Platform: kv.Platform, Tenants: make(map[string]KeyValidation, len(kv.Tenants))}
	for tid, v := range kv.Tenants {
		out.Tenants[tid] = v.Ratchet(prev)
	}
	return out
}

// PriorValues is a Ratchet prev over tenants (a tenant file's `tenants:` as
// decoded), looked up in the canonical spelling: an aliased spelling stands
// for its canonical key, and when a map has both the canonical entry wins,
// as resolve does.
func PriorValues(tenants map[string]map[string]ScheduledValue) func(tenant, key string) (ScheduledValue, bool) {
	return func(tenant, key string) (ScheduledValue, bool) {
		keys := tenants[tenant]
		if sv, ok := keys[key]; ok {
			return sv, true
		}
		for k, sv := range keys {
			if canon, wasAlias := canonicalKeyFor(k); wasAlias && canon == key {
				return sv, true
			}
		}
		return ScheduledValue{}, false
	}
}

// SelfRatchet is Ratchet against the values the findings are about: every
// value finding becomes a notice. For a reader of what is on disk (GET),
// where no write is being judged and nothing a write would leave as it is
// should read as blocking.
func (kv KeyValidationByTenant) SelfRatchet() KeyValidationByTenant {
	return kv.Ratchet(func(tenant, key string) (ScheduledValue, bool) {
		for _, f := range kv.Tenants[tenant].Values {
			if f.Key == key {
				return f.Value, true
			}
		}
		return ScheduledValue{}, false
	})
}

func removeFirst(in []string, s string) []string {
	for i, e := range in {
		if e == s {
			return append(in[:i:i], in[i+1:]...)
		}
	}
	return in
}
