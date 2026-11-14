package config

import (
	"sort"
	"strings"
	"time"
)

// silentModeTargets are the _silent_mode targets that silence something —
// exactly the targets resolveSilentTarget expands to entries (pinned by
// TestSilentModeTargetsMatchResolver). Turning silent mode off is spelled with
// any word isDisabled accepts, or left empty.
var silentModeTargets = []string{"warning", "critical", "all"}

// silentModeValidValues is the "(valid: ...)" list the _silent_mode WARNs
// print: the silencing targets plus the documented off spelling (the other
// disabled words are accepted aliases).
var silentModeValidValues = strings.Join(append(append([]string{}, silentModeTargets...), disabledSpelling), ", ")

// silentModeKind is what the exporter's reader makes of a _silent_mode value.
type silentModeKind uint8

const (
	// silentModeOff: nothing silenced, no WARN — empty, a disabled word, or a
	// structured value whose target is empty or a disabled word.
	silentModeOff silentModeKind = iota
	// silentModeActive: one entry per severity in Severities.
	silentModeActive
	// silentModeUnknownScalar: a scalar the reader does not know; WARN, ignored.
	silentModeUnknownScalar
	// silentModeParseError: a structured value that does not parse; WARN, ignored.
	silentModeParseError
	// silentModeInvalidExpires: a structured value whose expires is not
	// RFC3339; WARN, ignored (#2000 fail-closed).
	silentModeInvalidExpires
	// silentModeUnknownTarget: a structured value whose target the reader does
	// not know; WARN, ignored.
	silentModeUnknownTarget
)

// silentModeCanonical is the normalized reading of a _silent_mode value.
//
// Contract: for two values a and b,
//
//	silentModeOf(a).canonical() == silentModeOf(b).canonical()
//
// holds if and only if the reader (resolveSilentModesAt) treats them the same:
// the same silence entries (same severities, the same expires instant, the
// same reason) and the same WARN text, at every `now`. So it is the thing to
// compare when asking "did this write change _silent_mode?" — quoting, key
// order, comments, case and scalar-vs-mapping spelling all fall away, and
// only what the reader acts on remains.
//
// Every field is a plain comparable value (time.Time is kept in UTC, never
// with a monotonic reading), so `==` and reflect.DeepEqual agree. Fields that
// do not affect the reading for a Kind are left zero.
type silentModeCanonical struct {
	Kind silentModeKind
	// Severities (Active only): the severities silenced, lower-case, sorted,
	// comma-joined — "all" reads as "critical,warning".
	Severities string
	// HasExpires / Expires (Active only): whether expires was set, and the
	// parsed instant in UTC. The flag keeps "no expires" apart from an
	// expires of the zero instant, which the reader reads as already expired.
	HasExpires bool
	Expires    time.Time
	// Reason (Active only): the structured reason as written.
	Reason string
	// Invalid (the problem Kinds only): what the WARN names — the trimmed,
	// lower-cased scalar (UnknownScalar) or target (UnknownTarget), the
	// expires string as written (InvalidExpires), or the parser's error text
	// (ParseError).
	Invalid string
}

// silentModeReading is silentModeOf's result: what the reader decided, and
// only the inputs it acts on. ResolveSilentModes reads every tenant on every
// scrape, so the forms only the write gate needs — the comparable canonical()
// and the problem() sentence — are derived from it on demand, never built on
// the read path (#3262).
type silentModeReading struct {
	kind       silentModeKind
	hasExpires bool // Active: expires was set
	// target: the normalized target (Active — what resolveSilentTarget
	// expands), or the unknown word the WARN names (UnknownScalar,
	// UnknownTarget), or the expires as written (InvalidExpires).
	target  string
	expires time.Time // Active: the expires as parsed, location kept
	reason  string    // Active: the structured reason as written
	err     error     // ParseError / InvalidExpires: the error the WARN prints
}

// silentModeOf reads a _silent_mode value the way the exporter does: only
// sv.Default (time-window overrides are not consulted), the structured form
// for a mapping written with a `target` key, the scalar form otherwise.
//
// #3638: which form is the structure's (structureOf), never the text's — a
// scalar `"target: critical"` is the unknown scalar it was written as, not
// the mapping its text reads like. A mapping or sequence without a top-level
// `target` key is its text read as an unknown scalar (WARN, refused on
// write) — before #3638 one whose text held "target:" somewhere
// (`{reason: "target: x"}`, `[{target: all}]`) read as off or as a parse
// error instead.
func silentModeOf(sv ScheduledValue) silentModeReading {
	node, structured, err := structureOf(sv)
	if structured && err != nil {
		return silentModeReading{kind: silentModeParseError, err: err}
	}
	if mappingHasKey(node, "target") {
		parsed := silentModeStructured{}
		if err := node.Decode(&parsed); err != nil {
			return silentModeReading{kind: silentModeParseError, err: err}
		}
		target := strings.TrimSpace(strings.ToLower(parsed.Target))
		if isDisabled(target) || target == "" {
			return silentModeReading{kind: silentModeOff}
		}

		var expires time.Time
		hasExpires := parsed.Expires != ""
		if hasExpires {
			t, err := time.Parse(time.RFC3339, parsed.Expires)
			if err != nil {
				return silentModeReading{kind: silentModeInvalidExpires, target: parsed.Expires, err: err}
			}
			expires = t
		}

		if !isSilentModeTarget(target) {
			return silentModeReading{kind: silentModeUnknownTarget, target: target}
		}
		return silentModeReading{
			kind: silentModeActive, target: target,
			hasExpires: hasExpires, expires: expires, reason: parsed.Reason,
		}
	}

	// Scalar format (backward compatible).
	lower := strings.TrimSpace(strings.ToLower(sv.Default))
	if isDisabled(lower) || lower == "" {
		return silentModeReading{kind: silentModeOff}
	}
	if !isSilentModeTarget(lower) {
		return silentModeReading{kind: silentModeUnknownScalar, target: lower}
	}
	return silentModeReading{kind: silentModeActive, target: lower}
}

// isSilentModeTarget reports whether target is one of silentModeTargets.
func isSilentModeTarget(target string) bool {
	return silentModeSeverities(target) != ""
}

// canonical is r's comparable form (see silentModeCanonical). Write gate
// only; the reader never builds it.
func (r *silentModeReading) canonical() silentModeCanonical {
	switch r.kind {
	case silentModeActive:
		c := silentModeCanonical{Kind: r.kind, Severities: silentModeSeverities(r.target), Reason: r.reason}
		if r.hasExpires {
			c.HasExpires, c.Expires = true, r.expires.UTC()
		}
		return c
	case silentModeParseError:
		return silentModeCanonical{Kind: r.kind, Invalid: r.err.Error()}
	case silentModeOff:
		return silentModeCanonical{Kind: r.kind}
	default: // UnknownScalar, UnknownTarget, InvalidExpires
		return silentModeCanonical{Kind: r.kind, Invalid: r.target}
	}
}

// problem is non-empty exactly when the reader writes a WARN for r and
// ignores it; tenant-free, for callers that word their own message. Write
// gate and key verdict only; the reader prints logWarn's text instead.
// Constants only (#3703): the sentence never carries the value or a parser's
// error, which quotes it — the caller names the file, the tenant and the key.
func (r *silentModeReading) problem() string {
	switch r.kind {
	case silentModeParseError:
		return "the structured _silent_mode does not parse"
	case silentModeInvalidExpires:
		return expiresNotRFC3339Problem
	case silentModeUnknownTarget:
		return "unknown silent mode target (valid: " + silentModeValidValues + ")"
	case silentModeUnknownScalar:
		return "unknown silent mode (valid: " + silentModeValidValues + ")"
	}
	return ""
}

// silentModeSeverities is the sorted, comma-joined set of severities
// resolveSilentTarget expands target to; "" when it knows no such target.
func silentModeSeverities(target string) string {
	for i, t := range silentModeTargets {
		if t == target {
			return silentModeSeveritiesOfTarget[i]
		}
	}
	return ""
}

// silentModeSeveritiesOfTarget[i] is silentModeSeverities(silentModeTargets[i]),
// built once from resolveSilentTarget itself: ResolveSilentModes reads every
// tenant on every scrape, so the read is a scan of three strings, not an
// expand-and-sort. A target the resolver expands but silentModeTargets does
// not list would read as unknown here; TestSilentModeTargetsMatchResolver
// keeps the two lists equal.
var silentModeSeveritiesOfTarget = func() []string {
	out := make([]string, len(silentModeTargets))
	for i, target := range silentModeTargets {
		entries := resolveSilentTarget("", target, time.Time{}, "", false)
		sev := make([]string, 0, len(entries))
		for _, e := range entries {
			sev = append(sev, e.TargetSeverity)
		}
		sort.Strings(sev)
		out[i] = strings.Join(sev, ",")
	}
	return out
}()

// silentModeProblem is the tenant-free reason the exporter ignores a
// _silent_mode value with a WARN, or "" when it reads the value without one.
func silentModeProblem(sv ScheduledValue) string {
	r := silentModeOf(sv)
	return r.problem()
}

// logWarn writes the reader's WARN for r, if any. The texts are pinned by
// operational_log_test.go.
func (r *silentModeReading) logWarn(logf func(format string, args ...any), tenant string) {
	switch r.kind {
	case silentModeParseError:
		logf("WARN: failed to parse structured _silent_mode for tenant=%s: %v", tenant, r.err)
	case silentModeInvalidExpires:
		// #2000: fail-CLOSED — an unparseable expires drops the whole
		// structured _silent_mode (no silent entry → notifications NOT
		// suppressed) rather than silencing with no end. Same reasoning as
		// the maintenance branch in ResolveStateFiltersAt.
		logf("WARN: invalid expires %q in _silent_mode for tenant=%s: %v — expires is not RFC3339 (e.g. 2026-07-01T00:00:00Z), so this _silent_mode setting is IGNORED (not silenced) until it is fixed", r.target, tenant, r.err)
	case silentModeUnknownTarget:
		logf("WARN: unknown silent mode target %q in structured _silent_mode for tenant=%s, ignoring (valid: %s)", r.target, tenant, silentModeValidValues)
	case silentModeUnknownScalar:
		logf("WARN: unknown silent mode %q for tenant=%s, ignoring (valid: %s)", r.target, tenant, silentModeValidValues)
	}
}
