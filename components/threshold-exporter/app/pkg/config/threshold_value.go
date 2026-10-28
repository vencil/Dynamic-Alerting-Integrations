package config

import (
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"time"
)

// thresholdSeverities is the closed set of severities a threshold row can
// carry: the ones the rule packs' threshold recording rules select on
// user_threshold (`severity="warning"` for `tenant:alert_threshold:<key>`,
// `severity="critical"` for `<key>_critical`). A row of any other severity is
// compared by no alert, so it is never served (#3071).
//
// ⛔ The one Go source of the set. rulepack_severity_test.go holds every
// rule-pack copy's user_threshold severity selectors to it, and
// tests/shared/threshold_value_matrix.json carries it to the Python mirrors
// (_lib_validation.THRESHOLD_SEVERITIES, which backtest_threshold's
// _COMPARED_SEVERITIES is).
var thresholdSeverities = []string{"warning", "critical"}

// ThresholdSeverities is a copy of the severities a threshold row can carry.
func ThresholdSeverities() []string {
	return append([]string(nil), thresholdSeverities...)
}

// splitThresholdSeverity reads the one grammar of a threshold value's
// severity suffix, `value[:severity]` (#3071): it splits at the LAST `:`,
// trims both halves, and lower-cases an ASCII suffix. No `:` is severity
// warning. A suffix outside thresholdSeverities — `70:high`, the empty `70:`,
// or a non-ASCII spelling (`CRİTICAL`, whose Go and Python lower-casings
// differ) — is an error: the value is unreadable as a whole, exactly like a
// value whose number does not parse.
//
// Every reader of the grammar calls it: the three threshold row resolvers
// (resolveBaseRows / resolveDeclaredRows / appendDimensionalRow), the subtree
// overlay's thresholdScalar, and custom alerts (parseCustomAlertThreshold).
// The value half is returned trimmed and otherwise as written; whether it
// is a number is the caller's question. The Python mirrors are held to it by
// tests/shared/threshold_value_matrix.json (threshold_value_parity_test.go).
func splitThresholdSeverity(raw string) (value, severity string, err error) {
	raw = strings.TrimSpace(raw)
	i := strings.LastIndex(raw, ":")
	if i < 0 {
		return raw, "warning", nil
	}
	value = strings.TrimSpace(raw[:i])
	suffix := strings.TrimSpace(raw[i+1:])
	if isASCII(suffix) {
		severity = strings.ToLower(suffix)
		for _, s := range thresholdSeverities {
			if severity == s {
				return value, severity, nil
			}
		}
	}
	return "", "", fmt.Errorf("threshold severity %q must be warning or critical", strings.ToLower(suffix))
}

// parseThresholdValue reads one threshold row's value as the row resolvers
// serve it: splitThresholdSeverity, then the value half as a threshold number
// (parseThresholdNumber). ok is false for a value unreadable either way; the
// caller falls back to the platform default or drops the row (#2377, #3071,
// #3140).
func parseThresholdValue(raw string) (v float64, severity string, ok bool) {
	valueStr, severity, err := splitThresholdSeverity(raw)
	if err != nil {
		return 0, "", false
	}
	v, ok = parseThresholdNumber(valueStr)
	if !ok {
		return 0, "", false
	}
	return v, severity, true
}

// parseThresholdNumber reads s (already trimmed) as a threshold number:
// strconv.ParseFloat, and the result must be finite (#3140). ParseFloat
// accepts `NaN`, `Inf`, `+Inf`, `-Inf` and `infinity`; none of them is a
// threshold — an alert's comparison against NaN is always false (the alert
// never fires), and against ±Inf either never or always true — so each is a
// value the exporter cannot read, exactly like `abc`.
//
// ⛔ The one Go definition of "a threshold number". Every reader of a
// threshold value calls it: parseThresholdValue (the base / declared /
// dimensional rows, the subtree overlay's thresholdScalar),
// resolveCriticalRows, custom alerts (resolveOneCustomAlert) and the root
// `defaults:` decode (readRootDefault, #3250).
// tests/shared/threshold_value_matrix.json's `float_ok` column is it, and
// carries it to the Python mirror (_lib_validation.parse_threshold_number).
func parseThresholdNumber(s string) (float64, bool) {
	v, err := strconv.ParseFloat(s, 64)
	if err != nil || !finiteThreshold(v) {
		return 0, false
	}
	return v, true
}

// finiteThreshold is parseThresholdNumber's finiteness half, for a value
// already in Defaults: a root default the decode refused is a NaN there
// (refusedDefault).
func finiteThreshold(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }

// NonFiniteThresholdWrites is, sorted, one message per threshold key of a
// tenant's map (not `_`-prefixed) whose value — its `default` or a window's
// value — reads as a number parseThresholdNumber refuses for being
// non-finite: `NaN`, `Inf`, `-inf:critical` (#3140). The exporter serves the
// platform default or drops the row for each, and da-guard refuses the tree
// (value_not_served); tenant-api refuses the write with these messages
// (its body pre-flight), so the three answer with one predicate.
func NonFiniteThresholdWrites(overrides map[string]ScheduledValue) []string {
	var out []string
	for key, sv := range overrides {
		if baseRowsSkipKey(key) {
			continue
		}
		values := []string{sv.Default}
		for _, o := range sv.Overrides {
			values = append(values, o.Value)
		}
		for _, raw := range values {
			if nonFiniteThresholdText(raw) {
				out = append(out, fmt.Sprintf("%s: %q is not a finite number — an alert compared against "+
					"NaN or ±Inf never fires or always fires, so the exporter does not serve it as a threshold; "+
					"write a finite number (optionally with `:warning` or `:critical`) or `disable`", key, raw))
				break
			}
		}
	}
	sort.Strings(out)
	return out
}

// nonFiniteThresholdText reports whether raw's number half (the shared
// grammar; the whole value of a `_critical` key reads the same, since a
// non-finite text carries no `:` of its own) is a number ParseFloat reads but
// parseThresholdNumber refuses — non-finite.
func nonFiniteThresholdText(raw string) bool {
	value, _, err := splitThresholdSeverity(raw)
	if err != nil {
		return false
	}
	if _, ok := parseThresholdNumber(value); ok {
		return false
	}
	_, perr := strconv.ParseFloat(value, 64)
	return perr == nil
}

// Why the row resolvers refuse a threshold key's value (RefusedValueAt): the
// closed set da-guard served-values writes as keys[].refused and on a schedule
// segment with no row (#3140), so a reader tells a value the exporter refused
// from a key the tenant switched off without reading the value itself.
const (
	// RefusedNonFinite: a number strconv.ParseFloat reads but
	// parseThresholdNumber refuses — NaN or ±Inf.
	RefusedNonFinite = "non_finite"
	// RefusedUnparsed: any other value the row resolvers cannot read (`abc`,
	// `70:high`, `disable:critical`, `1e999`).
	RefusedUnparsed = "unparsed"
)

// RefusedValueAt is why the row resolvers refuse the value threshold key key
// (canonical spelling) resolves to at now — RefusedNonFinite or
// RefusedUnparsed — or "" when they read it: a number, or a disable
// (thresholdRowDisabled). Read the way the resolver of key reads it: the
// whole value of a `<key>_critical` (resolveCriticalRows), the shared
// grammar for any other key. Whether the resolver met the value at all (an
// undeclared key gets no row whatever it holds) is not decided here: the
// caller asks ValuesNotServed for that.
func RefusedValueAt(key string, sv ScheduledValue, now time.Time) string {
	raw := sv.resolveValue(now, nil)
	if off, _ := thresholdRowDisabled(raw); off {
		return ""
	}
	if _, critical := criticalRowBase(key); critical {
		v := strings.TrimSpace(raw)
		if _, ok := parseThresholdNumber(v); ok {
			return ""
		}
		if _, err := strconv.ParseFloat(v, 64); err == nil {
			return RefusedNonFinite
		}
		return RefusedUnparsed
	}
	if _, _, ok := parseThresholdValue(raw); ok {
		return ""
	}
	if nonFiniteThresholdText(raw) {
		return RefusedNonFinite
	}
	return RefusedUnparsed
}

// thresholdRowDisabled reads whether a threshold row's value switches its key
// off (#3141): the value read by splitThresholdSeverity's grammar is a
// disable word (isDisabled, after Go's strings.ToLower) and the value carries
// no `:<severity>` suffix. suffixed reports the other case of a disable word:
// `disable:critical`, `off:warning`. That is NOT a disable of a threshold row
// — `disable` switches the whole key off, and a key's critical tier alone is
// switched off by `<key>_critical: disable` — so the row resolvers read it as
// a value they cannot read (value_unparsed / value_unparsed_dropped) and say
// which two spellings were meant (DisableWithSeverityHint).
//
// ⚠️ A custom alert reads `disable:<severity>` as its opt-out (#3042,
// custom_alert_disabled in the matrix): its threshold has no `_critical`
// sibling key to name instead.
func thresholdRowDisabled(raw string) (off, suffixed bool) {
	value, _, err := splitThresholdSeverity(raw)
	if err != nil || !isDisabled(strings.ToLower(value)) {
		return false, false
	}
	if strings.Contains(strings.TrimSpace(raw), ":") {
		return false, true
	}
	return true, false
}

// DisableWithSeverityHint is the fix a `disable:<severity>` on threshold key
// key is told (thresholdRowDisabled's suffixed case): the one text, in the
// resolver's WARN (suffixedHint) and in da-guard's value_not_served message
// (valueNotServedMessage).
func DisableWithSeverityHint(key string) string {
	base := "<key>"
	if !strings.HasSuffix(key, criticalSuffix) && !strings.Contains(key, "{") {
		base = key
	}
	return fmt.Sprintf("`disable:<severity>` (e.g. `disable:critical`) does not switch a threshold row off: write "+
		"`disable` to switch the whole key off, or `%s_critical: disable` to switch off only the critical tier", base)
}

// suffixedHint is "; " + DisableWithSeverityHint(key) when suffixed, else "".
func suffixedHint(suffixed bool, key string) string {
	if !suffixed {
		return ""
	}
	return "; " + DisableWithSeverityHint(key)
}

func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			return false
		}
	}
	return true
}
