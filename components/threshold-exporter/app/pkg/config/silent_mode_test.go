package config

// #3227: silentModeOf is the one reading of _silent_mode. These tests pin
// that silentModeProblem flags exactly what the resolver WARNs about, and
// that the canonical form compares equal exactly when the resolver acts the
// same.

import (
	"fmt"
	"reflect"
	"slices"
	"sort"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

// silentModeCase is one tenant-file spelling of _silent_mode.
type silentModeCase struct {
	name        string
	doc         string // a tenant's override map, as YAML
	wantProblem bool
}

// silentModeCases covers every word the reader knows (with case and
// surrounding space), the empty / null spellings, non-string shapes, and the
// structured form's failure modes.
func silentModeCases(t testing.TB) []silentModeCase {
	var cs []silentModeCase
	for _, w := range append(append([]string{}, silentModeTargets...), disabledWords(t)...) {
		cs = append(cs,
			silentModeCase{"word/" + w, "_silent_mode: " + w, false},
			silentModeCase{"upper/" + w, "_silent_mode: " + strings.ToUpper(w), false},
			silentModeCase{"spaced/" + w, `_silent_mode: "  ` + w + ` "`, false},
			silentModeCase{"structured/" + w, "_silent_mode:\n  target: " + strings.ToUpper(w), false},
		)
	}
	return append(cs,
		silentModeCase{"purple", "_silent_mode: purple", true},
		silentModeCase{"Purple", "_silent_mode: Purple", true},
		silentModeCase{"orange", "_silent_mode: orange", true},
		silentModeCase{"empty", `_silent_mode: ""`, false},
		silentModeCase{"null", "_silent_mode: null", false},
		silentModeCase{"tilde", "_silent_mode: ~", false},
		silentModeCase{"bare", "_silent_mode:", false},
		silentModeCase{"int", "_silent_mode: 5", true},
		silentModeCase{"sequence", "_silent_mode: [warning, critical]", true},
		silentModeCase{"structured/purple", "_silent_mode:\n  target: purple", true},
		silentModeCase{"structured/empty-target", "_silent_mode:\n  target: \"\"\n  reason: x", false},
		// No target key: the mapping is serialized without "target:", so the
		// reader takes the scalar path and WARNs on the whole text.
		silentModeCase{"structured/no-target", "_silent_mode:\n  reason: x", true},
		silentModeCase{"structured/target-seq", "_silent_mode:\n  target: [warning]", true},
		silentModeCase{"string/unparseable", `_silent_mode: "target: ["`, true},
		silentModeCase{"expires/invalid", "_silent_mode:\n  target: all\n  expires: nope", true},
		silentModeCase{"expires/invalid-disabled-target", "_silent_mode:\n  target: off\n  expires: nope", false},
		silentModeCase{"expires/invalid-unknown-target", "_silent_mode:\n  target: purple\n  expires: nope", true},
		silentModeCase{"expires/past", "_silent_mode:\n  target: warning\n  expires: \"2020-01-01T00:00:00Z\"", false},
		// The zero instant parses and reads as already expired; with no
		// expires nothing expires. The canonical form must keep them apart
		// (HasExpires), since Expires is the zero time in both.
		silentModeCase{"expires/zero-instant", "_silent_mode:\n  target: warning\n  expires: \"0001-01-01T00:00:00Z\"", false},
		silentModeCase{"expires/future", "_silent_mode:\n  target: warning\n  expires: \"2099-01-01T00:00:00Z\"\n  reason: maint", false},
		silentModeCase{"expires/future-offset", "_silent_mode:\n  target: warning\n  expires: \"2099-01-01T08:00:00+08:00\"\n  reason: maint", false},
		// An unquoted date is a YAML timestamp: re-serialized as RFC3339.
		silentModeCase{"expires/date-only-unquoted", "_silent_mode:\n  target: critical\n  expires: 2026-07-01", false},
		silentModeCase{"expires/date-only-quoted", "_silent_mode:\n  target: critical\n  expires: \"2026-07-01\"", true},
		// Only the default is read; the overrides are not consulted.
		silentModeCase{"windowed/purple", "_silent_mode:\n  default: purple\n  overrides:\n    - window: \"00:00-06:00\"\n      value: warning", true},
		silentModeCase{"windowed/warning", "_silent_mode:\n  default: warning\n  overrides:\n    - window: \"00:00-06:00\"\n      value: purple", false},
	)
}

// parseSilentMode reads a tenant's override map the way the loader does and
// returns its _silent_mode value.
func parseSilentMode(t *testing.T, doc string) ScheduledValue {
	t.Helper()
	var m map[string]ScheduledValue
	if err := yaml.Unmarshal([]byte(doc), &m); err != nil {
		t.Fatalf("parse %q: %v", doc, err)
	}
	return m["_silent_mode"]
}

// silentModeReaderAt runs the resolver over one tenant holding sv and returns
// its entries (Expires in UTC, sorted) and the WARN lines it wrote.
func silentModeReaderAt(sv ScheduledValue, now time.Time) ([]ResolvedSilentMode, []string) {
	cfg := &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"t1": {"_silent_mode": sv}}}
	var warns []string
	entries := cfg.resolveSilentModesAt(now, func(format string, args ...any) {
		warns = append(warns, fmt.Sprintf(format, args...))
	})
	for i := range entries {
		entries[i].Expires = entries[i].Expires.UTC()
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].TargetSeverity < entries[j].TargetSeverity })
	return entries, warns
}

// silentModeNows straddle every expires in silentModeCases.
var silentModeNows = []time.Time{
	time.Date(2019, 1, 1, 0, 0, 0, 0, time.UTC),
	time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
	time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC),
	time.Date(2100, 1, 1, 0, 0, 0, 0, time.UTC),
}

// TestSilentModeProblemMatchesReaderWarn: silentModeProblem(v) != "" exactly
// when the resolver writes a WARN for v — the invariant the write gate
// relies on (a value it rejects is one the exporter ignores, and back).
func TestSilentModeProblemMatchesReaderWarn(t *testing.T) {
	t.Parallel()
	for _, c := range silentModeCases(t) {
		sv := parseSilentMode(t, c.doc)
		problem := silentModeProblem(sv)
		if (problem != "") != c.wantProblem {
			t.Errorf("%s: silentModeProblem(%+v) = %q, want problem=%v", c.name, sv, problem, c.wantProblem)
		}
		for _, now := range silentModeNows {
			entries, warns := silentModeReaderAt(sv, now)
			if (problem != "") != (len(warns) > 0) {
				t.Errorf("%s @%s: problem %q but resolver WARNs %q", c.name, now.Format("2006"), problem, warns)
			}
			if len(warns) > 1 {
				t.Errorf("%s: %d WARNs for one value: %q", c.name, len(warns), warns)
			}
			if problem != "" && len(entries) != 0 {
				t.Errorf("%s: problem %q yet the resolver silences %+v", c.name, problem, entries)
			}
		}
	}
}

// TestSilentModeCanonicalContract: two values' canonical forms are equal
// exactly when the resolver produces the same entries and the same WARN text
// for them at every probed now — checked over every pair of silentModeCases.
func TestSilentModeCanonicalContract(t *testing.T) {
	t.Parallel()
	cases := silentModeCases(t)
	type observed struct {
		entries [][]ResolvedSilentMode
		warns   [][]string
	}
	canon := make([]silentModeCanonical, len(cases))
	obs := make([]observed, len(cases))
	for i, c := range cases {
		sv := parseSilentMode(t, c.doc)
		r := silentModeOf(sv)
		canon[i] = r.canonical()
		for _, now := range silentModeNows {
			e, w := silentModeReaderAt(sv, now)
			obs[i].entries = append(obs[i].entries, e)
			obs[i].warns = append(obs[i].warns, w)
		}
	}
	for i := range cases {
		for j := range cases {
			same := reflect.DeepEqual(obs[i], obs[j])
			if eq := canon[i] == canon[j]; eq != same {
				t.Errorf("%s vs %s: canonical equal=%v but reader behaves the same=%v\n  %+v\n  %+v",
					cases[i].name, cases[j].name, eq, same, canon[i], canon[j])
			}
			if (canon[i] == canon[j]) != reflect.DeepEqual(canon[i], canon[j]) {
				t.Errorf("%s vs %s: == and DeepEqual disagree", cases[i].name, cases[j].name)
			}
		}
	}
}

// TestSilentModeCanonicalSpellings: spellings a writer may change without
// changing what the exporter does compare equal; changes the exporter acts
// on do not.
func TestSilentModeCanonicalSpellings(t *testing.T) {
	t.Parallel()
	equal := [][2]string{
		{"_silent_mode:\n  target: warning", "_silent_mode:\n  target: \"warning\""},
		{"_silent_mode:\n  target: warning\n  expires: 2099-01-01T00:00:00Z", "_silent_mode:\n  target: warning\n  expires: \"2099-01-01T00:00:00Z\""},
		{"_silent_mode:\n  target: warning\n  expires: \"2099-01-01T08:00:00+08:00\"", "_silent_mode:\n  target: warning\n  expires: \"2099-01-01T00:00:00Z\""},
		{"_silent_mode:\n  target: all\n  reason: r\n  expires: \"2099-01-01T00:00:00Z\"", "_silent_mode:\n  expires: \"2099-01-01T00:00:00Z\"\n  reason: r\n  target: all"},
		{"_silent_mode:\n  # why\n  target: all  # both\n  reason: r", "_silent_mode: {target: all, reason: r}"},
		{"_silent_mode: Purple", "_silent_mode: purple"},
		{"_silent_mode: PURPLE", "_silent_mode: \" purple \""},
		{"_silent_mode:\n  target: Purple", "_silent_mode:\n  target: purple"},
		{"_silent_mode: warning", "_silent_mode:\n  target: warning"},
		{"_silent_mode: all", "_silent_mode: {target: ALL}"},
		{"_silent_mode: off", "_silent_mode: disable"},
		{"_silent_mode: \"target: warning\"", "_silent_mode:\n  target: warning"},
	}
	unequal := [][2]string{
		{"_silent_mode: purple", "_silent_mode: orange"},
		{"_silent_mode:\n  target: purple", "_silent_mode: purple"},
		{"_silent_mode: warning", "_silent_mode: critical"},
		{"_silent_mode: warning", "_silent_mode: all"},
		{"_silent_mode:\n  target: warning\n  expires: \"2099-01-01T00:00:00Z\"", "_silent_mode:\n  target: warning\n  expires: \"2099-01-02T00:00:00Z\""},
		{"_silent_mode:\n  target: warning\n  expires: \"2099-01-01T00:00:00Z\"", "_silent_mode:\n  target: warning"},
		{"_silent_mode:\n  target: warning\n  reason: a", "_silent_mode:\n  target: warning\n  reason: b"},
		{"_silent_mode:\n  target: all\n  expires: nope", "_silent_mode:\n  target: all\n  expires: nada"},
		{"_silent_mode: warning", "_silent_mode: off"},
	}
	for _, p := range equal {
		a, b := silentModeCanonicalOf(parseSilentMode(t, p[0])), silentModeCanonicalOf(parseSilentMode(t, p[1]))
		if a != b {
			t.Errorf("want equal:\n  %q -> %+v\n  %q -> %+v", p[0], a, p[1], b)
		}
	}
	for _, p := range unequal {
		a, b := silentModeCanonicalOf(parseSilentMode(t, p[0])), silentModeCanonicalOf(parseSilentMode(t, p[1]))
		if a == b {
			t.Errorf("want unequal:\n  %q -> %+v\n  %q -> %+v", p[0], a, p[1], b)
		}
	}
}

// TestSilentModeTargetsMatchResolver: silentModeTargets is exactly the set
// resolveSilentTarget expands, so the "(valid: ...)" list cannot drift from
// what the resolver honours.
func TestSilentModeTargetsMatchResolver(t *testing.T) {
	t.Parallel()
	candidates := append(append([]string{"", "purple", "warn", "crit", "both", "none"}, silentModeTargets...), disabledWords(t)...)
	for _, c := range candidates {
		got := len(resolveSilentTarget("t1", c, time.Time{}, "", false)) > 0
		if want := slices.Contains(silentModeTargets, c); got != want {
			t.Errorf("resolveSilentTarget(%q) silences=%v, silentModeTargets lists it=%v", c, got, want)
		}
	}
	if want := "warning, critical, all, disable"; silentModeValidValues != want {
		t.Errorf("silentModeValidValues = %q, want %q (printed in the existing WARN)", silentModeValidValues, want)
	}
}

// TestDisabledWordsUnchanged pins the disabled vocabulary: every threshold
// key, custom alert and _silent_mode reads it, so a change here changes
// which values switch a key off.
func TestDisabledWordsUnchanged(t *testing.T) {
	t.Parallel()
	if want := []string{"disable", "disabled", "off", "false"}; !slices.Equal(disabledWords(t), want) {
		t.Errorf("disabledWords = %q, want %q", disabledWords(t), want)
	}
}

// silentModeCanonicalOf is silentModeOf(sv).canonical() in one expression.
func silentModeCanonicalOf(sv ScheduledValue) silentModeCanonical {
	r := silentModeOf(sv)
	return r.canonical()
}

// TestSilentModeProblemText pins the sentence the write gate shows for each
// kind of value the reader ignores. The underlying parser errors are taken
// from the parsers themselves; everything around them is written out here.
func TestSilentModeProblemText(t *testing.T) {
	t.Parallel()
	yamlErr := yaml.Unmarshal([]byte("target: ["), &silentModeStructured{})
	if yamlErr == nil {
		t.Fatal(`yaml accepted "target: ["`)
	}
	_, timeErr := time.Parse(time.RFC3339, "nope")
	if timeErr == nil {
		t.Fatal(`time.Parse accepted "nope"`)
	}
	for _, c := range []struct{ doc, want string }{
		{`_silent_mode: "target: ["`, "structured _silent_mode does not parse: " + yamlErr.Error()},
		{"_silent_mode:\n  target: all\n  expires: nope", `expires "nope" is not RFC3339 (e.g. 2026-07-01T00:00:00Z): ` + timeErr.Error()},
		{"_silent_mode:\n  target: Purple", `unknown silent mode target "purple" (valid: warning, critical, all, disable)`},
		{"_silent_mode: Purple", `unknown silent mode "purple" (valid: warning, critical, all, disable)`},
	} {
		if got := silentModeProblem(parseSilentMode(t, c.doc)); got != c.want {
			t.Errorf("silentModeProblem(%q)\n got  %q\n want %q", c.doc, got, c.want)
		}
	}
}
