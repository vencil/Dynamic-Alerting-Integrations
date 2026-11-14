package config

// threshold_value_parity_test.go — Go's half of
// tests/shared/threshold_value_matrix.json (#3071), and the program that
// writes the matrix's verdict columns and its `severities`.
//
// splitThresholdSeverity is the one grammar of a threshold value's
// `value[:severity]` suffix. The matrix's `ok` / `value_part` / `severity`
// columns are what it says about each `value`; `grammar_ok` is whether the
// value half is in the grammar of a threshold number (thresholdNumberRe, the
// recipe contract's patterns.thresholdNumber, #3457) and `float_ok` whether
// parseThresholdNumber reads it (in the grammar and finite, #3140);
// `row_disabled` is thresholdRowDisabled's (#3141), `custom_alert_disabled`
// resolveOneCustomAlert's, `severities` is ThresholdSeverities(),
// `trim_code_points` the characters strings.TrimSpace trims, `disable_words`
// isDisabled's words and `disable_letters` the characters strings.ToLower
// turns into each of their letters; nobody types them by hand:
//
//	DA_REGEN_THRESHOLD_VALUE_MATRIX=1 go test ./pkg/config -run TestThresholdValueMatrix
//
// rewrites them (add a sample by adding a row with only `value`, then
// regenerate). Without the variable the test asserts the file still says what
// Go says, and that every reader of the grammar — the row resolvers'
// parseThresholdValue, custom alerts' parseCustomAlertThreshold, the subtree
// overlay's thresholdScalar — agrees with it. The Python half
// (tests/shared/test_threshold_value_parity.py) holds the Python readers and
// the schema's thresholdScalar pattern to the same columns, and the portal's
// half (tools/portal/tests/threshold-value-matrix.test.ts) the RecipeBuilder.

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"unicode"
	"unicode/utf8"
)

type thresholdValueRow struct {
	Value     string  `json:"value"`
	OK        *bool   `json:"ok,omitempty"`
	ValuePart *string `json:"value_part,omitempty"`
	Severity  *string `json:"severity,omitempty"`
	// GrammarOK: the value half is in the grammar of a threshold number
	// (thresholdNumberRe). FloatOK: and the number is finite.
	GrammarOK *bool `json:"grammar_ok,omitempty"`
	FloatOK   *bool `json:"float_ok,omitempty"`
	// CustomAlertDisabled: a custom alert with this threshold is the
	// three-state opt-out (resolveOneCustomAlert's errCustomAlertDisabled).
	CustomAlertDisabled *bool `json:"custom_alert_disabled,omitempty"`
	// RowDisabled: a threshold row with this value switches its key off
	// (thresholdRowDisabled, #3141) — `disable:<severity>` does not.
	RowDisabled *bool `json:"row_disabled,omitempty"`
}

type thresholdValueMatrix struct {
	Comment    []string `json:"_comment"`
	Severities []string `json:"severities"`
	// TrimCodePoints is every character strings.TrimSpace trims — the one
	// trim of the grammar. A reader in another language trims exactly these:
	// JavaScript's trim() also takes U+FEFF and leaves U+0085, Python's
	// strip() also takes U+001C to U+001F.
	TrimCodePoints []int `json:"trim_code_points"`
	// DisableWords is isDisabled's words, and DisableLetters, for each letter
	// of them, every character strings.ToLower turns into it (`i`: i, I and
	// U+0130) — how a threshold value is read as a disable. The JSON schema's
	// thresholdScalar pattern is generated from the two.
	DisableWords   []string            `json:"disable_words"`
	DisableLetters map[string][]int    `json:"disable_letters"`
	Rows           []thresholdValueRow `json:"rows"`
}

// disableLetters is, for each letter of words, every character
// strings.ToLower turns into it, ascending.
func disableLetters(words []string) map[string][]int {
	out := map[string][]int{}
	for _, w := range words {
		for _, l := range w {
			out[string(l)] = nil
		}
	}
	for r := rune(0); r <= unicode.MaxRune; r++ {
		if !utf8.ValidRune(r) {
			continue
		}
		if lower := strings.ToLower(string(r)); len(lower) > 0 {
			if _, wanted := out[lower]; wanted {
				out[lower] = append(out[lower], int(r))
			}
		}
	}
	return out
}

// trimCodePoints is every character strings.TrimSpace trims, ascending.
func trimCodePoints() []int {
	var out []int
	for r := rune(0); r <= unicode.MaxRune; r++ {
		if utf8.ValidRune(r) && strings.TrimSpace(string(r)) == "" {
			out = append(out, int(r))
		}
	}
	return out
}

func thresholdValueMatrixPath() string {
	_, thisFile, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "threshold_value_matrix.json")
}

// thresholdValueVerdict is splitThresholdSeverity's verdict on value, in the
// matrix's shape.
func thresholdValueVerdict(value string) thresholdValueRow {
	row := thresholdValueRow{Value: value}
	v, sev, err := splitThresholdSeverity(value)
	ok := err == nil
	row.OK = &ok
	if ok {
		row.ValuePart, row.Severity = &v, &sev
		gok := thresholdNumberRe.MatchString(v)
		row.GrammarOK = &gok
		_, fok := parseThresholdNumber(v)
		row.FloatOK = &fok
	}
	_, cerr := resolveOneCustomAlert("t", CustomAlertSpec{
		Name: "a", Recipe: "threshold", Metric: "m", Op: ">", Window: "5m", Threshold: value})
	off := errors.Is(cerr, errCustomAlertDisabled)
	row.CustomAlertDisabled = &off
	rowOff, _ := thresholdRowDisabled(value)
	row.RowDisabled = &rowOff
	return row
}

// writeThresholdValueMatrix writes one row per line, so a diff names the row.
func writeThresholdValueMatrix(t *testing.T, path string, m thresholdValueMatrix) {
	t.Helper()
	var b bytes.Buffer
	head, err := json.MarshalIndent(struct {
		Comment      []string `json:"_comment"`
		Severities   []string `json:"severities"`
		DisableWords []string `json:"disable_words"`
	}{m.Comment, m.Severities, m.DisableWords}, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	b.Write(bytes.TrimSuffix(head, []byte("\n}")))
	// One line each: a list of code points reads no better one per line.
	for _, member := range []struct {
		name  string
		value any
	}{{"disable_letters", m.DisableLetters}, {"trim_code_points", m.TrimCodePoints}} {
		line, err := json.Marshal(member.value)
		if err != nil {
			t.Fatal(err)
		}
		b.WriteString(",\n  \"" + member.name + "\": ")
		b.Write(line)
	}
	b.WriteString(",\n  \"rows\": [\n")
	for i, r := range m.Rows {
		var line bytes.Buffer
		enc := json.NewEncoder(&line)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(r); err != nil {
			t.Fatal(err)
		}
		b.WriteString("    ")
		b.Write(bytes.TrimSuffix(line.Bytes(), []byte("\n")))
		if i < len(m.Rows)-1 {
			b.WriteString(",")
		}
		b.WriteString("\n")
	}
	b.WriteString("  ]\n}\n")
	if err := os.WriteFile(path, b.Bytes(), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestThresholdValueMatrix(t *testing.T) {
	path := thresholdValueMatrixPath()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m thresholdValueMatrix
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Rows) == 0 {
		t.Fatal("matrix has no rows — a vacuous table passes nothing")
	}

	if os.Getenv("DA_REGEN_THRESHOLD_VALUE_MATRIX") == "1" {
		m.Severities = ThresholdSeverities()
		m.TrimCodePoints = trimCodePoints()
		m.DisableWords = disabledWords(t)
		m.DisableLetters = disableLetters(m.DisableWords)
		for i, r := range m.Rows {
			m.Rows[i] = thresholdValueVerdict(r.Value)
		}
		writeThresholdValueMatrix(t, path, m)
		t.Logf("rewrote %d rows of %s", len(m.Rows), path)
		return
	}

	if !reflect.DeepEqual(m.Severities, ThresholdSeverities()) {
		t.Errorf("matrix severities %v, ThresholdSeverities() %v (regenerate with "+
			"DA_REGEN_THRESHOLD_VALUE_MATRIX=1)", m.Severities, ThresholdSeverities())
	}
	if !reflect.DeepEqual(m.TrimCodePoints, trimCodePoints()) {
		t.Errorf("matrix trim_code_points %v, strings.TrimSpace trims %v (regenerate with "+
			"DA_REGEN_THRESHOLD_VALUE_MATRIX=1)", m.TrimCodePoints, trimCodePoints())
	}
	if words := disabledWords(t); !reflect.DeepEqual(m.DisableWords, words) ||
		!reflect.DeepEqual(m.DisableLetters, disableLetters(words)) {
		t.Errorf("matrix disable_words %v / disable_letters %v, isDisabled's words %v with letters %v "+
			"(regenerate with DA_REGEN_THRESHOLD_VALUE_MATRIX=1)", m.DisableWords, m.DisableLetters,
			words, disableLetters(words))
	}
	seen := map[string]bool{}
	var accepted, refused int
	for i, r := range m.Rows {
		r := r
		if seen[r.Value] {
			t.Errorf("duplicate row %q", r.Value)
		}
		seen[r.Value] = true
		t.Run(strconv.Itoa(i)+"_"+strings.Map(func(c rune) rune {
			if c == '/' || c < 0x20 {
				return '_'
			}
			return c
		}, r.Value), func(t *testing.T) {
			want := thresholdValueVerdict(r.Value)
			gotJSON, _ := json.Marshal(r)
			wantJSON, _ := json.Marshal(want)
			if !bytes.Equal(gotJSON, wantJSON) {
				t.Fatalf("matrix row is not what splitThresholdSeverity says (regenerate with "+
					"DA_REGEN_THRESHOLD_VALUE_MATRIX=1):\n  file: %s\n  go:   %s", gotJSON, wantJSON)
			}
			// Every reader of the grammar agrees with it.
			cv, cs, cerr := parseCustomAlertThreshold(r.Value)
			if (cerr == nil) != *r.OK || (cerr == nil && (cv != *r.ValuePart || cs != *r.Severity)) {
				t.Errorf("parseCustomAlertThreshold(%q) = (%q, %q, %v), matrix %s", r.Value, cv, cs, cerr, gotJSON)
			}
			rowOK := *r.OK && *r.FloatOK
			// Every threshold row resolver reads the value through the same
			// two predicates (#3140, #3141): a disable switches the key off,
			// anything else serves the base default only when it is a finite
			// number with a readable severity.
			gotRows := baseRowValues(t, r.Value)
			switch {
			case *r.RowDisabled:
				if len(gotRows) != 0 {
					t.Errorf("resolveBaseRows(%q) = %v, matrix says disabled: %s", r.Value, gotRows, gotJSON)
				}
			case rowOK:
				if v, _ := strconv.ParseFloat(*r.ValuePart, 64); len(gotRows) != 1 || gotRows[0] != v {
					t.Errorf("resolveBaseRows(%q) = %v, want [%v]: %s", r.Value, gotRows, v, gotJSON)
				}
			default:
				if len(gotRows) != 1 || gotRows[0] != 80 {
					t.Errorf("resolveBaseRows(%q) = %v, want the default [80]: %s", r.Value, gotRows, gotJSON)
				}
			}
			_, rs, pok := parseThresholdValue(r.Value)
			if pok != rowOK || (pok && rs != *r.Severity) {
				t.Errorf("parseThresholdValue(%q) = (%q, %v), matrix %s", r.Value, rs, pok, gotJSON)
			}
			if scalar := thresholdScalar(r.Value); scalar != (rowOK || *r.RowDisabled) {
				t.Errorf("thresholdScalar(%q) = %v, matrix %s", r.Value, scalar, gotJSON)
			}
		})
		switch {
		case r.OK == nil:
		case *r.OK:
			accepted++
		default:
			refused++
		}
	}
	if accepted == 0 || refused == 0 {
		t.Errorf("matrix has %d accepted and %d refused rows; it needs both", accepted, refused)
	}
}

// baseRowValues is the values resolveBaseRows serves for one tenant writing
// value for a key whose platform default is 80.
func baseRowValues(t *testing.T, value string) []float64 {
	t.Helper()
	c := &ThresholdConfig{}
	rows := c.resolveBaseRows("t", map[string]float64{"mysql_connections": 80},
		map[string]ScheduledValue{"mysql_connections": {Default: value}}, fixedNow, nil, discardLogf, nil)
	var out []float64
	for _, r := range rows {
		out = append(out, r.Value)
	}
	return out
}
