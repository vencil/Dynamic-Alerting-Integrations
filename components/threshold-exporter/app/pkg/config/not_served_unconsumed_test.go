package config

// #3378: unconsumed — a threshold key of the tenant's built map that no row
// phase of the /metrics resolver reads: the RECORD (the resolver's table,
// ValuesNotServed, and the effective config's NotServed), and that it is not
// one of the reasons da-guard's value_not_served and the exporter's
// da_config_values_not_served select (TestValueNotServedAsWritten_NotUnconsumed).
// A subtree `x_critical` whose base has no root value is not here: the
// overlay does not deliver it (undeliverable; subtree_critical_gate_test.go).

import (
	"io"
	"log"
	"path/filepath"
	"reflect"
	"sort"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// unconsumedCases are trees whose tenant tx is or is not named unconsumed,
// with the effective config's NotServed for tx (only the entries the case is
// about: want lists every key of tx's NotServed).
var unconsumedCases = []struct {
	name  string
	files map[string]string
	want  map[string]NotServedKey
}{
	{name: "tenant flat key the root declares nowhere", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml":        "tenants:\n  tx:\n    redis_evicted_keys: \"7\"\n",
	}, want: map[string]NotServedKey{"redis_evicted_keys": {Reason: NotServedUnconsumed, File: "tx.yaml"}}},
	{name: "profile key the root declares nowhere: the profile's file", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"_profiles.yaml": "profiles:\n  gold:\n    redis_evicted_keys: \"7\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: gold\n",
	}, want: map[string]NotServedKey{"redis_evicted_keys": {Reason: NotServedUnconsumed, File: "_profiles.yaml"}}},
	{name: "platform tenants: entry key the root declares nowhere", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"_platform.yaml": "tenants:\n  tx:\n    redis_evicted_keys: \"7\"\n",
		"tx.yaml":        "tenants:\n  tx: {}\n",
	}, want: map[string]NotServedKey{"redis_evicted_keys": {Reason: NotServedUnconsumed, File: "_platform.yaml"}}},
	{name: "tenant _critical with no base anywhere", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml":        "tenants:\n  tx:\n    redis_evicted_keys_critical: \"9\"\n",
	}, want: map[string]NotServedKey{"redis_evicted_keys_critical": {Reason: NotServedUnconsumed, File: "tx.yaml"}}},
	{name: "a _critical declared in optional_overrides, its base not", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides: [redis_queue_length_critical]\n",
		"tx.yaml":        "tenants:\n  tx:\n    redis_queue_length_critical: \"9\"\n",
	}, want: map[string]NotServedKey{"redis_queue_length_critical": {Reason: NotServedUnconsumed, File: "tx.yaml"}}},
	{name: "a _critical whose base has a value: served", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml":        "tenants:\n  tx:\n    mysql_connections_critical: \"95\"\n",
	}},
	{name: "a _critical key the root values itself: resolveBaseRows reads it", files: map[string]string{
		"_defaults.yaml": "defaults:\n  redis_queue_length_critical: 9\n",
		"tx.yaml":        "tenants:\n  tx:\n    redis_queue_length_critical: \"5\"\n",
	}},
	// ADR-038 §11: a dimensional key needs its base named, as a flat key does.
	{name: "dimensional key, base undeclared: unconsumed", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml":        "tenants:\n  tx:\n    'redis_evicted_keys{env=\"prod\"}': \"7\"\n",
	}, want: map[string]NotServedKey{`redis_evicted_keys{env="prod"}`: {Reason: NotServedUnconsumed, File: "tx.yaml"}}},
	{name: "dimensional key, base declared: served", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides:\n  - redis_evicted_keys\n",
		"tx.yaml":        "tenants:\n  tx:\n    'redis_evicted_keys{env=\"prod\"}': \"7\"\n",
	}},
	{name: "dimensional key, base valued: served", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml":        "tenants:\n  tx:\n    'mysql_connections{db=\"a\"}': \"7\"\n",
	}},
	{name: "undeclared key switched off", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml":        "tenants:\n  tx:\n    redis_evicted_keys: disable\n    redis_evicted_keys_critical: disable\n",
	}},
	{name: "undeclared key switched off in every window", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml": "tenants:\n  tx:\n    redis_evicted_keys:\n      default: disable\n      overrides:\n" +
			"        - window: \"01:00-09:00\"\n          value: disable\n",
	}},
	{name: "undeclared key switched off in some windows only", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml": "tenants:\n  tx:\n    redis_evicted_keys:\n      default: disable\n      overrides:\n" +
			"        - window: \"01:00-09:00\"\n          value: \"7\"\n",
	}, want: map[string]NotServedKey{"redis_evicted_keys": {Reason: NotServedUnconsumed, File: "tx.yaml"}}},
	{name: "a _ key is unregistered, not unconsumed", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml":        "tenants:\n  tx:\n    _myth: \"7\"\n",
	}, want: map[string]NotServedKey{"_myth": {Reason: NotServedUnregistered, File: "tx.yaml"}}},
	{name: "declared in optional_overrides, with a value: served", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides: [redis_evicted_keys]\n",
		"tx.yaml":        "tenants:\n  tx:\n    redis_evicted_keys: \"7\"\n",
	}},
	{name: "valued at the root, tenant value expired: read (the default is served)", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml": "tenants:\n  tx:\n    mysql_connections:\n      default: \"70\"\n" +
			"      expires: \"2020-01-01T00:00:00Z\"\n",
	}},
	// The root's null is the cause, with a finding of its own
	// (root_default_null_undeclared): root_null_undeclared wins.
	{name: "root null: root_null_undeclared, not unconsumed", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_evicted_keys: null\n",
		"tx.yaml":        "tenants:\n  tx:\n    redis_evicted_keys: \"7\"\n",
	}, want: map[string]NotServedKey{"redis_evicted_keys": {Reason: NotServedRootNullUndeclared, File: "tx.yaml"}}},
	{name: "root null base of a _critical: root_null_undeclared", files: map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: null\n",
		"tx.yaml":        "tenants:\n  tx:\n    mysql_connections_critical: \"95\"\n",
	}, want: map[string]NotServedKey{"mysql_connections_critical": {Reason: NotServedRootNullUndeclared, File: "tx.yaml"}}},
}

// TestNotServedUnconsumed_EffectiveTable: the effective config names each
// shape as above, EffectiveTree and ResolveEffective alike.
func TestNotServedUnconsumed_EffectiveTable(t *testing.T) {
	t.Parallel()
	for _, tc := range unconsumedCases {
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

// TestNotServedUnconsumed_IsWhatMetricsDoesNotServe measures each verdict on
// the output: over the exporter's own build of the tree (LoadDir) resolved
// with keys at every minute of the day a schedule changes (the rows
// /metrics serves, ResolveAtWithKeys), a key the resolver records
// unconsumed is served at no minute, and every key of tx's built map that is
// not recorded and not switched off everywhere is served at some minute.
func TestNotServedUnconsumed_IsWhatMetricsDoesNotServe(t *testing.T) {
	t.Parallel()
	for _, tc := range unconsumedCases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			root := writeUnconsumedTree(t, tc.files)
			cfg, _, err := LoadDir(root, log.New(io.Discard, "", 0))
			if err != nil {
				t.Fatal(err)
			}
			served := map[string]bool{}
			read := func(c *ThresholdConfig) {
				keyed, _, err := c.ResolveAtWithKeys(fixedNow)
				if err != nil {
					t.Fatal(err)
				}
				for _, k := range keyed {
					if k.Tenant == "tx" {
						served[k.Key] = true
					}
				}
			}
			read(cfg)
			for _, m := range sortedCuts(addScheduleCuts(nil, cfg.Tenants["tx"])) {
				read(cfg.AtMinuteOfDay(m))
			}
			verdicts := cfg.ValuesNotServed(fixedNow)["tx"]
			canonTx, _ := canonicalizeOverrides(cfg.Tenants["tx"])
			for k, sv := range canonTx {
				if baseRowsSkipKey(k) {
					continue
				}
				switch {
				case verdicts[k] == NotServedUnconsumed && served[k]:
					t.Errorf("%s: recorded unconsumed, but /metrics serves it", k)
				case verdicts[k] == "" && !served[k] && !disabledEverywhere(sv) && !isThresholdExpired(sv, fixedNow):
					t.Errorf("%s: /metrics serves it at no minute, and nothing is recorded (served %v)", k, served)
				}
			}
		})
	}
}

// TestValuesNotServed_UnconsumedTable reads the resolver's table directly:
// the whole UTC day, and nothing for a clean tenant.
func TestValuesNotServed_UnconsumedTable(t *testing.T) {
	t.Parallel()
	sched := func(def, win string) ScheduledValue {
		return ScheduledValue{Default: def, Overrides: []TimeWindowOverride{{Window: "01:00-09:00", Value: win}}}
	}
	cfg := &ThresholdConfig{
		Defaults:          map[string]float64{"mysql_connections": 80},
		OptionalOverrides: []string{"redis_declared"},
		Tenants: map[string]map[string]ScheduledValue{
			"tx": {
				"redis_evicted_keys":          {Default: "7"},
				"redis_evicted_keys_critical": {Default: "9"},
				"redis_off_now":               sched("disable", "7"), // served in a window only
				"redis_on_now":                sched("7", "disable"),
				"redis_off_all_day":           sched("disable", "disable"),
				"redis_declared":              {Default: "7"},
				"mysql_connections":           {Default: "70"},
				"mysql_connections_critical":  {Default: "95"},
				`redis_dim{env="prod"}`:       {Default: "7"},
				"_myth":                       {Default: "7"},
			},
			"ty": {"mysql_connections": {Default: "70"}},
		},
	}
	want := map[string]map[string]string{"tx": {
		"redis_evicted_keys":          NotServedUnconsumed,
		"redis_evicted_keys_critical": NotServedUnconsumed,
		"redis_off_now":               NotServedUnconsumed,
		"redis_on_now":                NotServedUnconsumed,
		`redis_dim{env="prod"}`:       NotServedUnconsumed, // ADR-038 §11: base not named
	}}
	for _, at := range []int{0, 5 * 60, 12 * 60} { // outside, inside, outside the window
		now := fixedNow.Truncate(24 * time.Hour).Add(time.Duration(at) * time.Minute)
		if got := cfg.ValuesNotServed(now); !reflect.DeepEqual(got, want) {
			t.Errorf("at minute %d: %v, want %v", at, got, want)
		}
	}
	if got := cfg.TenantValuesNotServed("tx", fixedNow); !reflect.DeepEqual(got, want["tx"]) {
		t.Errorf("TenantValuesNotServed(tx) = %v, want %v", got, want["tx"])
	}
}

// TestRejectRecorder_ClaimsChangeNoRow: the claims ride the same resolve the
// scrape runs with a nil recorder; with one, the rows are the same and no
// claim is left over for the next tenant.
func TestRejectRecorder_ClaimsChangeNoRow(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 30},
		Tenants: map[string]map[string]ScheduledValue{
			"ta": {"mysql_connections": {Default: "40"}, "redis_x": {Default: "1"}},
			"tb": {"redis_x": {Default: "1"}, `redis_x{a="b"}`: {Default: "1"}},
		},
	}
	plain, _ := cfg.resolveAtWithStats(fixedNow, nil, nil, nil)
	rec := &rejectRecorder{}
	recorded, _ := cfg.resolveAtWithStats(fixedNow, nil, nil, rec)
	sortRows := func(rs []ResolvedThreshold) {
		sort.Slice(rs, func(i, j int) bool {
			return truncationSortKey(rs[i])+rs[i].Tenant < truncationSortKey(rs[j])+rs[j].Tenant
		})
	}
	sortRows(plain)
	sortRows(recorded)
	if !reflect.DeepEqual(plain, recorded) {
		t.Errorf("rows differ with a recorder:\n%v\n%v", plain, recorded)
	}
	want := map[string]map[string]string{"ta": {"redis_x": NotServedUnconsumed},
		"tb": {"redis_x": NotServedUnconsumed, `redis_x{a="b"}`: NotServedUnconsumed}}
	if !reflect.DeepEqual(rec.byTenant, want) {
		t.Errorf("recorded %v, want %v", rec.byTenant, want)
	}
	if len(rec.claimed) != 0 {
		t.Errorf("claims left after the resolve: %v", rec.claimed)
	}
}

func writeUnconsumedTree(t *testing.T, files map[string]string) string {
	t.Helper()
	tmp := t.TempDir()
	tree := map[string]string{}
	for k, v := range files {
		tree[filepath.Join("conf.d", k)] = v
	}
	testutil.WriteTree(t, tmp, tree)
	return filepath.Join(tmp, "conf.d")
}

// TestValueNotServedAsWritten_NotUnconsumed (#3378, owner ruling): unconsumed
// is named in not_served only. How severe a key nobody reads is, is ADR-038's
// shared verdict (an error in a tenant file, a notice in a profile or a
// platform file), so neither da-guard's value_not_served nor the exporter's
// da_config_values_not_served selects it.
func TestValueNotServedAsWritten_NotUnconsumed(t *testing.T) {
	t.Parallel()
	if ValueNotServedAsWritten("redis_evicted_keys", NotServedUnconsumed) {
		t.Error("unconsumed is selected as a value not served as written")
	}
	for _, r := range ValueNotServedReasons() {
		if r == NotServedUnconsumed {
			t.Errorf("ValueNotServedReasons() = %v holds %q", ValueNotServedReasons(), NotServedUnconsumed)
		}
	}
}

// ADR-038 §11: the root `defaults:` writing a dimensional key names it, even
// when its base is neither valued nor declared — a tenant's override of that
// key is served (it replaces the root's row), not unconsumed.
func TestDimensionalKey_RootDefaultNamesIt(t *testing.T) {
	t.Parallel()
	const key = `redis_x{env="a"}`
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{key: 5},
		Tenants: map[string]map[string]ScheduledValue{
			"tx": {key: {Default: "7"}},
			"ty": {},
		},
	}
	got := map[string]float64{}
	for _, r := range cfg.ResolveAt(fixedNow) {
		if r.Metric == "x" && r.CustomLabels["env"] == "a" {
			got[r.Tenant] = r.Value
		}
	}
	if want := map[string]float64{"tx": 7, "ty": 5}; !reflect.DeepEqual(got, want) {
		t.Errorf("rows %v, want %v", got, want)
	}
	if ns := cfg.ValuesNotServed(fixedNow); len(ns) != 0 {
		t.Errorf("not served: %v", ns)
	}
}
