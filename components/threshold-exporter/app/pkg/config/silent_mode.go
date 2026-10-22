package config

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// silentModeTargets are the _silent_mode targets that silence something —
// exactly the targets resolveSilentTarget expands to entries (pinned by
// TestSilentModeTargetsMatchResolver). Turning silent mode off is spelled with
// any of disabledWords, or left empty.
var silentModeTargets = []string{"warning", "critical", "all"}

// silentModeValidValues is the "(valid: ...)" list the _silent_mode WARNs
// print: the silencing targets plus the documented off spelling
// (disabledWords[0]; the other disabled words are accepted aliases).
var silentModeValidValues = strings.Join(append(append([]string{}, silentModeTargets...), disabledWords[0]), ", ")

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
//	silentModeOf(a).canonical == silentModeOf(b).canonical
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

// silentModeReading is silentModeOf's result.
type silentModeReading struct {
	// canonical is the comparable reading (see silentModeCanonical).
	canonical silentModeCanonical
	// problem is non-empty exactly when the reader writes a WARN for the value
	// and ignores it; tenant-free, for callers that word their own message.
	problem string

	// The setting the resolver expands (Active only): the normalized target
	// for resolveSilentTarget and the expires as parsed, location kept.
	target     string
	hasExpires bool
	expires    time.Time
	// err is the parse / expires error the WARN prints.
	err error
}

// silentModeOf reads a _silent_mode value the way the exporter does: only
// sv.Default (time-window overrides are not consulted), scalar form unless
// the trimmed value contains "target:", then the structured form.
func silentModeOf(sv ScheduledValue) silentModeReading {
	val := strings.TrimSpace(sv.Default)

	// Structured format: ScheduledValue.UnmarshalYAML serializes a mapping
	// back to a YAML string, so a mapping shows up here as "target: ...".
	if strings.Contains(val, "target:") {
		parsed := silentModeStructured{}
		if err := yaml.Unmarshal([]byte(val), &parsed); err != nil {
			return silentModeReading{
				canonical: silentModeCanonical{Kind: silentModeParseError, Invalid: err.Error()},
				problem:   fmt.Sprintf("structured _silent_mode does not parse: %v", err),
				err:       err,
			}
		}
		target := strings.TrimSpace(strings.ToLower(parsed.Target))
		if isDisabled(target) || target == "" {
			return silentModeReading{canonical: silentModeCanonical{Kind: silentModeOff}}
		}

		var expires time.Time
		hasExpires := parsed.Expires != ""
		if hasExpires {
			t, err := time.Parse(time.RFC3339, parsed.Expires)
			if err != nil {
				return silentModeReading{
					canonical: silentModeCanonical{Kind: silentModeInvalidExpires, Invalid: parsed.Expires},
					problem:   fmt.Sprintf("expires %q is not RFC3339 (e.g. 2026-07-01T00:00:00Z): %v", parsed.Expires, err),
					err:       err,
				}
			}
			expires = t
		}

		sev := silentModeSeverities(target)
		if sev == "" {
			return silentModeReading{
				canonical: silentModeCanonical{Kind: silentModeUnknownTarget, Invalid: target},
				problem:   fmt.Sprintf("unknown silent mode target %q (valid: %s)", target, silentModeValidValues),
			}
		}
		return silentModeReading{
			canonical: silentModeCanonical{
				Kind: silentModeActive, Severities: sev,
				HasExpires: hasExpires, Expires: expires.UTC(), Reason: parsed.Reason,
			},
			target:     target,
			hasExpires: hasExpires,
			expires:    expires,
		}
	}

	// Scalar format (backward compatible).
	lower := strings.TrimSpace(strings.ToLower(val))
	if isDisabled(lower) || lower == "" {
		return silentModeReading{canonical: silentModeCanonical{Kind: silentModeOff}}
	}
	sev := silentModeSeverities(lower)
	if sev == "" {
		return silentModeReading{
			canonical: silentModeCanonical{Kind: silentModeUnknownScalar, Invalid: lower},
			problem:   fmt.Sprintf("unknown silent mode %q (valid: %s)", lower, silentModeValidValues),
		}
	}
	return silentModeReading{
		canonical: silentModeCanonical{Kind: silentModeActive, Severities: sev},
		target:    lower,
	}
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
	return silentModeOf(sv).problem
}

// logWarn writes the reader's WARN for r, if any. The texts are pinned by
// operational_log_test.go.
func (r silentModeReading) logWarn(logf func(format string, args ...any), tenant string) {
	switch r.canonical.Kind {
	case silentModeParseError:
		logf("WARN: failed to parse structured _silent_mode for tenant=%s: %v", tenant, r.err)
	case silentModeInvalidExpires:
		// #2000: fail-CLOSED — an unparseable expires drops the whole
		// structured _silent_mode (no silent entry → notifications NOT
		// suppressed) rather than silencing with no end. Same reasoning as
		// the maintenance branch in ResolveStateFiltersAt.
		logf("WARN: invalid expires %q in _silent_mode for tenant=%s: %v — expires is not RFC3339 (e.g. 2026-07-01T00:00:00Z), so this _silent_mode setting is IGNORED (not silenced) until it is fixed", r.canonical.Invalid, tenant, r.err)
	case silentModeUnknownTarget:
		logf("WARN: unknown silent mode target %q in structured _silent_mode for tenant=%s, ignoring (valid: %s)", r.canonical.Invalid, tenant, silentModeValidValues)
	case silentModeUnknownScalar:
		logf("WARN: unknown silent mode %q for tenant=%s, ignoring (valid: %s)", r.canonical.Invalid, tenant, silentModeValidValues)
	}
}
