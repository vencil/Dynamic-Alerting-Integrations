package config

import (
	"io"
	"log"
	"os"
	"path/filepath"
	"testing"
)

// unsetOf is key → pack of the notices UnsetThresholdsAt gives tenant at
// fixedNow, the subscription read from cfg's own state filters.
func unsetOf(cfg *ThresholdConfig, tenant string) map[string]string {
	subs := UnsetThresholdsSubscribed(cfg.resolveStateFiltersAt(fixedNow, nil))
	out := map[string]string{}
	for _, u := range cfg.UnsetThresholdsAt(fixedNow, subs) {
		if u.Tenant == tenant {
			out[u.Key] = u.Pack
		}
	}
	return out
}

// loadTree writes files under a temp conf.d and loads it the way /metrics does.
func loadTree(t *testing.T, files map[string]string) *ThresholdConfig {
	t.Helper()
	root := t.TempDir()
	for name, body := range files {
		p := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	cfg, _, err := LoadDir(root, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatalf("LoadDir: %v", err)
	}
	return cfg
}

// rootWith is a root _defaults.yaml shipping the unset_thresholds filter at
// defaultState, plus extra root lines.
func rootWith(defaultState, extra string) string {
	return "state_filters:\n  unset_thresholds:\n    reasons: []\n    severity: info\n    default_state: " +
		defaultState + "\n" + extra
}

// tenantFile is a tenant file for "ta" subscribing, with body indented under it.
func tenantFile(body string) string {
	return "tenants:\n  ta:\n    _state_unset_thresholds: enable\n" + body
}

// The generated table is the operator keys of both tiers with their packs.
func TestUnsetThresholdKeys_HoldsOperatorKeysOfBothTiers(t *testing.T) {
	t.Parallel()
	keys := UnsetThresholdKeys()
	for key, pack := range map[string]string{
		"redis_replication_lag": "redis", "jvm_memory": "jvm", // defaults tier
		"oracle_wait_time_rate": "oracle", "mysql_threads_running": "mariadb", // optional_overrides
	} {
		if keys[key] != pack {
			t.Errorf("%s: pack %q, want %q", key, keys[key], pack)
		}
	}
	for _, shipped := range []string{"container_cpu", "pg_connections", "rabbitmq_node_mem_percent"} {
		if _, ok := keys[shipped]; ok {
			t.Errorf("%s ships armed and is not an unset candidate", shipped)
		}
	}
}

// The switch: off by default, on for every tenant when the platform enables
// it, and a tenant's own `_state_unset_thresholds` wins either way. No filter
// at the root: nobody is judged.
func TestUnsetThresholds_Subscription(t *testing.T) {
	t.Parallel()
	const key = "oracle_wait_time_rate"
	decl := "optional_overrides: [oracle_wait_time_rate]\n"
	cases := []struct {
		name  string
		files map[string]string
		want  map[string]bool // tenant → notified
	}{
		{"platform off, nobody opts in", map[string]string{
			"_defaults.yaml": rootWith("disable", decl),
			"t.yaml":         "tenants:\n  ta:\n    redis_x: \"1\"\n  tb:\n    redis_x: \"1\"\n"},
			map[string]bool{"ta": false, "tb": false}},
		{"platform off, ta opts in", map[string]string{
			"_defaults.yaml": rootWith("disable", decl),
			"t.yaml":         "tenants:\n  ta:\n    _state_unset_thresholds: enable\n  tb:\n    redis_x: \"1\"\n"},
			map[string]bool{"ta": true, "tb": false}},
		{"platform on, tb opts out", map[string]string{
			"_defaults.yaml": rootWith("enable", decl),
			"t.yaml":         "tenants:\n  ta:\n    redis_x: \"1\"\n  tb:\n    _state_unset_thresholds: disable\n"},
			map[string]bool{"ta": true, "tb": false}},
		{"another state filter on is no subscription", map[string]string{
			"_defaults.yaml": rootWith("disable", "  container_crashloop:\n    reasons: [CrashLoopBackOff]\n    severity: critical\n"+decl),
			"t.yaml":         "tenants:\n  ta:\n    redis_x: \"1\"\n"},
			map[string]bool{"ta": false}},
		{"no filter at the root: a tenant cannot opt in", map[string]string{
			"_defaults.yaml": decl,
			"t.yaml":         "tenants:\n  ta:\n    _state_unset_thresholds: enable\n"},
			map[string]bool{"ta": false}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			cfg := loadTree(t, tc.files)
			for tenant, want := range tc.want {
				if _, got := unsetOf(cfg, tenant)[key]; got != want {
					t.Errorf("%s notified = %v, want %v", tenant, got, want)
				}
			}
		})
	}
}

// What serves a threshold — or switches the key off — ends the notice; a
// value the resolver does not serve does not. Judged by the rows served, so
// a value written but never read leaves the alert silent and the notice up.
func TestUnsetThresholds_WhatCountsAsConfigured(t *testing.T) {
	t.Parallel()
	decl := "optional_overrides: [oracle_wait_time_rate]\n"
	const key = "oracle_wait_time_rate"
	cases := []struct {
		name      string
		root      string
		tenant    string
		wantUnset bool
	}{
		{"nothing written", decl, "    redis_x: \"1\"\n", true},
		{"a number", decl, "    oracle_wait_time_rate: \"5\"\n", false},
		{"disable", decl, "    oracle_wait_time_rate: \"disable\"\n", false},
		{"a value served only in a window", decl,
			"    oracle_wait_time_rate:\n      default: \"abc\"\n      overrides:\n        - window: \"01:00-02:00\"\n          value: \"5\"\n", false},
		{"disable in a window only", decl,
			"    oracle_wait_time_rate:\n      default: \"abc\"\n      overrides:\n        - window: \"01:00-02:00\"\n          value: \"disable\"\n", false},
		{"a value the resolver refuses", decl, "    oracle_wait_time_rate: \"abc\"\n", true},
		{"disable:critical is not a disable", decl, "    oracle_wait_time_rate: \"disable:critical\"\n", true},
		// #4163: the built-in layer values the key off, so a `_critical`
		// alone is served and arms the critical alert.
		{"only the _critical", decl, "    oracle_wait_time_rate_critical: \"9\"\n", false},
		{"root defaults serves it", "defaults:\n  oracle_wait_time_rate: 5\n", "    redis_x: \"1\"\n", false},
		{"an inline critical serves a row", decl, "    oracle_wait_time_rate: \"5:critical\"\n", false},
		// #4163: a root disable of a key of the built-in layer switches it
		// off for every tenant that gives it no value: no notice.
		{"a root disable switches it off", decl + "defaults:\n  oracle_wait_time_rate: disable\n", "    redis_x: \"1\"\n", false},
		{"a root value it refuses", decl + "defaults:\n  oracle_wait_time_rate: abc\n", "    redis_x: \"1\"\n", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			cfg := loadTree(t, map[string]string{
				"_defaults.yaml": rootWith("disable", tc.root),
				"ta.yaml":        tenantFile(tc.tenant),
			})
			if _, unset := unsetOf(cfg, "ta")[key]; unset != tc.wantUnset {
				t.Errorf("%s unset = %v, want %v", key, unset, tc.wantUnset)
			}
		})
	}
}

// #4163: the built-in layer values every key of the table off, so a key the
// root neither values nor declares is read from the tenant: a flat value
// serves it, and only a key nothing serves is unset.
func TestUnsetThresholds_TheBuiltinLayerMakesEveryKeySettable(t *testing.T) {
	t.Parallel()
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": rootWith("disable", ""),
		"ta.yaml":        tenantFile("    redis_replication_lag: \"30\"\n    jvm_memory_critical: \"95\"\n"),
	})
	got := unsetOf(cfg, "ta")
	for key, wantUnset := range map[string]bool{
		"redis_replication_lag": false, // flat value, nothing declared at the root: served
		"jvm_memory":            false, // only its `_critical`: served
		"oracle_wait_time_rate": true,  // nothing written
	} {
		if _, unset := got[key]; unset != wantUnset {
			t.Errorf("%s unset = %v, want %v (unset %v)", key, unset, wantUnset, got)
		}
	}
}

// A dimensional row serves its base; the deprecated spelling serves the key.
func TestUnsetThresholds_DimensionalAndDeprecatedSpellingsServe(t *testing.T) {
	t.Parallel()
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": rootWith("disable", "optional_overrides: [mysql_threads_running, oracle_wait_time_rate]\n"+
			"dimensions:\n  oracle_wait_time_rate: [instance]\n"),
		"ta.yaml": tenantFile("    mysql_cpu: \"40\"\n    oracle_wait_time_rate{instance=\"o1\"}: \"5\"\n"),
	})
	got := unsetOf(cfg, "ta")
	for _, key := range []string{"mysql_threads_running", "oracle_wait_time_rate"} {
		if _, unset := got[key]; unset {
			t.Errorf("%s is served (deprecated or dimensional spelling): not unset", key)
		}
	}
}

// Judged before the max_metrics_per_tenant cut: a tenant over the cap whose
// row is cut still configured the key, and is not told to write a value.
func TestUnsetThresholds_JudgedBeforeTheCardinalityCut(t *testing.T) {
	t.Parallel()
	cfg := loadTree(t, map[string]string{
		"_defaults.yaml": rootWith("disable", "max_metrics_per_tenant: 1\n"+
			"optional_overrides: [oracle_wait_time_rate]\ndefaults:\n  container_cpu: 80\n  container_memory: 85\n"),
		"ta.yaml": tenantFile("    oracle_wait_time_rate: \"5\"\n"),
	})
	if n := len(cfg.ResolveAt(fixedNow)); n > 1 {
		t.Fatalf("the fixture must hit the cap: %d rows served", n)
	}
	if _, unset := unsetOf(cfg, "ta")["oracle_wait_time_rate"]; unset {
		t.Error("a row cut by max_metrics_per_tenant was still configured")
	}
}

// One entry per (tenant, key) sorted by tenant then key; nil without tenants.
func TestUnsetThresholds_IsSortedAndNilSafe(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{"tb": {}, "ta": {}}}
	got := cfg.UnsetThresholdsAt(fixedNow, map[string]bool{"ta": true, "tb": true, "gone": true})
	if len(got) != 2*len(unsetThresholdKeys) {
		t.Fatalf("%d entries, want %d", len(got), 2*len(unsetThresholdKeys))
	}
	for i := 1; i < len(got); i++ {
		a, b := got[i-1], got[i]
		if a.Tenant > b.Tenant || (a.Tenant == b.Tenant && a.Key >= b.Key) {
			t.Fatalf("not sorted at %d: %v then %v", i, a, b)
		}
	}
	if cfg.UnsetThresholdsAt(fixedNow, nil) != nil {
		t.Error("nobody subscribed: want nil")
	}
	var nilCfg *ThresholdConfig
	if nilCfg.UnsetThresholdsAt(fixedNow, map[string]bool{"ta": true}) != nil {
		t.Error("nil config: want nil")
	}
}

// BenchmarkUnsetThresholdsAt: 1000 subscribed tenants, each writing ten keys
// of its own beside two operator keys (one with a window). The judgement
// resolves only the generated keys, so its cost follows the operator keys.
func BenchmarkUnsetThresholdsAt(b *testing.B) {
	tenants := map[string]map[string]ScheduledValue{}
	subs := map[string]bool{}
	for i := 0; i < 1000; i++ {
		m := map[string]ScheduledValue{
			"oracle_wait_time_rate": {Default: "5", Overrides: []TimeWindowOverride{{Window: "01:00-02:00", Value: "disable"}}},
			"redis_replication_lag": {Default: "30"},
		}
		for j := 0; j < 10; j++ {
			m["container_cpu_x"+string(rune('a'+j))] = ScheduledValue{Default: "70"}
		}
		name := "t" + string(rune('a'+i%26)) + string(rune('a'+i/26%26)) + string(rune('a'+i/676))
		tenants[name] = m
		subs[name] = true
	}
	cfg := &ThresholdConfig{
		Defaults:          map[string]float64{"container_cpu": 80, "container_memory": 85},
		OptionalOverrides: []string{"oracle_wait_time_rate"},
		Tenants:           tenants,
	}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = cfg.UnsetThresholdsAt(fixedNow, subs)
	}
}
