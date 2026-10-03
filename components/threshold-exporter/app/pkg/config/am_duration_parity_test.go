package config

// am_duration_parity_test.go — Go's half of tests/shared/am_duration_matrix.json
// (#2490), and the program that writes the matrix's expected columns.
//
// Alertmanager reads every routing timing value (group_wait / group_interval /
// repeat_interval) with prometheus/common model.ParseDuration — the version
// pinned in this module's go.mod. The matrix's `am_*` columns are what that
// function says about each `value`; nobody types them by hand:
//
//	DA_REGEN_AM_DURATION_MATRIX=1 go test ./pkg/config -run TestAMDurationMatrix
//
// rewrites them from the rows' `value`s (add a sample by adding a row with
// only `value`, then regenerate). Without the variable the test asserts the
// file still says what model.ParseDuration says, and that the exporter's
// clampDuration takes exactly the values Alertmanager takes. The Python half
// (tests/shared/test_am_duration_parity.py) holds the route generator's
// parser and the schema's definitions.duration to the same columns.

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/common/model"
)

type amDurationRow struct {
	Value   string  `json:"value"`
	AMValid *bool   `json:"am_valid,omitempty"`
	AMNs    *int64  `json:"am_ns,omitempty"`
	AMError *string `json:"am_error,omitempty"`
}

type amDurationMatrix struct {
	Comment []string        `json:"_comment"`
	Source  string          `json:"source"`
	Rows    []amDurationRow `json:"rows"`
}

func amDurationMatrixPath() string {
	_, thisFile, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "am_duration_matrix.json")
}

// amVerdict is model.ParseDuration's verdict on value, in the matrix's shape.
func amVerdict(value string) amDurationRow {
	row := amDurationRow{Value: value}
	d, err := model.ParseDuration(value)
	valid := err == nil
	row.AMValid = &valid
	if valid {
		ns := int64(time.Duration(d))
		row.AMNs = &ns
	} else {
		msg := err.Error()
		row.AMError = &msg
	}
	return row
}

// writeAMDurationMatrix writes one row per line, so a diff names the row.
func writeAMDurationMatrix(t *testing.T, path string, m amDurationMatrix) {
	t.Helper()
	var b bytes.Buffer
	head, err := json.MarshalIndent(struct {
		Comment []string `json:"_comment"`
		Source  string   `json:"source"`
	}{m.Comment, m.Source}, "", "  ")
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

func TestAMDurationMatrix(t *testing.T) {
	path := amDurationMatrixPath()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m amDurationMatrix
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Rows) == 0 {
		t.Fatal("matrix has no rows — a vacuous table passes nothing")
	}

	if os.Getenv("DA_REGEN_AM_DURATION_MATRIX") == "1" {
		for i, r := range m.Rows {
			m.Rows[i] = amVerdict(r.Value)
		}
		writeAMDurationMatrix(t, path, m)
		t.Logf("rewrote %d rows of %s", len(m.Rows), path)
		return
	}

	seen := map[string]bool{}
	for _, r := range m.Rows {
		r := r
		if seen[r.Value] {
			t.Errorf("duplicate row %q", r.Value)
		}
		seen[r.Value] = true
		t.Run(strings.ReplaceAll(r.Value, "/", "_"), func(t *testing.T) {
			want := amVerdict(r.Value)
			gotJSON, _ := json.Marshal(r)
			wantJSON, _ := json.Marshal(want)
			if !bytes.Equal(gotJSON, wantJSON) {
				t.Fatalf("matrix row is not what model.ParseDuration says (regenerate with "+
					"DA_REGEN_AM_DURATION_MATRIX=1):\n  file: %s\n  go:   %s", gotJSON, wantJSON)
			}
			// The exporter's clamp takes exactly what Alertmanager takes:
			// an invalid value is dropped ("", WARN), a valid one renders.
			for param := range routingGuardrails {
				out := clampDuration(r.Value, param, "parity")
				if !*r.AMValid {
					if out != "" {
						t.Errorf("clampDuration(%q, %s) = %q, want \"\" (Alertmanager refuses it)", r.Value, param, out)
					}
					continue
				}
				if out == "" {
					t.Errorf("clampDuration(%q, %s) dropped a value Alertmanager takes", r.Value, param)
					continue
				}
				if _, err := model.ParseDuration(out); err != nil {
					t.Errorf("clampDuration(%q, %s) = %q, which Alertmanager refuses: %v", r.Value, param, out, err)
				}
			}
		})
	}
}
