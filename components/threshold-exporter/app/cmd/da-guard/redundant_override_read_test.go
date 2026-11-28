package main

import (
	"reflect"
	"slices"
	"testing"
)

// #4288: redundant_override says "remove the override and rely on
// inheritance", so it must fire exactly when removing the override leaves
// what /metrics serves unchanged — its values and severities, as
// `da-guard served-values` reports them, with what it does not serve, drops
// or schedules — and the tenant's value is one the
// exporter reads: a refused one (`"0x50"`) changes nothing when removed only
// because it was never served, and value_not_served already says so. Each case writes the key in the
// defaults chain (root or subtree) and in the tenant's own file, in the
// spellings conf.d trees use: the root `defaults:` takes numbers, the tenant
// file quotes them as the schema asks.
func TestGuard_RedundantOverrideFiresExactlyWhenServedIsUnchanged(t *testing.T) {
	t.Parallel()
	const key = "mysql_connection_usage_percent"
	type tc struct{ inherited, tenant string }
	var cases []tc
	for _, tenant := range []string{
		`80`, `"80"`, `"80.0"`, `"8e1"`, `"80:warning"`, `"80:critical"`, `"81"`,
		`"disable"`, `" 80"`, `"080"`, `"0x50"`, `"80.5"`,
	} {
		cases = append(cases, tc{"80", tenant})
	}
	cases = append(cases, tc{"80.5", `"80.50"`}, tc{"80.5", `"80.5"`}, tc{`"80"`, `80`},
		// the same text the exporter refuses on both sides: not redundant
		tc{`"0x50"`, `"0x50"`}, tc{`"NaN"`, `"NaN"`}, tc{`"abc"`, `"abc"`})

	fired := 0
	for _, c := range cases {
		for _, layer := range []string{"root", "subtree"} {
			with, without := map[string]string{}, map[string]string{}
			if layer == "root" {
				with["_defaults.yaml"] = "defaults:\n  " + key + ": " + c.inherited + "\n"
			} else {
				with["_defaults.yaml"] = "defaults:\n  " + key + ": 70\n"
				with["a/_defaults.yaml"] = "defaults:\n  " + key + ": " + c.inherited + "\n"
			}
			for k, v := range with {
				without[k] = v
			}
			with["a/t.yaml"] = "tenants:\n  t1:\n    " + key + ": " + c.tenant + "\n"
			without["a/t.yaml"] = "tenants:\n  t1: {}\n"

			findings := allFindings(t, "--config-dir", writeReservedConfD(t, with))
			reported := slices.Contains(findings, "warn redundant_override t1 "+key)

			code, w, _, stderr := served(t, with, "2026-10-01T00:00:00Z")
			mustOK(t, code, stderr)
			code, wo, _, stderr := served(t, without, "2026-10-01T00:00:00Z")
			mustOK(t, code, stderr)
			unchanged := reflect.DeepEqual(w.Tenants["t1"].Values, wo.Tenants["t1"].Values) &&
				reflect.DeepEqual(w.Tenants["t1"].Severities, wo.Tenants["t1"].Severities) &&
				reflect.DeepEqual(w.Tenants["t1"].Unserved, wo.Tenants["t1"].Unserved) &&
				reflect.DeepEqual(w.Tenants["t1"].Dropped, wo.Tenants["t1"].Dropped) &&
				reflect.DeepEqual(w.Tenants["t1"].Schedules, wo.Tenants["t1"].Schedules)
			refused := slices.Contains(findings, "error value_not_served t1 "+key)

			if reported != (unchanged && !refused) {
				t.Errorf("%s %s=%s, tenant %s: redundant_override reported=%v, served unchanged by removal=%v, value refused=%v (served %v/%v → %v/%v)",
					layer, key, c.inherited, c.tenant, reported, unchanged, refused,
					w.Tenants["t1"].Values[key], w.Tenants["t1"].Severities[key],
					wo.Tenants["t1"].Values[key], wo.Tenants["t1"].Severities[key])
			}
			if reported {
				fired++
			}
		}
	}
	if fired < 6 {
		t.Fatalf("redundant_override fired %d times, want >= 6 (corpus undershot)", fired)
	}
}
