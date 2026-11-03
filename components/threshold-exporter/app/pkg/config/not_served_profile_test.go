package config

// #3395: profile_not_filled — a key the tenant's profile writes that the
// profile may not fill in, because the root declares it without a platform
// value (#1189, profileFill's onDeclared). The effective config names it in
// NotServed with the profile file and does not show it; it is not one of
// the reasons da-guard's value_not_served and the exporter's
// da_config_values_not_served select
// (TestValueNotServedAsWritten_NotProfileNotFilled).

import (
	"bytes"
	"log"
	"reflect"
	"strings"
	"testing"
)

var profileNotFilledCases = []struct {
	name  string
	files map[string]string
	want  map[string]NotServedKey
}{
	{name: "declared without value, filled by the profile only: named", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides: [kafka_lag]\n",
		"_profiles.yaml": "profiles:\n  gold:\n    kafka_lag: \"7\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n",
	}, want: map[string]NotServedKey{"kafka_lag": {Reason: NotServedProfileNotFilled, File: "_profiles.yaml"}}},
	{name: "the tenant writes the key itself: not named", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides: [kafka_lag]\n",
		"_profiles.yaml": "profiles:\n  gold:\n    kafka_lag: \"7\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n    kafka_lag: \"5\"\n",
	}},
	{name: "a platform tenants: entry writes the key: not named", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides: [kafka_lag]\n",
		"_profiles.yaml": "profiles:\n  gold:\n    kafka_lag: \"7\"\n",
		"_platform.yaml": "tenants:\n  tx:\n    kafka_lag: \"5\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n",
	}},
	{name: "a valued key the profile fills: not named", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"_profiles.yaml": "profiles:\n  gold:\n    mysql_connections: \"60\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n",
	}},
	{name: "the tenant is on no profile: not named", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides: [kafka_lag]\n",
		"_profiles.yaml": "profiles:\n  gold:\n    kafka_lag: \"7\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    mysql_connections: \"70\"\n",
	}},
}

// TestNotServedProfileNotFilled_EffectiveTable: EffectiveTree and
// ResolveEffective name the key with the profile file, and the effective
// config does not show it (no plane lays that value).
func TestNotServedProfileNotFilled_EffectiveTable(t *testing.T) {
	t.Parallel()
	for _, tc := range profileNotFilledCases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			root := writeUnconsumedTree(t, tc.files)
			tree, err := EffectiveTree(root, fixedNow)
			if err != nil {
				t.Fatalf("EffectiveTree: %v", err)
			}
			var tx *EffectiveConfig
			for _, ec := range tree.Tenants {
				if ec.TenantID == "tx" {
					tx = ec
				}
			}
			if tx == nil {
				t.Fatal("tenant tx missing")
			}
			if !reflect.DeepEqual(tx.NotServed, tc.want) {
				t.Errorf("NotServed = %v, want %v (effective_config %v)", tx.NotServed, tc.want, tx.EffectiveConfig)
			}
			for k, ns := range tc.want {
				if v, shown := tx.EffectiveConfig[k]; ns.Reason == NotServedProfileNotFilled && shown {
					t.Errorf("effective_config shows %s = %v; the profile value is applied by no plane", k, v)
				}
			}
			one, err := ResolveEffective(root, "tx", fixedNow)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(one.NotServed, tx.NotServed) {
				t.Errorf("ResolveEffective NotServed %v, EffectiveTree %v", one.NotServed, tx.NotServed)
			}
		})
	}
}

// TestNotServedProfileNotFilled_IsMetricsDecision: on the same tree the
// exporter's own build (LoadDir → ApplyProfiles) WARNs about exactly the
// key the effective config names, and serves no row for it — one decision
// (profileFill), read on both planes.
func TestNotServedProfileNotFilled_IsMetricsDecision(t *testing.T) {
	t.Parallel()
	for _, tc := range profileNotFilledCases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			root := writeUnconsumedTree(t, tc.files)
			var buf bytes.Buffer
			cfg, _, err := LoadDir(root, log.New(&buf, "", 0))
			if err != nil {
				t.Fatal(err)
			}
			warned := strings.Contains(buf.String(), `declared without a platform value`)
			if want := len(tc.want) > 0; warned != want {
				t.Errorf("ApplyProfiles WARN = %v, effective names %v; log:\n%s", warned, tc.want, buf.String())
			}
			keyed, _, err := cfg.ResolveAtWithKeys(fixedNow)
			if err != nil {
				t.Fatal(err)
			}
			for _, k := range keyed {
				if _, named := tc.want[k.Key]; named && k.Tenant == "tx" {
					t.Errorf("/metrics serves %s for tx, which effective names %s", k.Key, NotServedProfileNotFilled)
				}
			}
		})
	}
}

// TestValueNotServedAsWritten_NotProfileNotFilled: like unconsumed, how
// severe a profile value the declared tier refuses is belongs to ADR-038's
// shared verdict (#3394) — not a uniform value_not_served error nor a
// da_config_values_not_served count.
func TestValueNotServedAsWritten_NotProfileNotFilled(t *testing.T) {
	t.Parallel()
	if ValueNotServedAsWritten("kafka_lag", NotServedProfileNotFilled) {
		t.Error("profile_not_filled is selected as a value not served as written")
	}
	for _, r := range ValueNotServedReasons() {
		if r == NotServedProfileNotFilled {
			t.Errorf("ValueNotServedReasons() = %v holds %q", ValueNotServedReasons(), NotServedProfileNotFilled)
		}
	}
}
