package main

import (
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
)

// A `_custom_alerts` entry the exporter drops because an earlier entry
// already serves its series is refused by the gate; entries that only share
// a shape and severity are not (#2031).
func TestGuard_CustomAlertDuplicateSeries(t *testing.T) {
	t.Parallel()
	const thr = "recipe: threshold, metric: qd, op: \">\", window: 5m"
	for name, tc := range map[string]struct {
		list string
		want []string // fields
	}{
		"same name and shape": {
			"      - {" + thr + ", name: q_high, threshold: \"100:warning\"}\n" +
				"      - {" + thr + ", name: q_high, threshold: \"200:warning\"}\n",
			[]string{"_custom_alerts[1]"}},
		"slo, same shape": {
			"      - {recipe: slo_burn_rate, name: avail1, metric: err_total, denominator_metric: req_total, objective: \"99.9\"}\n" +
				"      - {recipe: slo_burn_rate, name: avail2, metric: err_total, denominator_metric: req_total, objective: \"99.5\"}\n",
			[]string{"_custom_alerts[1]"}},
		"control: another name": {
			"      - {" + thr + ", name: q_high, threshold: \"100:warning\"}\n" +
				"      - {" + thr + ", name: q_other, threshold: \"200:warning\"}\n",
			nil},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			code, fs := guardFindingsOf(t, map[string]string{
				"_defaults.yaml": spellingRoot,
				"tx.yaml":        "tenants:\n  tx:\n    _custom_alerts:\n" + tc.list,
			})
			var got []string
			for _, f := range fs {
				if f.Kind == guard.FindingCustomAlertDuplicateSeries && f.Severity == guard.SeverityError && f.TenantID == "tx" {
					got = append(got, f.Field)
				}
			}
			if strings.Join(got, ",") != strings.Join(tc.want, ",") {
				t.Errorf("custom_alert_duplicate_series on %v, want %v (all %+v)", got, tc.want, fs)
			}
			if want := map[bool]int{true: exitFindings, false: exitOK}[len(tc.want) > 0]; code != want {
				t.Errorf("exit = %d, want %d", code, want)
			}
		})
	}
}
