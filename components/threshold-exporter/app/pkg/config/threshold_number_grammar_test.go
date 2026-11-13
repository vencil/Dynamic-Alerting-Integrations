package config

// threshold_number_grammar_test.go — the one grammar of a threshold number
// (#3457, #3827): the recipe contract's patterns.thresholdNumber, which
// parseThresholdNumber reads. The grammar is stated here a second time, as a
// character count (inThresholdNumberGrammar), so that the pattern is held to
// what it is documented to accept rather than to itself.

import (
	"errors"
	"math"

	"gopkg.in/yaml.v3"
	"math/rand"
	"strconv"
	"strings"
	"testing"
	"time"
)

// maxThresholdMantissaDigits is the most digits a threshold number's mantissa
// may have, every digit counted — leading and trailing zeros, both sides of
// the `.`.
const maxThresholdMantissaDigits = 20

// inThresholdNumberGrammar is the grammar of a threshold number, by counting:
// an optional `+` or `-`; a mantissa of ASCII digits with at most one `.`, at
// least one digit and at most maxThresholdMantissaDigits; then optionally `e`
// or `E`, an optional sign and at least one ASCII digit (any number of them).
func inThresholdNumberGrammar(s string) bool {
	if s != "" && (s[0] == '+' || s[0] == '-') {
		s = s[1:]
	}
	mantissa, exponent, hasExponent := s, "", false
	if i := strings.IndexAny(s, "eE"); i >= 0 {
		mantissa, exponent, hasExponent = s[:i], s[i+1:], true
	}
	digits, dots := 0, 0
	for _, c := range mantissa {
		switch {
		case c >= '0' && c <= '9':
			digits++
		case c == '.':
			dots++
		default:
			return false
		}
	}
	if digits < 1 || digits > maxThresholdMantissaDigits || dots > 1 {
		return false
	}
	if !hasExponent {
		return true
	}
	if exponent != "" && (exponent[0] == '+' || exponent[0] == '-') {
		exponent = exponent[1:]
	}
	if exponent == "" {
		return false
	}
	for _, c := range exponent {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// Every string of up to six characters over the grammar's alphabet and the
// characters it must refuse, and every mantissa length around the bound with
// the `.` at every position: the contract's pattern accepts exactly what the
// count accepts. Widening the pattern (a `_`, a 21st digit, a hex prefix) or
// narrowing it fails here.
func TestThresholdNumberGrammar_IsTheDocumentedCount(t *testing.T) {
	t.Parallel()
	check := func(s string) {
		if got, want := thresholdNumberRe.MatchString(s), inThresholdNumberGrammar(s); got != want {
			t.Errorf("patterns.thresholdNumber matches %q = %v, the grammar says %v", s, got, want)
		}
	}
	const alphabet = "09.eE+-_x \n"
	n := 0
	var walk func(prefix string, left int)
	walk = func(prefix string, left int) {
		check(prefix)
		n++
		if left == 0 {
			return
		}
		for _, c := range alphabet {
			walk(prefix+string(c), left-1)
		}
	}
	walk("", 6)
	if n < 1_000_000 {
		t.Fatalf("only %d strings walked", n)
	}
	for digits := 1; digits <= maxThresholdMantissaDigits+3; digits++ {
		run := strings.Repeat("7", digits)
		for _, sign := range []string{"", "+", "-"} {
			for _, exp := range []string{"", "e5", "E-5", "e+" + strings.Repeat("9", 40), "e", "e_1"} {
				check(sign + run + exp)
				for dot := 0; dot <= digits; dot++ {
					check(sign + run[:dot] + "." + run[dot:] + exp)
					check(sign + run[:dot] + "_" + run[dot:] + exp)
				}
			}
		}
	}
	for _, s := range []string{"0x10", "0x1p4", "0X1P-2", "0o17", "0b101", "1_000", "Inf", "+Inf", "NaN", "infinity",
		"１２", "١٠", "1\u00a0", "\ufeff1", "1e1.5", "1.e", "..1", "1..", "+", "-", ".", "+.", "-.e1"} {
		check(s)
		if thresholdNumberRe.MatchString(s) {
			t.Errorf("patterns.thresholdNumber matches %q", s)
		}
	}
}

// A number parseThresholdNumber reads is the one strconv.ParseFloat reads,
// and it reads none outside the grammar or non-finite.
func TestParseThresholdNumber_IsTheGrammarAndFinite(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		text string
		want float64
		ok   bool
	}{
		{"70", 70, true}, {"+70", 70, true}, {"-1.5", -1.5, true}, {".5", 0.5, true}, {"5.", 5, true},
		{"1e3", 1000, true}, {"1E-2", 0.01, true}, {"017", 17, true}, {"1e-400", 0, true},
		{"12345678901234567890", 12345678901234567890, true},
		{"00000000000000000001", 1, true},
		{"1e999", 0, false}, {"-1e999", 0, false},
		{"123456789012345678901", 0, false}, {"000000000000000000001", 0, false},
		{"1_000", 0, false}, {"0x1p4", 0, false}, {"0x1F", 0, false}, {"0x10", 0, false},
		{"Inf", 0, false}, {"NaN", 0, false}, {"", 0, false}, {" 70", 0, false}, {"70 ", 0, false},
	} {
		got, ok := parseThresholdNumber(c.text)
		if ok != c.ok || got != c.want {
			t.Errorf("parseThresholdNumber(%q) = (%v, %v), want (%v, %v)", c.text, got, ok, c.want, c.ok)
		}
	}
}

// #3827: strconv.ParseFloat reads a mantissa of some 800 digits as another
// number, with no error. Such a value is outside the grammar: the exporter
// serves the platform default for it, never the misread number, and da-guard
// names it refused.
func TestLongMantissa_IsRefusedNotMisread(t *testing.T) {
	t.Parallel()
	one := "1" + strings.Repeat("0", 800) + "e-800" // mathematically 1
	if misread, err := strconv.ParseFloat(one, 64); err != nil || misread == 1 {
		t.Logf("strconv.ParseFloat reads it as %v (%v): the defect #3827 reports is gone from this Go", misread, err)
	}
	if v, ok := parseThresholdNumber(one); ok {
		t.Fatalf("parseThresholdNumber reads the 801-digit mantissa as %v", v)
	}
	if got := baseRowValues(t, one); len(got) != 1 || got[0] != 80 {
		t.Errorf("resolveBaseRows serves %v for it, want the platform default [80]", got)
	}
	if got := baseRowValues(t, one+":critical"); len(got) != 1 || got[0] != 80 {
		t.Errorf("resolveBaseRows serves %v for it with a severity, want the platform default [80]", got)
	}
	read := ReadThresholdText("mysql_connections", one)
	if read.HasNumber || read.HasNonFinite || read.Refused != RefusedUnparsed {
		t.Errorf("ReadThresholdText = %+v, want no number and refused %q", read, RefusedUnparsed)
	}
	if got := refusedThresholdText("mysql_connections_critical", one); got != RefusedUnparsed {
		t.Errorf("a _critical key: refused %q, want %q", got, RefusedUnparsed)
	}
	// The longest mantissa the grammar takes is read exactly.
	for text, want := range map[string]float64{
		"99999999999999999999":    1e20,
		"0.0000000000000000001":   1e-19,
		"1.0000000000000000000":   1,
		"9007199254740993":        9007199254740992, // 2^53+1 rounds to even
		"9007199254740993.000":    9007199254740992,
		"0.3000000000000000444":   0.30000000000000004,
		"17976931348623157e292":   math.MaxFloat64,
		"4.9406564584124654e-324": math.SmallestNonzeroFloat64,
	} {
		if got, ok := parseThresholdNumber(text); !ok || got != want {
			t.Errorf("parseThresholdNumber(%q) = (%v, %v), want %v", text, got, ok, want)
		}
	}
}

// What names NaN or ±Inf is RefusedNonFinite; a text strconv.ParseFloat reads
// as a finite number and the grammar refuses is RefusedUnparsed, in every
// place that classifies a refusal.
func TestRefusalOfATextOutsideTheGrammar_IsUnparsed(t *testing.T) {
	t.Parallel()
	for _, text := range []string{"1_000", "0x1p4", "0X1P-2", "123456789012345678901", "1" + strings.Repeat("0", 800) + "e-800"} {
		if nonFiniteNumberText(text) || nonFiniteThresholdText(text+":critical") {
			t.Errorf("%q is classified non-finite", text)
		}
		if got := refusedReason(" " + text + " "); got != RefusedUnparsed {
			t.Errorf("refusedReason(%q) = %q, want %q", text, got, RefusedUnparsed)
		}
		for _, key := range []string{"mysql_connections", "mysql_connections_critical"} {
			if got := refusedThresholdText(key, text); got != RefusedUnparsed {
				t.Errorf("refusedThresholdText(%s, %q) = %q, want %q", key, text, got, RefusedUnparsed)
			}
		}
		if got := NonFiniteThresholdWrites(map[string]ScheduledValue{"k": {Default: text}}); len(got) != 0 {
			t.Errorf("NonFiniteThresholdWrites reports %q: %v", text, got)
		}
		_, err := resolveOneCustomAlert("t", CustomAlertSpec{
			Name: "a", Recipe: "threshold", Metric: "m", Op: ">", Window: "5m", Threshold: text})
		var refusal *customAlertThresholdRefusal
		if !errors.As(err, &refusal) || refusal.refused != RefusedUnparsed {
			t.Errorf("a custom alert with threshold %q: %v, want a refusal of kind %q", text, err, RefusedUnparsed)
		}
	}
	for _, text := range []string{"NaN", "Inf", "+Inf", "-infinity"} {
		if !nonFiniteNumberText(text) || refusedReason(text) != RefusedNonFinite ||
			refusedThresholdText("mysql_connections", text) != RefusedNonFinite {
			t.Errorf("%q is not classified non-finite everywhere", text)
		}
	}
}

// thresholdNumberText writes every finite float64 as a text
// parseThresholdNumber reads back as the same number. strconv's 'g' form —
// what resolveDimensionalRows wrote a root default with, and what yaml.v3
// writes a float with — is outside the grammar for some of them.
func TestThresholdNumberText_IsAlwaysInTheGrammar(t *testing.T) {
	t.Parallel()
	check := func(v float64) {
		text := thresholdNumberText(v)
		if got, ok := parseThresholdNumber(text); !ok || got != v {
			t.Fatalf("thresholdNumberText(%v) = %q, read back as (%v, %v)", v, text, got, ok)
		}
	}
	outside := 1.2345678901234567e-4
	if g := strconv.FormatFloat(outside, 'g', -1, 64); thresholdNumberRe.MatchString(g) {
		t.Fatalf("the 'g' form %q is in the grammar: this test's sample no longer shows the difference", g)
	}
	for _, v := range []float64{0, math.Copysign(0, -1), 1, -1, 70, 0.1, 1e6, 1e21, 1e-7, outside, -outside,
		math.MaxFloat64, -math.MaxFloat64, math.SmallestNonzeroFloat64, 9007199254740993, 0.30000000000000004} {
		check(v)
	}
	rng := rand.New(rand.NewSource(3457))
	for i := 0; i < 200_000; i++ {
		v := math.Float64frombits(rng.Uint64())
		if finiteThreshold(v) {
			check(v)
		}
	}
}

// A root `defaults:` dimensional key and a subtree default carry a number,
// not a text: each is served as that number even when its shortest decimal
// spelling has more digits than the grammar takes.
func TestANumberCarriedAsAFloat_IsServed(t *testing.T) {
	t.Parallel()
	const want = 1.2345678901234567e-4
	cfg, err := ParseConfigFile([]byte(`dimensions:
  pg_connections: [env]
defaults:
  pg_connections: 100
  'pg_connections{env="prod"}': 1.2345678901234567e-4
tenants:
  ta:
    pg_connections: 90
`))
	if err != nil {
		t.Fatal(err)
	}
	var got []float64
	for _, r := range cfg.ResolveAt(time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)) {
		if len(r.CustomLabels) > 0 {
			got = append(got, r.Value)
		}
	}
	if len(got) != 1 || got[0] != want {
		t.Errorf("the root dimensional default is served as %v, want [%v]", got, want)
	}

	sv, ok := scheduledValueFromRaw(want)
	if !ok {
		t.Fatal("scheduledValueFromRaw refuses a float")
	}
	if v, _, read := parseThresholdValue(sv.Default); !read || v != want {
		t.Errorf("a subtree default of %v is carried as %q, read as (%v, %v)", want, sv.Default, v, read)
	}
	// A float whose YAML spelling is in the grammar keeps that spelling.
	if sv, _ := scheduledValueFromRaw(0.5); sv.Default != "0.5" {
		t.Errorf("a subtree default of 0.5 is carried as %q, want the YAML spelling", sv.Default)
	}
	if sv, _ := scheduledValueFromRaw(1e6); sv.Default != "1e+06" {
		t.Errorf("a subtree default of 1e6 is carried as %q, want the YAML spelling", sv.Default)
	}
}

// The scheduled form of a subtree default carries its numbers the same way:
// the default and every window's value are served as written, where the
// tenant file writing the same mapping would be.
func TestAScheduledSubtreeDefault_CarriesItsFloats(t *testing.T) {
	t.Parallel()
	const want = 1.2345678901234567e-4
	var raw any
	if err := yaml.Unmarshal([]byte(
		"default: 1.2345678901234567e-4\n"+
			"overrides:\n  - {window: \"01:00-02:00\", value: 1.2345678901234567e-4}\n"+
			"  - {window: \"03:00-04:00\", value: 0.5}\n"+
			"  - {window: \"05:00-06:00\", value: \"70:critical\"}\n"), &raw); err != nil {
		t.Fatal(err)
	}
	sv, ok := scheduledValueFromRaw(raw)
	if !ok || len(sv.Overrides) != 3 {
		t.Fatalf("scheduledValueFromRaw = %+v, %v", sv, ok)
	}
	for where, text := range map[string]string{"default": sv.Default, "the first window": sv.Overrides[0].Value} {
		if v, _, read := parseThresholdValue(text); !read || v != want {
			t.Errorf("%s is carried as %q, read as (%v, %v), want %v", where, text, v, read, want)
		}
	}
	if sv.Overrides[1].Value != "0.5" || sv.Overrides[2].Value != "70:critical" {
		t.Errorf("the other windows are carried as %q and %q, want them as written",
			sv.Overrides[1].Value, sv.Overrides[2].Value)
	}
	if !thresholdScalar(sv.Default) || !thresholdScalar(sv.Overrides[0].Value) {
		t.Error("the overlay's shape filter refuses the carried number")
	}
}
