package guard

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/pyyamlcompat"
	"gopkg.in/yaml.v3"
)

// TestReceiverPresenceCases runs the shared receiver case table
// (pkg/receiverspec/testdata/receiver_presence_cases.json) through the whole
// guard: a row is valid exactly when the routing yields no error finding.
// The contract itself is pkg/receiverspec's (TestPresenceCases there); this
// pins that the guard neither drops nor adds errors around it. The table is
// shared with tests/shared/test_receiver_spec_parity.py and
// tests/alertmanager-inhibit.
func TestReceiverPresenceCases(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "pkg", "receiverspec", "testdata", "receiver_presence_cases.json"))
	if err != nil {
		t.Fatalf("read cases: %v", err)
	}
	var cases []struct {
		Name     string         `json:"name"`
		Receiver map[string]any `json:"receiver"`
		YAML     string         `json:"yaml"`
		Valid    bool           `json:"valid"`
	}
	if err := json.Unmarshal(data, &cases); err != nil || len(cases) == 0 {
		t.Fatalf("parse cases: %v (n=%d)", err, len(cases))
	}
	for _, tc := range cases {
		// #2295: a `yaml` row is decoded as da-guard decodes a conf.d
		// receiver (pkg/pyyamlcompat over the yaml.v3 node).
		if tc.YAML != "" {
			var n yaml.Node
			if err := yaml.Unmarshal([]byte(tc.YAML), &n); err != nil {
				t.Fatalf("%s: yaml: %v", tc.Name, err)
			}
			m, ok := pyyamlcompat.Decode(&n).(map[string]any)
			if !ok {
				t.Fatalf("%s: yaml is not a mapping with string keys", tc.Name)
			}
			tc.Receiver = m
		}
		t.Run(tc.Name, func(t *testing.T) {
			var errs []string
			for _, f := range runWithRouting(t, "t1", map[string]any{"receiver": tc.Receiver}) {
				if f.Severity == SeverityError {
					errs = append(errs, f.Message)
				}
			}
			if valid := len(errs) == 0; valid != tc.Valid {
				t.Errorf("guard valid=%v, table says %v: %s", valid, tc.Valid, strings.Join(errs, "; "))
			}
		})
	}
}

// TestReceiverNonStringKey (#2295): a receiver with a key PyYAML reads as a
// non-string (`1:`) is decoded as map[any]any. The route generator and
// Alertmanager take it, so the guard hands it to receiverspec.Check as
// decoded — main, override and routes receiver — instead of reading it as
// "not an object". The contract rows are in pkg/receiverspec
// (TestCheck_NonStringKeys).
func TestReceiverNonStringKey(t *testing.T) {
	var n yaml.Node
	if err := yaml.Unmarshal([]byte("type: webhook\nurl: https://h.example/a\n1: x\n"), &n); err != nil {
		t.Fatal(err)
	}
	recv := pyyamlcompat.Decode(&n)
	if _, isAnyMap := recv.(map[any]any); !isAnyMap {
		t.Fatalf("fixture decoded as %T, want map[any]any", recv)
	}
	routing := map[string]any{
		"receiver":  recv,
		"overrides": []any{map[string]any{"alertname": "X", "receiver": recv}},
		"routes":    []any{map[string]any{"match": map[string]any{"team": "a"}, "receiver": recv}},
	}
	for _, f := range runWithRouting(t, "t1", routing) {
		if f.Severity == SeverityError {
			t.Errorf("error finding %s: %s", f.Field, f.Message)
		}
	}
}
