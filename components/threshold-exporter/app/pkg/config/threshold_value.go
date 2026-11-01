package config

import (
	"errors"
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
// is a number is the caller's question. Python has no copy of it: a Python
// reader asks da-guard threshold-values (ReadThresholdText, #3267), held to
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
// tests/shared/threshold_value_matrix.json's `float_ok` column is it; a
// Python reader asks da-guard threshold-values (ReadThresholdText, #3267).
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
//
// ⚠️ Strict: tenant-api refuses these whether or not the file already held
// them (#3140). Every OTHER value the resolvers refuse is
// RefusedThresholdWrites', which tenant-api ratchets (#3267, ADR-038
// decision 12).
func NonFiniteThresholdWrites(overrides map[string]ScheduledValue) []string {
	var out []string
	for key, sv := range overrides {
		if baseRowsSkipKey(key) {
			continue
		}
		for _, raw := range scheduledTexts(sv) {
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

// scheduledTexts is sv's values as written: its `default`, then each
// window's value.
func scheduledTexts(sv ScheduledValue) []string {
	values := []string{sv.Default}
	for _, o := range sv.Overrides {
		values = append(values, o.Value)
	}
	return values
}

// ThresholdRefusal is one threshold key whose value — its `default` or a
// window's — the row resolver of the key refuses, and NonFiniteThresholdWrites
// does not (#3267): `abc`, `70:high`, `disable:critical`, a number ParseFloat
// reads only out of range (`1e999`, `0x1p1024`), "" and, on a
// `<key>_critical`, any severity suffix. Judged by refusedThresholdText, the
// classifier RefusedValueAt (da-guard's value_not_served) is.
type ThresholdRefusal struct {
	// Key is the key as written; Value the first refused value as written.
	Key, Value string
	// Message is the refusal, "<key>: <why>".
	Message string
}

// Notice is the advisory a write gate puts in r's place when tenant's file
// already holds the value and the write leaves it as the exporter reads it
// (ThresholdReadAlike; ADR-038 decision 12).
func (r ThresholdRefusal) Notice(tenant string) string {
	return fmt.Sprintf("NOTICE: tenant=%s: %s %q is not a threshold the exporter reads, so it does not serve it. "+
		"It is already in the tenant file, so it does not block a write that leaves it as the exporter reads it; "+
		"fix it the next time %s is changed", tenant, r.Key, r.Value, r.Key)
}

// RefusedThresholdWrites is, sorted by key, the ThresholdRefusal of every
// threshold key of a tenant's map (not `_`-prefixed) that has one. A value
// NonFiniteThresholdWrites reports is not one: the two never report the
// same value, and a value either reports is exactly a value RefusedValueAt
// refuses (TestRefusedThresholdWrites_IsRefusedValueAt).
func RefusedThresholdWrites(overrides map[string]ScheduledValue) []ThresholdRefusal {
	var out []ThresholdRefusal
	for key, sv := range overrides {
		if baseRowsSkipKey(key) {
			continue
		}
		canon, _ := CanonicalKeyFor(key)
		for _, raw := range scheduledTexts(sv) {
			if nonFiniteThresholdText(raw) || refusedThresholdText(canon, raw) == "" {
				continue
			}
			out = append(out, ThresholdRefusal{Key: key, Value: raw, Message: refusedThresholdWriteMessage(key, canon, raw)})
			break
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	return out
}

// refusedThresholdWriteMessage is the ThresholdRefusal message for raw,
// written on key (canon its canonical spelling).
func refusedThresholdWriteMessage(key, canon, raw string) string {
	if _, critical := criticalRowBase(canon); critical {
		return fmt.Sprintf("%s: %q is not a threshold the exporter reads — a `_critical` key's value is "+
			"the critical threshold itself: write a finite number with no `:<severity>` suffix, or `disable`", key, raw)
	}
	_, suffixed := thresholdRowDisabled(raw)
	return fmt.Sprintf("%s: %q is not a threshold the exporter reads, so it does not serve it — write a "+
		"finite number (optionally with `:warning` or `:critical`) or `disable`%s", key, raw, suffixedHint(suffixed, key))
}

// ThresholdReadAlike reports whether the row resolver of threshold key key
// (canonical spelling) reads a and b the same way (#3267, ADR-038 decision
// 12): each value — `default` and every window's, in order — by its
// thresholdValueReading, each window by its parsed span, and the same
// expiry. Quoting, surrounding space, a severity suffix's case, `1_000`
// against `1000`, or `abc:warning` against `abc` are read alike; `abc`
// against `xyz`, or a moved window, are not.
func ThresholdReadAlike(key string, a, b ScheduledValue) bool {
	if len(a.Overrides) != len(b.Overrides) || thresholdValueReading(key, a.Default) != thresholdValueReading(key, b.Default) {
		return false
	}
	for i := range a.Overrides {
		if windowReading(a.Overrides[i].Window) != windowReading(b.Overrides[i].Window) ||
			thresholdValueReading(key, a.Overrides[i].Value) != thresholdValueReading(key, b.Overrides[i].Value) {
			return false
		}
	}
	if (a.Expiry == nil) != (b.Expiry == nil) {
		return false
	}
	return a.Expiry == nil || *a.Expiry == *b.Expiry
}

// thresholdValueReading is raw as the row resolver of key reads it, as a
// comparable text: a disable; for a number, the number and the severity;
// for a value the resolver refuses, the text with the grammar's own
// normalization (trimmed halves, an ASCII-lower-cased suffix).
func thresholdValueReading(key, raw string) string {
	if off, _ := thresholdRowDisabled(raw); off {
		return "disable"
	}
	if _, critical := criticalRowBase(key); critical {
		v := strings.TrimSpace(raw)
		if n, ok := parseThresholdNumber(v); ok {
			return "n:" + strconv.FormatFloat(n, 'g', -1, 64)
		}
		return "t:" + v
	}
	if n, sev, ok := parseThresholdValue(raw); ok {
		return "n:" + strconv.FormatFloat(n, 'g', -1, 64) + ":" + sev
	}
	t := strings.TrimSpace(raw)
	if i := strings.LastIndex(t, ":"); i >= 0 {
		return "t:" + strings.TrimSpace(t[:i]) + ":" + strings.ToLower(strings.TrimSpace(t[i+1:]))
	}
	return "t:" + t + ":warning"
}

// windowReading is a window as the resolver reads it: its span, or its text
// when it does not parse (it then matches no minute).
func windowReading(w string) string {
	if start, end, ok := parseTimeWindow(w, nil); ok {
		return fmt.Sprintf("%d-%d", start, end)
	}
	return "x:" + strings.TrimSpace(w)
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
	return refusedThresholdText(key, sv.resolveValue(now, nil))
}

// refusedThresholdText is RefusedValueAt on one value as written, raw, with
// no schedule to resolve: the one classifier of a threshold key's value, which
// RefusedValueAt (da-guard) and RefusedThresholdWrites (tenant-api's
// ratcheted write check) both are (#3267).
func refusedThresholdText(key, raw string) string {
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

// ThresholdText is one threshold value's text as the exporter reads it, for
// a caller that holds the text alone — no tenant, no tree: da-guard
// threshold-values (#3267), which the unit-sanity lint reads every value it
// judges through, so that lint has no reading of its own.
//
// ⛔ NO READING OF ITS OWN. Every field is one of this file's readers:
// Severity is splitThresholdSeverity's, Number parseThresholdValue's,
// Refused RefusedValueAt's; NonFinite is strconv.ParseFloat's result on the
// value half — the call parseThresholdNumber makes — when that result is
// what parseThresholdNumber refuses for being non-finite.
type ThresholdText struct {
	// Severity is the `:severity` suffix as splitThresholdSeverity reads it
	// ("warning" with no suffix), or "" when it refuses the suffix (`70:high`,
	// `70:`): then neither Number nor NonFinite is read.
	Severity string
	// Number is the value parseThresholdValue reads (the shared grammar,
	// finite only); HasNumber is false when it reads none.
	Number    float64
	HasNumber bool
	// NonFinite is NaN or ±Inf when strconv.ParseFloat reads the value half
	// as one — `NaN`, `-Inf`, `infinity`, and an overflow (`1e999`,
	// `0x1p1024`: ErrRange, ±Inf); HasNonFinite is false otherwise. The
	// exporter refuses every such value; a caller that judges a non-finite
	// value as written (the unit-sanity lint, #3140 ruling D) reads it here.
	NonFinite    float64
	HasNonFinite bool
	// Refused is RefusedValueAt for key, the text being the whole value:
	// "", RefusedNonFinite or RefusedUnparsed. It is the one field that
	// depends on key: a `<key>_critical` value is read whole
	// (resolveCriticalRows), so `70:critical` is refused there although
	// Number, the shared grammar's reading, is 70.
	Refused string
}

// ReadThresholdText is raw (a threshold value's text, as written for
// threshold key key in canonical spelling) as ThresholdText reads it.
func ReadThresholdText(key, raw string) ThresholdText {
	// A value with no schedule: RefusedValueAt resolves it at any instant.
	out := ThresholdText{Refused: RefusedValueAt(key, ScheduledValue{Default: raw}, time.Time{})}
	value, severity, err := splitThresholdSeverity(raw)
	if err != nil {
		return out
	}
	out.Severity = severity
	if v, _, ok := parseThresholdValue(raw); ok {
		out.Number, out.HasNumber = v, true
		return out
	}
	v, perr := strconv.ParseFloat(value, 64)
	if (perr == nil || errors.Is(perr, strconv.ErrRange)) && !finiteThreshold(v) {
		out.NonFinite, out.HasNonFinite = v, true
	}
	return out
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
