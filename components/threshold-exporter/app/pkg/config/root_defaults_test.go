package config

import (
	"encoding/json"
	"math"
	"os"
	"strings"
	"testing"
)

// #3250: a root `defaults:` value is read as a tenant's is — its text through
// parseThresholdNumber. Every value of tests/shared/threshold_value_matrix.json
// without a `:` (a root default is a number only) is written, quoted, under a
// root key: it is served iff the matrix's float_ok (Go's own verdict, written
// by TestThresholdValueMatrix) says the tenant path reads it as a number, and
// a refused one keeps the text and the reason the tenant path gives it.
func TestRootDefaults_ReadLikeATenantValue(t *testing.T) {
	t.Parallel()
	raw, err := os.ReadFile(thresholdValueMatrixPath())
	if err != nil {
		t.Fatal(err)
	}
	var m thresholdValueMatrix
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	n := 0
	for _, row := range m.Rows {
		if strings.Contains(row.Value, ":") || strings.IndexFunc(row.Value, yamlCannotQuote) >= 0 || row.FloatOK == nil {
			continue
		}
		n++
		cfg, err := parseConfigFile([]byte("defaults:\n  k: '"+row.Value+"'\n  other: 5\n"), false)
		if err != nil {
			t.Errorf("%q: the file is refused (%v); one value must not drop the file", row.Value, err)
			continue
		}
		if cfg.Defaults["other"] != 5 {
			t.Errorf("%q: other = %v, want 5", row.Value, cfg.Defaults["other"])
		}
		v := cfg.Defaults["k"]
		text, reason, refused := RefusedDefault(v)
		if *row.FloatOK {
			want, _ := parseThresholdNumber(strings.TrimSpace(row.Value))
			if refused || v != want {
				t.Errorf("%q: Defaults = %v (refused %v), want %v served", row.Value, v, refused, want)
			}
			continue
		}
		wantReason := RefusedUnparsed
		if nonFiniteThresholdText(row.Value) {
			wantReason = RefusedNonFinite
		}
		if !refused || text != row.Value || reason != wantReason {
			t.Errorf("%q: RefusedDefault = (%q, %q, %v), want (%q, %q, true)", row.Value, text, reason, refused,
				row.Value, wantReason)
		}
	}
	if n < 10 {
		t.Fatalf("only %d matrix rows apply; the matrix moved", n)
	}
}

// The YAML spellings yaml.v3's float64 decode read its own way are the text
// now: `0x1F` was 31 and is refused (the tenant path refuses it), `.nan` was
// NaN and is refused as text ParseFloat does not read, and a value yaml.v3
// could not put in a float64 no longer drops the file.
func TestRootDefaults_YAMLSpellings(t *testing.T) {
	t.Parallel()
	cfg, err := parseConfigFile([]byte("defaults:\n  hex: 0x1F\n  hexfloat: 0x1p4\n  under: 1_000\n  sci: 7e1\n"+
		"  quoted: \"70\"\n  ynan: .nan\n  pinf: .inf\n  word: abc\n  big: 1e999\n  yes: true\n  gone: ~\n"), false)
	if err != nil {
		t.Fatal(err)
	}
	served := map[string]float64{"hexfloat": 16, "under": 1000, "sci": 70, "quoted": 70}
	for k, want := range served {
		if got := cfg.Defaults[k]; got != want {
			t.Errorf("%s = %v, want %v", k, got, want)
		}
	}
	refusedWant := map[string][2]string{
		"hex": {"0x1F", RefusedUnparsed}, "ynan": {".nan", RefusedUnparsed}, "pinf": {".inf", RefusedUnparsed},
		"word": {"abc", RefusedUnparsed}, "big": {"1e999", RefusedUnparsed}, "yes": {"true", RefusedUnparsed},
	}
	for k, want := range refusedWant {
		text, reason, refused := RefusedDefault(cfg.Defaults[k])
		if !refused || text != want[0] || reason != want[1] {
			t.Errorf("%s: RefusedDefault = (%q, %q, %v), want (%q, %q, true)", k, text, reason, refused, want[0], want[1])
		}
	}
	if _, in := cfg.Defaults["gone"]; in {
		t.Errorf("a null root default must write nothing (#2518), got %v", cfg.Defaults["gone"])
	}
	got := cfg.RefusedRootDefaults()
	if len(got) != len(refusedWant) {
		t.Errorf("RefusedRootDefaults = %v, want the %d refused keys", got, len(refusedWant))
	}
}

// The verdict travels with the value: a merge (mergePartialInto, the flat
// plane's) is a plain map copy and must not lose the text or the reason.
func TestRootDefaults_VerdictSurvivesAMerge(t *testing.T) {
	t.Parallel()
	partial, err := parseConfigFile([]byte("defaults:\n  a: NaN\n  b: zz9\n"), false)
	if err != nil {
		t.Fatal(err)
	}
	merged := ThresholdConfig{Defaults: map[string]float64{}, StateFilters: map[string]StateFilter{},
		Tenants: map[string]map[string]ScheduledValue{}, Profiles: map[string]map[string]ScheduledValue{}}
	mergePartialInto(&merged, partial)
	for k, want := range map[string][2]string{"a": {"NaN", RefusedNonFinite}, "b": {"zz9", RefusedUnparsed}} {
		text, reason, refused := RefusedDefault(merged.Defaults[k])
		if !refused || text != want[0] || reason != want[1] {
			t.Errorf("%s after merge: (%q, %q, %v), want (%q, %q, true)", k, text, reason, refused, want[0], want[1])
		}
	}
}

// yamlCannotQuote: a rune a single-quoted YAML scalar cannot carry as is.
func yamlCannotQuote(r rune) bool { return r == '\'' || r < 0x20 || r == 0x7f }

// A NaN or ±Inf that no file wrote (set by code) is refused with no text.
func TestRefusedDefault_PlainNonFinite(t *testing.T) {
	t.Parallel()
	for _, v := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		text, reason, refused := RefusedDefault(v)
		if !refused || text != "" || reason != RefusedNonFinite {
			t.Errorf("%v: (%q, %q, %v), want (\"\", non_finite, true)", v, text, reason, refused)
		}
	}
	if _, _, refused := RefusedDefault(70); refused {
		t.Error("70 must not be refused")
	}
}

// The shapes the typed decode handled keep working: an alias, a `<<` merge
// (keys written beside it win), and the typed decode's errors for a
// `defaults:` that is not a mapping, a value that is not a scalar, and a
// repeated key.
func TestRootDefaults_Shapes(t *testing.T) {
	t.Parallel()
	cfg, err := parseConfigFile([]byte("x-v: &v 9\nx-base: &b\n  a: 1\n  b: 2\ndefaults:\n  <<: *b\n  b: 3\n  c: *v\n"), false)
	if err != nil {
		t.Fatal(err)
	}
	for k, want := range map[string]float64{"a": 1, "b": 3, "c": 9} {
		if got := cfg.Defaults[k]; got != want {
			t.Errorf("%s = %v, want %v", k, got, want)
		}
	}
	for name, doc := range map[string]string{
		"not a mapping":  "defaults: [1, 2]\n",
		"mapping value":  "defaults:\n  a:\n    b: 1\n",
		"sequence value": "defaults:\n  a: [1]\n",
		"repeated key":   "defaults:\n  a: 1\n  a: 2\n",
	} {
		if _, err := parseConfigFile([]byte(doc), false); err == nil {
			t.Errorf("%s: decoded, want the typed decode's error", name)
		}
	}
}
