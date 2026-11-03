package config

// #3395: profile_not_filled — a key the tenant's profile writes that the
// profile may not fill in, because the root declares it without a platform
// value (#1189, profileFill's onDeclared). The effective config names it in
// NotServed with the profile file, under the canonical name (the one
// ApplyProfiles' WARN names), and does not show it; it is not one of the
// reasons da-guard's value_not_served and the exporter's
// da_config_values_not_served select
// (TestValueNotServedAsWritten_NotProfileNotFilled).

import (
	"bytes"
	"fmt"
	"log"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"testing"
)

const profileNotFilledRoot = "defaults:\n  mysql_connections: 80\noptional_overrides: [kafka_lag]\n"

var profileNotFilledCases = []struct {
	name  string
	files map[string]string
	want  map[string]NotServedKey
	// warnOnly: keys ApplyProfiles WARNs about on /metrics that the
	// effective config does not name — a key another layer gives a value,
	// which effective shows and /metrics serves (withProfileNotFilled).
	warnOnly []string
}{
	{name: "declared without value, filled by the profile only: named", files: map[string]string{
		"_defaults.yaml": profileNotFilledRoot,
		"_profiles.yaml": "profiles:\n  gold:\n    kafka_lag: \"7\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n",
	}, want: map[string]NotServedKey{"kafka_lag": {Reason: NotServedProfileNotFilled, File: "_profiles.yaml"}}},
	{name: "the tenant writes the key itself: not named", files: map[string]string{
		"_defaults.yaml": profileNotFilledRoot,
		"_profiles.yaml": "profiles:\n  gold:\n    kafka_lag: \"7\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n    kafka_lag: \"5\"\n",
	}},
	// A threshold written as null is no write (#2518): the profile fill
	// sees the key as absent, so the declared-key decision is reached.
	{name: "the tenant writes the key as null: named", files: map[string]string{
		"_defaults.yaml": profileNotFilledRoot,
		"_profiles.yaml": "profiles:\n  gold:\n    kafka_lag: \"7\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n    kafka_lag: null\n",
	}, want: map[string]NotServedKey{"kafka_lag": {Reason: NotServedProfileNotFilled, File: "_profiles.yaml"}}},
	{name: "a platform tenants: entry writes the key: not named", files: map[string]string{
		"_defaults.yaml": profileNotFilledRoot,
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
		"_defaults.yaml": profileNotFilledRoot,
		"_profiles.yaml": "profiles:\n  gold:\n    kafka_lag: \"7\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    mysql_connections: \"70\"\n",
	}},
	{name: "an unknown profile: not named", files: map[string]string{
		"_defaults.yaml": profileNotFilledRoot,
		"_profiles.yaml": "profiles:\n  gold:\n    kafka_lag: \"7\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: silver\n",
	}},
	{name: "_profile in the scheduled-value mapping form: named", files: map[string]string{
		"_defaults.yaml": profileNotFilledRoot,
		"_profiles.yaml": "profiles:\n  gold:\n    kafka_lag: \"7\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile:\n      default: gold\n",
	}, want: map[string]NotServedKey{"kafka_lag": {Reason: NotServedProfileNotFilled, File: "_profiles.yaml"}}},
	// The key is the canonical name, as the WARN names it — not the
	// profile's spelling (profileFill runs over canonicalView).
	{name: "a #1231 retired spelling: named under the canonical name", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides: [mysql_threads_running]\n",
		"_profiles.yaml": "profiles:\n  gold:\n    mysql_cpu: \"7\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n",
	}, want: map[string]NotServedKey{"mysql_threads_running": {Reason: NotServedProfileNotFilled, File: "_profiles.yaml"}}},
	{name: "a dimensional key on a declared base: named in canonical label form", files: map[string]string{
		"_defaults.yaml": profileNotFilledRoot,
		"_profiles.yaml": "profiles:\n  gold:\n    'kafka_lag{ topic = ''a'' , zone=\"b\"}': \"7\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n",
	}, want: map[string]NotServedKey{`kafka_lag{topic="a", zone="b"}`: {Reason: NotServedProfileNotFilled, File: "_profiles.yaml"}}},
	// profileFill does not block `_critical` (resolveCriticalRows admits on
	// defaults[base]): the profile fills it in, and with no root value for
	// the base no row reads it — the existing unconsumed, not this reason.
	{name: "a _critical of a declared base: filled, unconsumed", files: map[string]string{
		"_defaults.yaml": profileNotFilledRoot,
		"_profiles.yaml": "profiles:\n  gold:\n    kafka_lag_critical: \"9\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n",
	}, want: map[string]NotServedKey{"kafka_lag_critical": {Reason: NotServedUnconsumed, File: "_profiles.yaml"}}},
	// A subtree level values the key: effective shows and /metrics serves
	// the chain's value. The profile's value is still discarded — the WARN
	// is the only trace, and the only shape where the two planes differ in
	// whether the decision is reported.
	{name: "a subtree _defaults.yaml values the key: not named, WARN only", files: map[string]string{
		"_defaults.yaml":      profileNotFilledRoot,
		"_profiles.yaml":      "profiles:\n  gold:\n    kafka_lag: \"7\"\n",
		"team/_defaults.yaml": "defaults:\n  kafka_lag: 5\n",
		"team/tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n",
	}, warnOnly: []string{"kafka_lag"}},
}

func effectiveTx(t *testing.T, root string) *EffectiveConfig {
	t.Helper()
	tree, err := EffectiveTree(root, fixedNow)
	if err != nil {
		t.Fatalf("EffectiveTree: %v", err)
	}
	for _, ec := range tree.Tenants {
		if ec.TenantID == "tx" {
			return ec
		}
	}
	t.Fatal("tenant tx missing")
	return nil
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
			tx := effectiveTx(t, root)
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

// TestNotServedProfileNotFilled_SubtreeValueIsShownAndServed: where a
// subtree level values the key, effective shows the chain's value and
// /metrics serves it — the profile's is the one discarded.
func TestNotServedProfileNotFilled_SubtreeValueIsShownAndServed(t *testing.T) {
	t.Parallel()
	tc := profileNotFilledCases[len(profileNotFilledCases)-1]
	if len(tc.warnOnly) == 0 {
		t.Fatalf("case %q is not the subtree shape", tc.name)
	}
	root := writeUnconsumedTree(t, tc.files)
	tx := effectiveTx(t, root)
	if got, shown := tx.EffectiveConfig["kafka_lag"]; !shown || fmt.Sprint(got) != "5" {
		t.Errorf("effective_config kafka_lag = %#v, want the subtree's 5", got)
	}
	cfg, _, err := LoadDir(root, log.New(&bytes.Buffer{}, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	if got := cfg.Tenants["tx"]["kafka_lag"].Default; got != "5" {
		t.Errorf("/metrics build: tx kafka_lag = %q, want the subtree's 5", got)
	}
}

// warnedProfileKey is the key of ApplyProfiles' declared-key WARN.
var warnedProfileKey = regexp.MustCompile(`supplies ("(?:[^"\\]|\\.)*"), but that key is declared without a platform value`)

// TestNotServedProfileNotFilled_IsMetricsDecision: on the same tree the
// exporter's own build (LoadDir → ApplyProfiles) WARNs about exactly the
// keys the effective config names profile_not_filled (plus warnOnly), and
// serves no row for them — one decision (profileFill), read on both planes.
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
			var warned []string
			for _, m := range warnedProfileKey.FindAllStringSubmatch(buf.String(), -1) {
				k, err := strconv.Unquote(m[1])
				if err != nil {
					t.Fatalf("WARN key %s: %v", m[1], err)
				}
				warned = append(warned, k)
			}
			want := append([]string(nil), tc.warnOnly...)
			for k, ns := range tc.want {
				if ns.Reason == NotServedProfileNotFilled {
					want = append(want, k)
				}
			}
			sort.Strings(warned)
			sort.Strings(want)
			if !reflect.DeepEqual(warned, want) && (len(warned) > 0 || len(want) > 0) {
				t.Errorf("ApplyProfiles WARNs %q, want %q; log:\n%s", warned, want, buf.String())
			}
			keyed, _, err := cfg.ResolveAtWithKeys(fixedNow)
			if err != nil {
				t.Fatal(err)
			}
			for _, k := range keyed {
				if ns, named := tc.want[k.Key]; named && ns.Reason == NotServedProfileNotFilled && k.Tenant == "tx" {
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
