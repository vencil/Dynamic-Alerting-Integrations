package aminhibit

// Alertmanager's verdict on the shared receiver case table (#2180).
//
// components/threshold-exporter/app/internal/guard/testdata/receiver_presence_cases.json
// is read by the Go guard (TestReceiverPresenceCases) and by pytest
// (tests/shared/test_receiver_spec_parity.py), which both assert each
// row's `valid` — the verdict the schema, the Python route generator and
// the guard must agree on. Its `am` column records what Alertmanager
// itself does with the receiver the pipeline would hand it. This file is
// the only place that runs Alertmanager, so it is the one that keeps that
// column honest:
//
//   - every row's `am` must equal what config.Load (v0.33.1, this
//     module's pin) actually says, and
//   - a row Alertmanager rejects must have `valid: false` — one receiver
//     it cannot load fails the WHOLE config reload, not just one tenant.
//
// `am: "n/a"` is for rows whose only defect is something Alertmanager
// never sees (the receiver `type` spelling); they are not loaded.

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/prometheus/alertmanager/config"
	"gopkg.in/yaml.v3"
)

// receiverAMKey maps a receiver type to its Alertmanager config list — a
// copy of `am_key` in scripts/tools/_lib_constants.py RECEIVER_TYPES,
// which is the authority. An unknown type fails the test.
var receiverAMKey = map[string]string{
	"webhook":    "webhook_configs",
	"email":      "email_configs",
	"slack":      "slack_configs",
	"teams":      "msteams_configs",
	"rocketchat": "webhook_configs",
	"pagerduty":  "pagerduty_configs",
}

// pythonStr renders a JSON-decoded value the way Python's str() does, for
// the email `to` join below. Only the kinds the table uses are handled.
func pythonStr(t *testing.T, v any) string {
	t.Helper()
	switch x := v.(type) {
	case string:
		return x
	case nil:
		return "None"
	case bool:
		if x {
			return "True"
		}
		return "False"
	case float64:
		if x == float64(int64(x)) {
			return fmt.Sprintf("%d", int64(x))
		}
	}
	t.Fatalf("pythonStr: no Python str() rendering modelled for %#v", v)
	return ""
}

// amReceiverBody is the receiver as the pipeline hands it to
// Alertmanager. The authority is
// scripts/tools/ops/_grar_merge.py build_receiver_config: drop `type`,
// and join a list-valued email `to` with ", " (via Python str() per
// item). Table rows carry no rocketchat metadata fields, so every other
// field is forwarded as is.
func amReceiverBody(t *testing.T, receiver map[string]any) (string, map[string]any) {
	t.Helper()
	rtype, _ := receiver["type"].(string)
	key, ok := receiverAMKey[rtype]
	if !ok {
		t.Fatalf("receiver type %q has no Alertmanager key; mark the row am: \"n/a\" or extend receiverAMKey", rtype)
	}
	body := map[string]any{}
	for k, v := range receiver {
		if k != "type" {
			body[k] = v
		}
	}
	if list, isList := body["to"].([]any); isList && rtype == "email" {
		parts := make([]string, len(list))
		for i, item := range list {
			parts[i] = pythonStr(t, item)
		}
		body["to"] = strings.Join(parts, ", ")
	}
	return key, body
}

func TestReceiverCaseTable_AlertmanagerVerdict(t *testing.T) {
	data, err := os.ReadFile(repoRoot("components/threshold-exporter/app/internal/guard/testdata/receiver_presence_cases.json"))
	if err != nil {
		t.Fatalf("read case table: %v", err)
	}
	var cases []struct {
		Name     string         `json:"name"`
		Receiver map[string]any `json:"receiver"`
		Valid    *bool          `json:"valid"`
		AM       string         `json:"am"`
	}
	if err := json.Unmarshal(data, &cases); err != nil || len(cases) == 0 {
		t.Fatalf("parse case table: %v (n=%d)", err, len(cases))
	}
	loaded := 0
	for _, tc := range cases {
		t.Run(tc.Name, func(t *testing.T) {
			if tc.Valid == nil {
				t.Fatal("row has no `valid`")
			}
			switch tc.AM {
			case "n/a":
				if *tc.Valid {
					t.Fatal(`am: "n/a" is only for rows rejected over something Alertmanager never sees; this row is valid`)
				}
				return
			case "accept", "reject":
			default:
				t.Fatalf(`am must be "accept", "reject" or "n/a", got %q`, tc.AM)
			}
			key, body := amReceiverBody(t, tc.Receiver)
			doc := map[string]any{
				"route":     map[string]any{"receiver": "r"},
				"receivers": []any{map[string]any{"name": "r", key: []any{body}}},
			}
			raw, err := yaml.Marshal(doc)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			_, loadErr := config.Load(string(raw))
			loaded++
			got := "accept"
			if loadErr != nil {
				got = "reject"
			}
			if got != tc.AM {
				t.Errorf("Alertmanager says %s (err: %v), table says am: %q\n%s", got, loadErr, tc.AM, raw)
			}
			if got == "reject" && *tc.Valid {
				t.Errorf("Alertmanager rejects this receiver (%v) but the table has valid: true — the pipeline would write a config Alertmanager cannot reload", loadErr)
			}
		})
	}
	if loaded == 0 {
		t.Fatal("no row was loaded through Alertmanager")
	}
}
