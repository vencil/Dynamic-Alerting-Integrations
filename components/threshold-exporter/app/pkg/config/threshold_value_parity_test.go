package config

// threshold_value_parity_test.go — Go's half of
// tests/shared/threshold_value_matrix.json (#3071), and the program that
// writes the matrix's verdict columns and its `severities`.
//
// splitThresholdSeverity is the one grammar of a threshold value's
// `value[:severity]` suffix. The matrix's `ok` / `value_part` / `severity` /
// `float_ok` columns are what it (and parseThresholdNumber on the value half:
// finite only, #3140) says about each `value`, `row_disabled` is
// thresholdRowDisabled's (#3141), `custom_alert_disabled` resolveOneCustomAlert's,
// and `severities` is ThresholdSeverities(); nobody types them by hand:
//
//	DA_REGEN_THRESHOLD_VALUE_MATRIX=1 go test ./pkg/config -run TestThresholdValueMatrix
//
// rewrites them (add a sample by adding a row with only `value`, then
// regenerate). Without the variable the test asserts the file still says what
// Go says, and that every reader of the grammar — the row resolvers'
// parseThresholdValue, custom alerts' parseCustomAlertThreshold, the subtree
// overlay's thresholdScalar — agrees with it. The Python half
// (tests/shared/test_threshold_value_parity.py) holds the backtest's and the
// custom-alert compiler's parsers, and the schema's thresholdScalar pattern,
// to the same columns.

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
	"time"
)

type thresholdValueRow struct {
	Value     string  `json:"value"`
	OK        *bool   `json:"ok,omitempty"`
	ValuePart *string `json:"value_part,omitempty"`
	Severity  *string `json:"severity,omitempty"`
	FloatOK   *bool   `json:"float_ok,omitempty"`
	// CustomAlertDisabled: a custom alert with this threshold is the
	// three-state opt-out (resolveOneCustomAlert's errCustomAlertDisabled).
	CustomAlertDisabled *bool `json:"custom_alert_disabled,omitempty"`
	// RowDisabled: a threshold row with this value switches its key off
	// (thresholdRowDisabled, #3141) — `disable:<severity>` does not.
	RowDisabled *bool `json:"row_disabled,omitempty"`
}

type thresholdValueMatrix struct {
	Comment    []string            `json:"_comment"`
	Severities []string            `json:"severities"`
	Rows       []thresholdValueRow `json:"rows"`
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
		Comment    []string `json:"_comment"`
		Severities []string `json:"severities"`
	}{m.Comment, m.Severities}, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	b.Write(bytes.TrimSuffix(head, []byte("\n}")))
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
		map[string]ScheduledValue{"mysql_connections": {Default: value}}, time.Now(), nil, discardLogf, nil)
	var out []float64
	for _, r := range rows {
		out = append(out, r.Value)
	}
	return out
}
