package aminhibit

// Alertmanager's verdict on the expanded "at most one of" pairs (#2984).
//
// tests/shared/http_config_exclusive_pairs.json is generated from the tenant
// schema by tests/shared/test_http_config_exclusive_pairs.py, which also
// asserts the schema refuses each `both` row and accepts each `only-*` row.
// Here config.Load judges the same rows, so a pair is pinned on both sides:
// the schema refuses exactly what Alertmanager refuses, pair by pair, and the
// `only-*` rows prove that nothing but the pair refuses `both`.

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/prometheus/alertmanager/config"
	"gopkg.in/yaml.v3"
)

func TestExclusivePairs_AlertmanagerVerdict(t *testing.T) {
	data, err := os.ReadFile(repoRoot("tests/shared/http_config_exclusive_pairs.json"))
	if err != nil {
		t.Fatalf("read pairs table: %v", err)
	}
	var rows []struct {
		Name     string         `json:"name"`
		Receiver map[string]any `json:"receiver"`
		Expect   string         `json:"expect"`
	}
	if err := json.Unmarshal(data, &rows); err != nil || len(rows) == 0 {
		t.Fatalf("parse pairs table: %v (n=%d)", err, len(rows))
	}
	for _, row := range rows {
		t.Run(row.Name, func(t *testing.T) {
			if row.Expect != "accept" && row.Expect != "reject" {
				t.Fatalf("expect must be accept or reject, got %q", row.Expect)
			}
			key, body := amReceiverBody(t, row.Receiver)
			raw, err := yaml.Marshal(map[string]any{
				"route":     map[string]any{"receiver": "r"},
				"receivers": []any{map[string]any{"name": "r", key: []any{body}}},
			})
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			got := "accept"
			_, loadErr := config.Load(string(raw))
			if loadErr != nil {
				got = "reject"
			}
			if got != row.Expect {
				t.Errorf("Alertmanager says %s (err: %v), table says %s\n%s", got, loadErr, row.Expect, raw)
			}
		})
	}
}
