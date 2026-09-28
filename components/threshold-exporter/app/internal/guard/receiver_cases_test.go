package guard

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

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
		// #2295: a `yaml` row is decoded as da-guard decodes conf.d (yaml.v3).
		if tc.YAML != "" {
			if err := yaml.Unmarshal([]byte(tc.YAML), &tc.Receiver); err != nil {
				t.Fatalf("%s: yaml: %v", tc.Name, err)
			}
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
