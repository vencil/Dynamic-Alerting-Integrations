package main

import (
	"bytes"
	"encoding/json"
	"testing"
)

// #3042: served-values lists every `_custom_alerts` entry of each tenant
// with what the exporter does with it — the custom-alert compiler's input —
// including entries the per-tenant metric limit truncates from Values.
func TestServedValues_CustomAlerts(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{
		"_defaults.yaml": "max_metrics_per_tenant: 1\ndefaults:\n  mysql_connections: 80\n",
		"tx.yaml": "tenants:\n  tx:\n    _custom_alerts:\n" +
			"      - {recipe: threshold, name: a, metric: m1, op: \">\", window: 5m, threshold: \"80:critical\", selectors: {flag: true}}\n" +
			"      - {recipe: threshold, name: b, metric: m2, op: \">\", window: 5m, threshold: disable}\n" +
			"      - {recipe: threshold, name: c, metric: m3, op: \">\", window: 5m, threshold: \"1\"}\n",
		"ty.yaml": "tenants:\n  ty:\n    _custom_alerts: \"not: [a list\"\n",
	})
	var out, errOut bytes.Buffer
	if code := runServedValues([]string{"--config-dir", root}, &out, &errOut); code != exitOK {
		t.Fatalf("served-values exit %d: %s", code, errOut.String())
	}
	var doc struct {
		Tenants map[string]struct {
			Values            map[string]any      `json:"values"`
			CustomAlerts      []servedCustomAlert `json:"custom_alerts"`
			CustomAlertsError string              `json:"custom_alerts_error"`
		} `json:"tenants"`
	}
	if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	tx := doc.Tenants["tx"]
	if len(tx.CustomAlerts) != 3 {
		t.Fatalf("tx custom_alerts = %+v", tx.CustomAlerts)
	}
	for i, want := range []string{"served", "disabled", "served"} {
		if e := tx.CustomAlerts[i]; e.Index != i || e.Status != want || e.RecipeID == "" {
			t.Errorf("entry %d = %+v, want %s", i, e, want)
		}
	}
	if sel, _ := tx.CustomAlerts[0].Spec["selectors"].(map[string]any); sel["flag"] != "true" {
		t.Errorf("spec selectors = %#v, want the decoded string \"true\"", tx.CustomAlerts[0].Spec["selectors"])
	}
	if rows, _ := tx.Values["_custom_alerts"].([]any); len(rows) >= 2 {
		t.Errorf("the metric limit of 1 should truncate the served rows, got %d", len(rows))
	}
	ty := doc.Tenants["ty"]
	if ty.CustomAlerts == nil || len(ty.CustomAlerts) != 0 || ty.CustomAlertsError == "" {
		t.Errorf("ty: custom_alerts %v, error %q", ty.CustomAlerts, ty.CustomAlertsError)
	}
}
