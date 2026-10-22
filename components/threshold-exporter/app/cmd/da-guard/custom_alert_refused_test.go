package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
)

// #3209: a `_custom_alerts` entry whose `threshold:` the exporter refuses
// (`abc`, `NaN`, `-Inf:critical`, `70:high`) is the same refusal as a
// threshold row's, so it is the same finding — value_not_served, error —
// on Field `_custom_alerts[<index>]`, naming the entry, the value and the
// exporter's reason; exit 1. served-values names the refusal in
// custom_alerts[].refused. A served or switched-off entry, and one rejected
// for another field, are not this finding.
func TestGuard_CustomAlertThresholdRefused(t *testing.T) {
	t.Parallel()
	const thr = "recipe: threshold, metric: qd, op: \">\", window: 5m, name: q_high"
	// The threshold row refusing the same value: the finding a refused custom
	// alert must match in kind and severity.
	_, rowFs := guardFindingsOf(t, map[string]string{
		"_defaults.yaml": spellingRoot,
		"tx.yaml":        "tenants:\n  tx:\n    pg_connections: \"NaN\"\n",
	})
	var row *reportedFinding
	for i := range rowFs {
		if rowFs[i].TenantID == "tx" && rowFs[i].Field == "pg_connections" {
			row = &rowFs[i]
		}
	}
	if row == nil || row.Kind != findingkind.FindingValueNotServed.String() || row.Severity != guard.SeverityError {
		t.Fatalf("threshold row NaN: finding %+v (all %+v)", row, rowFs)
	}
	for name, tc := range map[string]struct {
		entry   string
		refused string // "" = no refusal: no value_not_served on the entry
	}{
		"abc":               {"{" + thr + ", threshold: abc}", "unparsed"},
		"NaN":               {"{" + thr + ", threshold: \"NaN\"}", "non_finite"},
		"-Inf:critical":     {"{" + thr + ", threshold: \"-Inf:critical\"}", "non_finite"},
		"70:high":           {"{" + thr + ", threshold: \"70:high\"}", "unparsed"},
		"control: served":   {"{" + thr + ", threshold: \"80:critical\"}", ""},
		"control: disabled": {"{" + thr + ", threshold: disable}", ""},
		"control: bad window, finite threshold": {
			"{recipe: threshold, metric: qd, op: \">\", window: 5x, name: q_high, threshold: \"80\"}", ""},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{
				"_defaults.yaml": spellingRoot,
				"tx.yaml":        "tenants:\n  tx:\n    _custom_alerts:\n      - " + tc.entry + "\n",
			}
			code, fs := guardFindingsOf(t, files)
			var got []reportedFinding
			for _, f := range fs {
				if f.TenantID == "tx" && strings.HasPrefix(f.Field, "_custom_alerts") && f.Kind == row.Kind {
					got = append(got, f)
				}
			}
			if tc.refused == "" {
				if len(got) != 0 {
					t.Errorf("value_not_served on a custom alert the exporter does not refuse a threshold of: %+v", got)
				}
			} else {
				if len(got) != 1 || got[0].Field != "_custom_alerts[0]" || got[0].Severity != row.Severity {
					t.Fatalf("findings %+v, want one %s %s on _custom_alerts[0] (all %+v)", got, row.Severity, row.Kind, fs)
				}
				for _, want := range []string{"value_unparsed_dropped:", `"q_high"`, tc.refused + ":"} {
					if !strings.Contains(got[0].Message, want) {
						t.Errorf("message %q does not contain %q", got[0].Message, want)
					}
				}
				if code != exitFindings {
					t.Errorf("exit = %d, want %d", code, exitFindings)
				}
			}

			var out, errOut bytes.Buffer
			if c := runServedValues([]string{"--config-dir", writeTree(t, files)}, &out, &errOut); c != exitOK {
				t.Fatalf("served-values exit %d: %s", c, errOut.String())
			}
			var doc struct {
				Tenants map[string]struct {
					CustomAlerts []servedCustomAlert `json:"custom_alerts"`
				} `json:"tenants"`
			}
			if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
				t.Fatal(err)
			}
			if ca := doc.Tenants["tx"].CustomAlerts; len(ca) != 1 || ca[0].Refused != tc.refused {
				t.Errorf("served-values custom_alerts %+v, want refused %q", ca, tc.refused)
			}
		})
	}
}
