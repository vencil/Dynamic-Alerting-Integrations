package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
)

// #3234, #3235: every `_custom_alerts` entry the exporter does not serve is a
// da-guard error — value_not_served (a refused `threshold:`, #3209),
// custom_alert_duplicate_series, or custom_alert_rejected for any other
// rejection (a bad window, quantile, objective, recipe, …) — and
// served-values prints that same kind on the entry (custom_alerts[].finding).
// validate-config lists the findings (guard_errors, #3387). A served or disabled entry, and a switched-off
// one the exporter would reject switched back on (a valid opt-out it counts
// no error for), are neither.
func TestGuard_EveryUnservedCustomAlertIsAFinding(t *testing.T) {
	t.Parallel()
	const thr = "recipe: threshold, metric: qd, op: \">\", name: q_high"
	slo := func(objective string) string {
		return "{recipe: slo_burn_rate, name: avail, metric: err_total, denominator_metric: req_total, objective: \"" + objective + "\"}"
	}
	p99 := func(quantile string) string {
		return "{recipe: p99_latency, name: lat, metric: req_seconds_bucket, quantile: \"" + quantile + "\", threshold: \"0.5\", window: 5m}"
	}
	rejected := findingkind.FindingCustomAlertRejected.String()
	for name, tc := range map[string]struct {
		entries []string
		want    []string // per entry: the finding's kind, "" = none
	}{
		"served":   {[]string{"{" + thr + ", window: 5m, threshold: \"80\"}"}, []string{""}},
		"disabled": {[]string{"{" + thr + ", window: 5m, threshold: disable}"}, []string{""}},
		// rejected only switched back on: the exporter's valid opt-out.
		"switched off, bad": {[]string{"{recipe: slo_burn_rate, name: soffbad, metric: e3, denominator_metric: r3, objective: disable, mode: loud}"}, []string{""}},
		// a bad window is rejected before the switch is read.
		"switched off, bad window": {[]string{"{" + thr + ", window: 5x, threshold: disable}"}, []string{rejected}},
		"refused threshold":        {[]string{"{" + thr + ", window: 5m, threshold: abc}"}, []string{findingkind.FindingValueNotServed.String()}},
		"bad window":               {[]string{"{" + thr + ", window: 5x, threshold: \"80\"}"}, []string{rejected}},
		"bad for":                  {[]string{"{" + thr + ", window: 5m, for: 5x, threshold: \"80\"}"}, []string{rejected}},
		"no name":                  {[]string{"{recipe: threshold, metric: qd, op: \">\", window: 5m, threshold: \"80\"}"}, []string{rejected}},
		"unknown recipe":           {[]string{"{recipe: nope, metric: qd, name: q, window: 5m, threshold: \"80\"}"}, []string{rejected}},
		"quantile 1.5":             {[]string{p99("1.5")}, []string{rejected}},
		"quantile served":          {[]string{p99("0.99")}, []string{""}},
		"objective 100":            {[]string{slo("100")}, []string{rejected}},
		"objective served":         {[]string{slo("99.9")}, []string{""}},
		"duplicate of served":      {[]string{"{" + thr + ", window: 5m, threshold: \"80\"}", "{recipe: threshold, metric: qd, op: \">\", name: q_again, window: 5m, threshold: \"80\"}"}, []string{"", findingkind.FindingCustomAlertDuplicateSeries.String()}},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{
				"_defaults.yaml": spellingRoot,
				"tx.yaml":        "tenants:\n  tx:\n    _custom_alerts:\n      - " + strings.Join(tc.entries, "\n      - ") + "\n",
			}
			code, fs := guardFindingsOf(t, files)
			got := make([]string, len(tc.entries))
			for _, f := range fs {
				var i int
				if f.TenantID != "tx" {
					continue
				}
				if _, err := fmt.Sscanf(f.Field, "_custom_alerts[%d]", &i); err != nil || i >= len(got) {
					continue
				}
				if got[i] != "" {
					t.Errorf("two findings on _custom_alerts[%d]: %s and %s", i, got[i], f.Kind)
				}
				if f.Severity != guard.SeverityError {
					t.Errorf("_custom_alerts[%d]: severity %s, want error", i, f.Severity)
				}
				got[i] = f.Kind
			}
			if !reflect.DeepEqual(got, tc.want) {
				t.Errorf("da-guard findings per entry %q, want %q (all %+v)", got, tc.want, fs)
			}
			wantCode := exitOK
			for _, k := range tc.want {
				if k != "" {
					wantCode = exitFindings
				}
			}
			if code != wantCode {
				t.Errorf("exit = %d, want %d", code, wantCode)
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
			ca := doc.Tenants["tx"].CustomAlerts
			printed := make([]string, len(ca))
			for i, e := range ca {
				if e.Finding != nil {
					printed[i] = *e.Finding
				}
			}
			// The key is always there (null for none): a reader tells a
			// da-guard older than the field from "no finding".
			var raw struct {
				Tenants map[string]struct {
					CustomAlerts []map[string]json.RawMessage `json:"custom_alerts"`
				} `json:"tenants"`
			}
			if err := json.Unmarshal(out.Bytes(), &raw); err != nil {
				t.Fatal(err)
			}
			for i, e := range raw.Tenants["tx"].CustomAlerts {
				if _, ok := e["finding"]; !ok {
					t.Errorf("custom_alerts[%d] has no finding key: %v", i, e)
				}
			}
			if !reflect.DeepEqual(printed, got) {
				t.Errorf("served-values custom_alerts[].finding %q, da-guard reports %q", printed, got)
			}
		})
	}
}

// #3235: a `_custom_alerts` block that does not decode serves nothing — one
// custom_alert_rejected on Field `_custom_alerts`, exit 1 — and served-values
// prints that kind as custom_alerts_finding (null for a block that decodes).
func TestGuard_ACustomAlertsBlockThatDoesNotDecodeIsAFinding(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		block string
		want  string
	}{
		"not a list":  {"    _custom_alerts: 'not: [a list'\n", findingkind.FindingCustomAlertRejected.String()},
		"a bad field": {"    _custom_alerts:\n      - {recipe: threshold, metric: qd, op: \">\", name: [a, b], window: 5m, threshold: \"80\"}\n", findingkind.FindingCustomAlertRejected.String()},
		"decodes":     {"    _custom_alerts:\n      - {recipe: threshold, metric: qd, op: \">\", name: q, window: 5m, threshold: \"80\"}\n", ""},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{"_defaults.yaml": spellingRoot, "tx.yaml": "tenants:\n  tx:\n" + tc.block}
			code, fs := guardFindingsOf(t, files)
			got := ""
			for _, f := range fs {
				if f.TenantID == "tx" && f.Field == "_custom_alerts" {
					got = f.Kind
				}
			}
			if got != tc.want || (tc.want != "") != (code == exitFindings) {
				t.Errorf("finding on _custom_alerts %q exit %d, want %q (all %+v)", got, code, tc.want, fs)
			}
			var out, errOut bytes.Buffer
			if c := runServedValues([]string{"--config-dir", writeTree(t, files)}, &out, &errOut); c != exitOK {
				t.Fatalf("served-values exit %d: %s", c, errOut.String())
			}
			var doc struct {
				Tenants map[string]map[string]json.RawMessage `json:"tenants"`
			}
			if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
				t.Fatal(err)
			}
			raw, ok := doc.Tenants["tx"]["custom_alerts_finding"]
			if !ok {
				t.Fatalf("no custom_alerts_finding key")
			}
			var printed *string
			if err := json.Unmarshal(raw, &printed); err != nil {
				t.Fatal(err)
			}
			if (printed == nil && tc.want != "") || (printed != nil && *printed != tc.want) {
				t.Errorf("custom_alerts_finding %s, want %q", raw, tc.want)
			}
		})
	}
}
