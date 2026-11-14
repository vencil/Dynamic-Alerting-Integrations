package main

import (
	"bytes"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
)

// thresholdValuesRun runs the subcommand on stdin and returns its exit code,
// stdout and stderr.
func thresholdValuesRun(t *testing.T, stdin string, args ...string) (int, string, string) {
	t.Helper()
	var out, errOut bytes.Buffer
	code := runThresholdValues(args, strings.NewReader(stdin), &out, &errOut)
	return code, out.String(), errOut.String()
}

// thresholdValuesAnswer is one stdout element, decoded strictly (every field
// present; number null or a number).
type thresholdValuesAnswer struct {
	Key       string   `json:"key"`
	Text      string   `json:"text"`
	Number    *float64 `json:"number"`
	NonFinite string   `json:"non_finite"`
	Severity  string   `json:"severity"`
	Refused   string   `json:"refused"`
}

func thresholdValuesAnswers(t *testing.T, items [][2]string) []thresholdValuesAnswer {
	t.Helper()
	in := make([]map[string]string, 0, len(items))
	for _, it := range items {
		in = append(in, map[string]string{"key": it[0], "text": it[1]})
	}
	raw, err := json.Marshal(in)
	if err != nil {
		t.Fatal(err)
	}
	code, out, errOut := thresholdValuesRun(t, string(raw))
	if code != exitOK {
		t.Fatalf("exit %d, stderr %q", code, errOut)
	}
	var docs []map[string]json.RawMessage
	if err := json.Unmarshal([]byte(out), &docs); err != nil {
		t.Fatalf("stdout is not a JSON array: %v\n%s", err, out)
	}
	if len(docs) != len(items) {
		t.Fatalf("%d answers for %d values:\n%s", len(docs), len(items), out)
	}
	answers := make([]thresholdValuesAnswer, len(docs))
	for i, d := range docs {
		for _, f := range []string{"key", "text", "number", "non_finite", "severity", "refused"} {
			if _, ok := d[f]; !ok {
				t.Fatalf("answer %d has no %q: %s", i, f, out)
			}
		}
		if len(d) != 6 {
			t.Fatalf("answer %d has fields beyond the six: %s", i, out)
		}
		b, _ := json.Marshal(d)
		dec := json.NewDecoder(bytes.NewReader(b))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&answers[i]); err != nil {
			t.Fatalf("answer %d: %v", i, err)
		}
		if answers[i].Key != items[i][0] || answers[i].Text != items[i][1] {
			t.Fatalf("answer %d is for (%q, %q), asked (%q, %q)", i,
				answers[i].Key, answers[i].Text, items[i][0], items[i][1])
		}
	}
	return answers
}

func num(v float64) *float64 { return &v }

func TestThresholdValues_ReadsAsTheExporter(t *testing.T) {
	cases := []struct {
		key, text string
		want      thresholdValuesAnswer
	}{
		{"cpu", "70", thresholdValuesAnswer{Number: num(70), Severity: "warning"}},
		{"cpu", "1e3", thresholdValuesAnswer{Number: num(1000), Severity: "warning"}},
		{"cpu", "+.5", thresholdValuesAnswer{Number: num(0.5), Severity: "warning"}},
		// #3457: outside the grammar of a threshold number, whatever
		// strconv.ParseFloat reads it as.
		{"cpu", "1_000", thresholdValuesAnswer{Severity: "warning", Refused: "unparsed"}},
		{"cpu", "0x1p4", thresholdValuesAnswer{Severity: "warning", Refused: "unparsed"}},
		{"cpu", "0x1F:critical", thresholdValuesAnswer{Severity: "critical", Refused: "unparsed"}},
		{"cpu", "123456789012345678901", thresholdValuesAnswer{Severity: "warning", Refused: "unparsed"}},
		{"cpu", "12345678901234567890", thresholdValuesAnswer{Number: num(12345678901234567890), Severity: "warning"}},
		{"cpu", "70:critical", thresholdValuesAnswer{Number: num(70), Severity: "critical"}},
		{"cpu", " 70 : Critical ", thresholdValuesAnswer{Number: num(70), Severity: "critical"}},
		{"cpu", "70:high", thresholdValuesAnswer{Refused: "unparsed"}},
		{"cpu", "70:", thresholdValuesAnswer{Refused: "unparsed"}},
		{"cpu", "1e999", thresholdValuesAnswer{NonFinite: "+Inf", Severity: "warning", Refused: "unparsed"}},
		{"cpu", "-1e999:critical", thresholdValuesAnswer{NonFinite: "-Inf", Severity: "critical", Refused: "unparsed"}},
		{"cpu", "0x1p1024", thresholdValuesAnswer{Severity: "warning", Refused: "unparsed"}},
		{"cpu", "1_0e999", thresholdValuesAnswer{Severity: "warning", Refused: "unparsed"}},
		{"cpu", "abc", thresholdValuesAnswer{Severity: "warning", Refused: "unparsed"}},
		{"cpu", "+nan", thresholdValuesAnswer{Severity: "warning", Refused: "unparsed"}},
		{"cpu", "disable", thresholdValuesAnswer{Severity: "warning"}},
		{"cpu", "disable:critical", thresholdValuesAnswer{Severity: "critical", Refused: "unparsed"}},
		{"cpu", "NaN", thresholdValuesAnswer{NonFinite: "NaN", Severity: "warning", Refused: "non_finite"}},
		{"cpu", "Inf", thresholdValuesAnswer{NonFinite: "+Inf", Severity: "warning", Refused: "non_finite"}},
		{"cpu", "-infinity", thresholdValuesAnswer{NonFinite: "-Inf", Severity: "warning", Refused: "non_finite"}},
		{"cpu", "-Inf:critical", thresholdValuesAnswer{NonFinite: "-Inf", Severity: "critical", Refused: "non_finite"}},
		{"cpu", "Inf:high", thresholdValuesAnswer{Refused: "unparsed"}},
		// A `_critical` key's value is read whole (resolveCriticalRows): only
		// refused depends on the key.
		{"cpu_critical", "90", thresholdValuesAnswer{Number: num(90), Severity: "warning"}},
		{"cpu_critical", "90:critical", thresholdValuesAnswer{Number: num(90), Severity: "critical", Refused: "unparsed"}},
		{"cpu_critical", "Inf", thresholdValuesAnswer{NonFinite: "+Inf", Severity: "warning", Refused: "non_finite"}},
		{"cpu_critical", "disable", thresholdValuesAnswer{Severity: "warning"}},
	}
	items := make([][2]string, len(cases))
	for i, c := range cases {
		items[i] = [2]string{c.key, c.text}
	}
	got := thresholdValuesAnswers(t, items)
	for i, c := range cases {
		want := c.want
		want.Key, want.Text = c.key, c.text
		g := got[i]
		if (g.Number == nil) != (want.Number == nil) || (g.Number != nil && *g.Number != *want.Number) ||
			g.NonFinite != want.NonFinite || g.Severity != want.Severity || g.Refused != want.Refused {
			gj, _ := json.Marshal(g)
			wj, _ := json.Marshal(want)
			t.Errorf("(%q, %q):\n  got  %s\n  want %s", c.key, c.text, gj, wj)
		}
	}
}

func TestThresholdValues_EmptyArrayIsAnEmptyArray(t *testing.T) {
	code, out, errOut := thresholdValuesRun(t, "[]")
	if code != exitOK || out != "[]\n" {
		t.Fatalf("exit %d, stdout %q, stderr %q; want 0 and []", code, out, errOut)
	}
}

func TestThresholdValues_RefusesWhatIsNotAnArrayOfPairs(t *testing.T) {
	for _, stdin := range []string{
		"", "null", "{}", `"70"`, `["70"]`, `[null]`, `[{"key":"cpu"}]`, `[{"text":"70"}]`,
		`[{"key":"cpu","text":70}]`, `[{"key":"cpu","text":null}]`, `[{"key":"cpu","text":"70","x":1}]`,
		`[] []`, `[{"key":"cpu","text":"70"}`, `not json`,
		// Field names are exact: encoding/json would fold case and keep the last.
		`[{"KEY":"cpu","text":"70"}]`, `[{"key":"cpu","Text":"70"}]`, `[{"key":"a","Key":"b","text":"70"}]`,
		`[{"key":"a","key":"b","text":"70"}]`, `[{"key":"cpu","text":"70","text":"80"}]`,
		`[{"key":"cpu","text":{}}]`, `[{"key":"cpu","text":["70"]}]`, `[{"key":"cpu","text":"70"},]`,
		`[[]]`, `{"key":"cpu","text":"70"}`,
	} {
		code, out, errOut := thresholdValuesRun(t, stdin)
		if code != exitCallerErr || out != "" || !strings.Contains(errOut, thresholdValuesCmd) {
			t.Errorf("stdin %q: exit %d, stdout %q, stderr %q; want exit 2, nothing on stdout", stdin, code, out, errOut)
		}
	}
}

func TestThresholdValues_ArgumentsAndHelp(t *testing.T) {
	if code, out, _ := thresholdValuesRun(t, "[]", "extra"); code != exitCallerErr || out != "" {
		t.Errorf("an argument: exit %d, stdout %q; want 2", code, out)
	}
	if code, _, _ := thresholdValuesRun(t, "[]", "--bogus"); code != exitCallerErr {
		t.Errorf("an unknown flag: exit %d; want 2", code)
	}
	code, out, errOut := thresholdValuesRun(t, "", "-h")
	if code != exitOK || out != "" || !strings.Contains(errOut, "Usage: da-guard threshold-values") {
		t.Errorf("-h: exit %d, stdout %q, stderr %q", code, out, errOut)
	}
	// Dispatched from the top level (the help path reads no stdin).
	var o, e bytes.Buffer
	if code := run([]string{thresholdValuesCmd, "-h"}, &o, &e); code != exitOK ||
		!strings.Contains(e.String(), "Usage: da-guard threshold-values") {
		t.Errorf("run threshold-values -h: exit %d, stderr %q", code, e.String())
	}
	o.Reset()
	e.Reset()
	if code := run([]string{"-h"}, &o, &e); code != exitOK || !strings.Contains(e.String(), thresholdValuesCmd) {
		t.Errorf("top-level -h does not name %s: %q", thresholdValuesCmd, e.String())
	}
}

func TestThresholdValues_OrderAndUTF8ArePreserved(t *testing.T) {
	items := [][2]string{{"b", "2"}, {"a", "1"}, {"b", "2"}, {"k<&>", "７０"}, {"x", ""}}
	got := thresholdValuesAnswers(t, items)
	if *got[0].Number != 2 || *got[1].Number != 1 || *got[2].Number != 2 {
		t.Errorf("answers out of order: %+v", got)
	}
	if got[3].Number != nil || got[3].Refused != "unparsed" || got[4].Number != nil || got[4].Refused != "unparsed" {
		t.Errorf("a non-ASCII digit or an empty text read as a number: %+v %+v", got[3], got[4])
	}
}

// thresholdMatrixRow is tests/shared/threshold_value_matrix.json's row, the
// columns Go's own test (pkg/config threshold_value_parity_test.go) writes.
type thresholdMatrixRow struct {
	Value       string  `json:"value"`
	OK          bool    `json:"ok"`
	ValuePart   *string `json:"value_part"`
	Severity    *string `json:"severity"`
	FloatOK     *bool   `json:"float_ok"`
	RowDisabled bool    `json:"row_disabled"`
}

// TestThresholdValues_AgreesWithTheMatrix: for every row of the matrix Go
// writes, the subcommand reads the value as the row's columns say — for a
// base key and for a `_critical` key.
func TestThresholdValues_AgreesWithTheMatrix(t *testing.T) {
	_, thisFile, _, _ := runtime.Caller(0)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "threshold_value_matrix.json"))
	if err != nil {
		t.Fatal(err)
	}
	var m struct {
		Rows []thresholdMatrixRow `json:"rows"`
	}
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	if len(m.Rows) == 0 {
		t.Fatal("matrix has no rows")
	}
	var items [][2]string
	for _, r := range m.Rows {
		items = append(items, [2]string{"mysql_connections", r.Value}, [2]string{"mysql_connections_critical", r.Value})
	}
	got := thresholdValuesAnswers(t, items)
	for i, r := range m.Rows {
		base, crit := got[2*i], got[2*i+1]
		rowOK := r.OK && *r.FloatOK
		switch {
		case !r.OK:
			if base.Severity != "" || base.Number != nil || base.NonFinite != "" {
				t.Errorf("%q: refused suffix, got %+v", r.Value, base)
			}
		default:
			if base.Severity != *r.Severity {
				t.Errorf("%q: severity %q, matrix %q", r.Value, base.Severity, *r.Severity)
			}
			if (base.Number != nil) != *r.FloatOK {
				t.Errorf("%q: number %v, matrix float_ok %v", r.Value, base.Number, *r.FloatOK)
			}
			if base.Number != nil {
				want, _ := strconv.ParseFloat(*r.ValuePart, 64)
				if *base.Number != want {
					t.Errorf("%q: number %v, ParseFloat(value_part) %v", r.Value, *base.Number, want)
				}
			}
			if base.NonFinite != "" {
				f, _ := strconv.ParseFloat(*r.ValuePart, 64)
				if *r.FloatOK || !(math.IsNaN(f) || math.IsInf(f, 0)) {
					t.Errorf("%q: non_finite %q for value_part %q", r.Value, base.NonFinite, *r.ValuePart)
				}
			}
		}
		if (base.Refused == "") != (rowOK || r.RowDisabled) {
			t.Errorf("%q: refused %q, matrix ok&&float_ok %v row_disabled %v", r.Value, base.Refused, rowOK, r.RowDisabled)
		}
		// The one key-dependent field: a `_critical` value is read whole,
		// so a suffix refuses it; everything else is the base reading.
		if crit.Severity != base.Severity || (crit.Number == nil) != (base.Number == nil) || crit.NonFinite != base.NonFinite {
			t.Errorf("%q: the _critical key read differently beyond refused: %+v vs %+v", r.Value, crit, base)
		}
		wholeNumber := rowOK && !strings.Contains(r.Value, ":")
		if (crit.Refused == "") != (wholeNumber || r.RowDisabled) {
			t.Errorf("%q: _critical refused %q", r.Value, crit.Refused)
		}
	}
}
