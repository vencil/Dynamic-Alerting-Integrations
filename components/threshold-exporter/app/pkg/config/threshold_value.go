package config

import (
	"errors"
	"fmt"
	"math"
	"regexp"
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

// thresholdNumberRe is the one grammar of a threshold number (#3457): the
// recipe contract's patterns.thresholdNumber, which the portal, the Python
// tools and the JSON schema's thresholdScalar read from the same file
// (scripts/tools/dx/gen_recipe_contract.py generates their copies). Decimal
// only: an optional sign, ASCII digits with at most one `.` — at least one
// digit and at most 20 in all, every digit counted, a leading or trailing
// zero too — and an optional `e` / `E` exponent of any number of digits.
//
// So `1_000`, `0x1p4`, `0x1F`, `Inf` and `NaN` are not threshold numbers,
// whatever strconv.ParseFloat makes of them. The 20 digits are the bound
// within which every reader is known to round alike: ECMAScript may round a
// longer mantissa to 20 significant digits before converting, and
// strconv.ParseFloat reads a mantissa of some 800 digits or more as another
// number with no error (#3827: "1", 800 zeros, "e-800" is 0.1). The exponent
// needs no bound: it only scales, and a value past float64 is ±Inf, which
// parseThresholdNumber refuses.
var thresholdNumberRe = regexp.MustCompile(contract.Patterns.ThresholdNumber)

// parseThresholdNumber reads s (already trimmed) as a threshold number: s is
// in the grammar (thresholdNumberRe), and the number strconv.ParseFloat reads
// it as is finite (#3140) — `1e999` is in the grammar and is +Inf. An alert's
// comparison against NaN is always false (the alert never fires), and against
// ±Inf either never or always true, so a non-finite value is one the exporter
// cannot read, exactly like `abc`.
//
// ⛔ The one Go definition of "a threshold number". Every reader of a
// threshold value calls it: parseThresholdValue (the base / declared /
// dimensional rows, the subtree overlay's thresholdScalar),
// resolveCriticalRows, custom alerts (resolveOneCustomAlert) and the root
// `defaults:` decode (readRootDefault, #3250).
// tests/shared/threshold_value_matrix.json's `float_ok` column is it; a
// Python reader asks da-guard threshold-values (ReadThresholdText, #3267).
func parseThresholdNumber(s string) (float64, bool) {
	if !thresholdNumberRe.MatchString(s) {
		return 0, false
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil || !finiteThreshold(v) {
		return 0, false
	}
	return v, true
}

// thresholdNumberText is v, a finite number, as a text parseThresholdNumber
// reads back as v: the shortest `d.ddde±dd` form, whose mantissa is never
// more than 17 digits. The 'g' and 'f' forms are not in the grammar for every
// number — 0.00012345678901234567 is 21 digits written out, and 1e-300 is
// 300.
func thresholdNumberText(v float64) string {
	return strconv.FormatFloat(v, 'e', -1, 64)
}

// nonFiniteNumberText reports whether s is a text strconv.ParseFloat reads,
// with no error, as NaN or ±Inf: `NaN`, `Inf`, `+Inf`, `-Inf`, `infinity`, in
// any letter case. That is what RefusedNonFinite names. A text ParseFloat
// reads as a finite number and the grammar refuses (`1_000`, `0x1p4`, a
// mantissa of more than 20 digits), and one it reads only out of range
// (`1e999`), are RefusedUnparsed.
func nonFiniteNumberText(s string) bool {
	v, err := strconv.ParseFloat(s, 64)
	return err == nil && !finiteThreshold(v)
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
// them (#3140). Every OTHER value the resolvers refuse is a threshold value
// finding of ValidateTenantKeys (thresholdRefusalIn), which tenant-api
// ratchets (#3267, ADR-038 decision 12).
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

// thresholdRefusal is one threshold key whose value — its `default` or a
// window's — the row resolvers of the tree refuse, and
// NonFiniteThresholdWrites does not (#3267): `abc`, `70:high`,
// `disable:critical`, a text outside the grammar of a threshold number
// (`1_000`, `0x1p4`, #3457), a number out of range (`1e999`), "" and, on a `<key>_critical` that resolveCriticalRows reads,
// any severity suffix. Judged by refusedThresholdTextIn, the classifier
// RefusedValueAt (da-guard's value_not_served) is.
type thresholdRefusal struct {
	// Key is the key; Value the first refused value as written.
	Key, Value string
	// Message is the refusal, "<key>: <why>".
	Message string
}

// notice is the advisory a write gate puts in r's place when tenant's file
// already holds the value and the write leaves it as the exporter reads it
// (readAlike; ADR-038 decision 12).
func (r thresholdRefusal) notice(tenant string) string {
	return fmt.Sprintf("NOTICE: tenant=%s: %s %q is not a threshold the exporter reads, so it does not serve it. "+
		"It is already in the tenant file, so it does not block a write that leaves it as the exporter reads it; "+
		"fix it the next time %s is changed", tenant, r.Key, r.Value, r.Key)
}

// thresholdRefusalIn is the thresholdRefusal of sv, the value of threshold
// key key (canonical spelling), in a tree whose root `defaults:` (canonical)
// is defaults — or false. A value NonFiniteThresholdWrites reports is not
// one: the two never report the same value. One value at a time, no
// allocation for a value the resolvers read: it runs for every threshold key
// of every tenant ValidateTenantKeys judges.
//
// #3683: only an entry /metrics may serve is judged. A value under a window
// parseTimeWindow refuses is the write gate's window_invalid finding
// (newInvalidWindowFinding), and one in an entry that serves no minute of
// the day (ScheduledValue.DeadEntries) its dead_entry finding: each entry is
// reported once, by the defect that keeps it from being served — the
// exporter never reads the value, so refusing it as well would name a second
// problem that is not the one to fix. Asked only for a refused value: the
// entries the resolvers read cost nothing more.
func thresholdRefusalIn(defaults map[string]float64, key string, sv ScheduledValue) (thresholdRefusal, bool) {
	// The dead entries are read once, and only once a value is refused.
	var des []DeadEntry
	read := false
	dead := func(entry string) bool {
		if !read {
			des, read = sv.DeadEntries(), true
		}
		return entryIn(des, entry)
	}
	if r, ok := thresholdValueRefusal(defaults, key, sv.Default); ok && !dead("default") {
		return r, true
	}
	for i, o := range sv.Overrides {
		r, ok := thresholdValueRefusal(defaults, key, o.Value)
		if !ok {
			continue
		}
		if _, _, valid := parseTimeWindow(o.Window, nil); !valid {
			continue
		}
		if dead(overrideEntry(i)) {
			continue
		}
		return r, true
	}
	return thresholdRefusal{}, false
}

// servedEntryValues is the values of sv's entries /metrics may serve, as
// written: its default unless the windows cover the whole day, then each
// window's whose `window:` parses and that some minute is not shadowed from
// (DeadEntries) — the entries thresholdRefusalIn judges.
func servedEntryValues(sv ScheduledValue) []string {
	des := sv.DeadEntries()
	out := make([]string, 0, 1+len(sv.Overrides))
	if !entryIn(des, "default") {
		out = append(out, sv.Default)
	}
	for i, o := range sv.Overrides {
		if _, _, ok := parseTimeWindow(o.Window, nil); !ok || entryIn(des, overrideEntry(i)) {
			continue
		}
		out = append(out, o.Value)
	}
	return out
}

// entryIn reports whether entry ("default" or overrideEntry(i)) is one of
// des (a schedule's DeadEntries, read once by the caller).
func entryIn(des []DeadEntry, entry string) bool {
	for _, d := range des {
		if d.Entry == entry {
			return true
		}
	}
	return false
}

func thresholdValueRefusal(defaults map[string]float64, key, raw string) (thresholdRefusal, bool) {
	refused, whole := refusedThresholdTextIn(defaults, key, raw)
	if refused == "" || nonFiniteThresholdText(raw) {
		return thresholdRefusal{}, false
	}
	return thresholdRefusal{Key: key, Value: raw, Message: refusedThresholdWriteMessage(key, raw, whole)}, true
}

// refusedThresholdWrites is, sorted by key, the thresholdRefusal of every
// threshold key of overrides (not `_`-prefixed) that has one, in a tree whose
// root `defaults:` is defaults (nil: none). For tests: ValidateTenantKeys
// asks thresholdRefusalIn key by key.
func refusedThresholdWrites(defaults map[string]float64, overrides map[string]ScheduledValue) []thresholdRefusal {
	var out []thresholdRefusal
	for key, sv := range overrides {
		if baseRowsSkipKey(key) {
			continue
		}
		canon, _ := CanonicalKeyFor(key)
		if r, ok := thresholdRefusalIn(defaults, canon, sv); ok {
			out = append(out, r)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	return out
}

// refusedThresholdWriteMessage is the thresholdRefusal message for raw on
// key; whole: resolveCriticalRows' whole-value reading refused it.
func refusedThresholdWriteMessage(key, raw string, whole bool) string {
	if whole {
		return fmt.Sprintf("%s: %q is not a threshold the exporter reads — a `_critical` key's value is "+
			"the critical threshold itself: write a finite number with no `:<severity>` suffix, or `disable`", key, raw)
	}
	_, suffixed := thresholdRowDisabled(raw)
	return fmt.Sprintf("%s: %q is not a threshold the exporter reads, so it does not serve it — write a "+
		"finite number (optionally with `:warning` or `:critical`) or `disable`%s", key, raw, suffixedHint(suffixed, key))
}

// thresholdReadAlike reports whether the row resolver of threshold key key
// (canonical spelling) reads a and b the same way (#3267, ADR-038 decision
// 12): at every minute of the UTC day the value the schedule resolves to —
// valueAtMinute, the function /metrics resolves it with — has the same
// thresholdValueReading, and the expiry lapses at the same instant
// (expiryReading). So quoting, surrounding space, a severity suffix's case,
// `1e3` against `1000`, or `abc:warning` against `abc` are read alike; so
// are (#3508) windows that do not overlap written in another order, a
// `reason:` added or changed, and `expires` written with another offset for
// the same instant. `abc` against `xyz`, a moved window, or overlapping
// windows reordered so another one wins a minute are not.
//
// Every value written in an entry /metrics may serve must also be one the
// other side writes (as a set of readings, so a window repeating the default
// stays alike): the minute-by-minute comparison sees only the entry that
// wins each minute. An entry that serves no minute — a window that does not
// parse, one wholly shadowed by an earlier window, a default the windows
// cover — is not part of the reading (#3683): its value is not judged as a
// threshold (thresholdRefusalIn), and the entry itself is the write gate's
// window_invalid or dead_entry finding, ratcheted as written. So removing
// such an entry leaves the rest read alike. readAlike, the one "read the
// same way" of a value finding, dispatches a threshold key to it.
func thresholdReadAlike(key string, a, b ScheduledValue) bool {
	if expiryReading(a.Expiry) != expiryReading(b.Expiry) {
		return false
	}
	if len(a.Overrides) == 0 && len(b.Overrides) == 0 {
		return thresholdValueReading(key, a.Default) == thresholdValueReading(key, b.Default)
	}
	if !sameValueReadings(key, a, b) {
		return false
	}
	for m := 0; m < 24*60; m++ {
		if thresholdValueReading(key, a.valueAtMinute(m, nil)) != thresholdValueReading(key, b.valueAtMinute(m, nil)) {
			return false
		}
	}
	return true
}

// sameValueReadings reports whether a and b write the same set of values —
// default and every window's, by thresholdValueReading — wherever they sit,
// leaving out each entry that serves no minute (servedEntryValues).
func sameValueReadings(key string, a, b ScheduledValue) bool {
	set := func(sv ScheduledValue) map[string]bool {
		out := map[string]bool{}
		for _, v := range servedEntryValues(sv) {
			out[thresholdValueReading(key, v)] = true
		}
		return out
	}
	sa, sb := set(a), set(b)
	if len(sa) != len(sb) {
		return false
	}
	for r := range sa {
		if !sb[r] {
			return false
		}
	}
	return true
}

// expiryReading is when e makes a threshold lapse, as isThresholdExpired
// reads it: the parsed instant in UTC, or "never" — for no expiry, an empty
// `expires`, and one that does not parse (that reading is fail-open: the
// value never lapses). `reason:` is not read.
func expiryReading(e *ExpiryMeta) string {
	if e == nil || e.Expires == "" {
		return "never"
	}
	t, err := time.Parse(time.RFC3339, e.Expires)
	if err != nil {
		return "never"
	}
	return t.UTC().Format(time.RFC3339Nano)
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

// nonFiniteThresholdText reports whether raw's number half (the shared
// grammar; the whole value of a `_critical` key reads the same, since a
// non-finite text carries no `:` of its own) is a non-finite number as
// written (nonFiniteNumberText).
func nonFiniteThresholdText(raw string) bool {
	value, _, err := splitThresholdSeverity(raw)
	if err != nil {
		return false
	}
	return nonFiniteNumberText(value)
}

// Why the row resolvers refuse a threshold key's value (RefusedValueAt): the
// closed set da-guard served-values writes as keys[].refused and on a schedule
// segment with no row (#3140), so a reader tells a value the exporter refused
// from a key the tenant switched off without reading the value itself.
const (
	// RefusedNonFinite: a text that names NaN or ±Inf (nonFiniteNumberText).
	RefusedNonFinite = "non_finite"
	// RefusedUnparsed: any other value the row resolvers cannot read (`abc`,
	// `70:high`, `disable:critical`, `1e999`, `1_000`, `0x1p4`).
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
// no schedule to resolve and no tree: refusedThresholdTextIn with no root
// `defaults:`, which reads a `<key>_critical` by resolveCriticalRows' whole
// value. da-guard asks it only of a key the resolver recorded refusing.
func refusedThresholdText(key, raw string) string {
	r, _ := refusedThresholdTextIn(nil, key, raw)
	return r
}

// refusedThresholdTextIn is why the row resolvers of a tree whose root
// `defaults:` (canonical) is defaults refuse raw, the value of threshold key
// key (canonical): RefusedNonFinite, RefusedUnparsed or "" — the one
// classifier of a threshold key's value, which RefusedValueAt (da-guard)
// and the write gate's threshold value finding (ValidateTenantKeys, #3267)
// both are. whole reports that resolveCriticalRows' whole-value reading is
// the one that refused it.
//
// A `<key>_critical` is read by whichever resolvers read it in the tree:
// resolveCriticalRows (the whole value is the number) when the tree values
// its base, or when it values neither — the key's own reading, as da-guard
// asks it; resolveBaseRows (the shared `value[:severity]` grammar) when the
// root `defaults:` values the `_critical` key itself (#3267 review: `90:
// critical` is then served). Refused when any of them refuses it. Every
// other key is read by the shared grammar.
func refusedThresholdTextIn(defaults map[string]float64, key, raw string) (refused string, whole bool) {
	base, critical := criticalRowBase(key)
	wholeReads, sharedReads := false, true
	if critical {
		_, valued := defaults[key]
		_, baseValued := defaults[base]
		wholeReads, sharedReads = !valued || baseValued, valued
	}
	// The common case first, with no allocation: a number every resolver
	// reading the key reads. A number is never a disable.
	wholeOK, sharedOK := !wholeReads, !sharedReads
	if wholeReads {
		_, wholeOK = parseThresholdNumber(strings.TrimSpace(raw))
	}
	if sharedReads {
		_, _, sharedOK = parseThresholdValue(raw)
	}
	if wholeOK && sharedOK {
		return "", false
	}
	if off, _ := thresholdRowDisabled(raw); off {
		return "", false
	}
	if !wholeOK {
		if nonFiniteNumberText(strings.TrimSpace(raw)) {
			return RefusedNonFinite, true
		}
		return RefusedUnparsed, true
	}
	if nonFiniteThresholdText(raw) {
		return RefusedNonFinite, false
	}
	return RefusedUnparsed, false
}

// ThresholdText is one threshold value's text as the exporter reads it, for
// a caller that holds the text alone — no tenant, no tree: da-guard
// threshold-values (#3267), which the unit-sanity lint reads every value it
// judges through, so that lint has no reading of its own.
//
// ⛔ NO READING OF ITS OWN. Every field is one of this file's readers:
// Severity is splitThresholdSeverity's, Number parseThresholdValue's,
// Refused RefusedValueAt's; NonFinite is the NaN or ±Inf the value half
// names (nonFiniteNumberText) or, for a text in the grammar
// (thresholdNumberRe), overflows to.
type ThresholdText struct {
	// Severity is the `:severity` suffix as splitThresholdSeverity reads it
	// ("warning" with no suffix), or "" when it refuses the suffix (`70:high`,
	// `70:`): then neither Number nor NonFinite is read.
	Severity string
	// Number is the value parseThresholdValue reads (the shared grammar,
	// finite only); HasNumber is false when it reads none.
	Number    float64
	HasNumber bool
	// NonFinite is NaN or ±Inf when the value half names one — `NaN`, `-Inf`,
	// `infinity` — or is a number of the grammar past float64 (`1e999`:
	// ErrRange, ±Inf); HasNonFinite is false otherwise, and for a text
	// outside the grammar that overflows (`0x1p1024`, `1_0e999`, #3457). The
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
	overflows := errors.Is(perr, strconv.ErrRange) && thresholdNumberRe.MatchString(value)
	if (perr == nil || overflows) && !finiteThreshold(v) {
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
