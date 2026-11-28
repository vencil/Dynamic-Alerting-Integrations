package main

import (
	"slices"
	"testing"
)

// #4330: redundant_override on a threshold key is the counterfactual —
// deleting the line leaves what the exporter serves for the tenant the same
// all day — and no other finding owns the line. Each case is a shape a
// predicate comparing the tenant's value with the inherited one got wrong.
func TestGuard_RedundantOverrideIsTheCounterfactual(t *testing.T) {
	t.Parallel()
	const dims = "dimensions:\n  pg_connections: [env, env_re]\n"
	for _, tc := range []struct {
		name      string
		files     map[string]string
		field     string
		redundant bool
	}{
		{"number at the root, string in the tenant file",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 80\n",
				"tx.yaml": "tenants:\n  tx:\n    pg_connections: \"80\"\n"},
			"pg_connections", true},
		{"another value is no redundancy",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 80\n",
				"tx.yaml": "tenants:\n  tx:\n    pg_connections: \"81\"\n"},
			"pg_connections", false},
		// Deleting the line moves the threshold from 77 to the built-in 80:
		// the unwrapped root line is no default (root_defaults_unwrapped).
		{"root carrier without defaults: wrapper",
			map[string]string{"_defaults.yaml": "pg_connections: 77\n",
				"tx.yaml": "tenants:\n  tx:\n    pg_connections: 77\n"},
			"pg_connections", false},
		// The tenant's own base `disable` switches the row off: deleting the
		// line serves nothing either way, and the key verdict reports the line
		// (disable_cascade) — its finding, not a redundancy.
		{"critical tier of a base the tenant switches off",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 80\n  pg_connections_critical: 90\n",
				"tx.yaml": "tenants:\n  tx:\n    pg_connections: disable\n    pg_connections_critical: 90\n"},
			"pg_connections_critical", false},
		// Unread (the shared key verdict's unread error owns it): its label
		// is not declared, so nothing is served either way.
		{"dimensional key whose label is not declared",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 80\n  'pg_connections{pod=\"x\"}': 50\n",
				"tx.yaml": "tenants:\n  tx:\n    'pg_connections{pod=\"x\"}': \"50\"\n"},
			`pg_connections{pod="x"}`, false},
		// A regex key holds dots; the key is never cut at one.
		{"regex dimensional key with a dot, label not declared",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 80\n  'pg_connections{db=~\"p.*\"}': 50\n",
				"tx.yaml": "tenants:\n  tx:\n    'pg_connections{db=~\"p.*\"}': \"50\"\n"},
			`pg_connections{db=~"p.*"}`, false},
		{"regex dimensional key with a dot, label declared",
			map[string]string{"_defaults.yaml": dims + "defaults:\n  pg_connections: 80\n  'pg_connections{env=~\"p.*\"}': 50\n",
				"tx.yaml": "tenants:\n  tx:\n    'pg_connections{env=~\"p.*\"}': \"50\"\n"},
			`pg_connections{env=~"p.*"}`, true},
		// value_not_served owns a refused value, even one the subtree writes too.
		{"the same refused value on both sides",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 80\n",
				"a/_defaults.yaml": "defaults:\n  pg_connections: \"0x50\"\n",
				"a/tx.yaml":        "tenants:\n  tx:\n    pg_connections: \"0x50\"\n"},
			"pg_connections", false},
		// The tenant's schedule repeats the subtree's: deleting it serves
		// the same at every minute of the day.
		{"schedule equal to the inherited one",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 80\n",
				"a/_defaults.yaml": "defaults:\n  pg_connections:\n    default: \"60\"\n    overrides:\n      - {window: \"01:00-02:00\", value: \"90\"}\n",
				"a/tx.yaml":        "tenants:\n  tx:\n    pg_connections:\n      default: \"60\"\n      overrides:\n        - {window: \"01:00-02:00\", value: \"90\"}\n"},
			"pg_connections", true},
		// …and one that differs only inside a window is not, whatever the
		// minute the run is made at.
		{"schedule differing only inside a window",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 80\n",
				"a/_defaults.yaml": "defaults:\n  pg_connections:\n    default: \"60\"\n    overrides:\n      - {window: \"01:00-02:00\", value: \"90\"}\n",
				"a/tx.yaml":        "tenants:\n  tx:\n    pg_connections:\n      default: \"60\"\n      overrides:\n        - {window: \"01:00-02:00\", value: \"95\"}\n"},
			"pg_connections", false},
		{"another value under a base the tenant switches off",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 80\n  pg_connections_critical: 90\n",
				"tx.yaml": "tenants:\n  tx:\n    pg_connections: disable\n    pg_connections_critical: 95\n"},
			"pg_connections_critical", false},
		// A retired key the exporter still serves: the key verdict reports
		// the line (retired), so it is that finding's (owner call, #4330).
		{"retired key repeating the inherited value",
			map[string]string{"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
				"tx.yaml": "tenants:\n  tx:\n    mysql_connections: \"80\"\n"},
			"mysql_connections", false},
		// An inherited value time-boxed to expire: deleting the line serves
		// the root's value once it expires, so the line is not redundant.
		{"inherited value that expires",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 100\n",
				"a/_defaults.yaml": "defaults:\n  pg_connections:\n    default: \"50\"\n    expires: \"2099-01-01T00:00:00Z\"\n",
				"a/tx.yaml":        "tenants:\n  tx:\n    pg_connections: \"50\"\n"},
			"pg_connections", false},
		// The tenant's own value expires (#4263): past it the merge serves the
		// layer beneath — here the same subtree value, so the line changes
		// nothing at any instant.
		{"own expiring value equal to the layer beneath",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 100\n",
				"a/_defaults.yaml": "defaults:\n  pg_connections: \"50\"\n",
				"a/tx.yaml":        "tenants:\n  tx:\n    pg_connections:\n      default: \"50\"\n      expires: \"2099-01-01T00:00:00Z\"\n"},
			"pg_connections", true},
		// …and one over an inherited value that expires first: from 2098 to
		// 2099 deleting the line serves the root's 100, not 50.
		{"own expiring value over an inherited one that also expires",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 100\n",
				"a/_defaults.yaml": "defaults:\n  pg_connections:\n    default: \"50\"\n    expires: \"2098-01-01T00:00:00Z\"\n",
				"a/tx.yaml":        "tenants:\n  tx:\n    pg_connections:\n      default: \"50\"\n      expires: \"2099-01-01T00:00:00Z\"\n"},
			"pg_connections", false},
		// The shared key verdict owns a `disable` that switches nothing off
		// (disable_noop): no default to switch off.
		{"disable with nothing to switch off",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 100\n",
				"tx.yaml": "tenants:\n  tx:\n    mysql_threads_running: disable\n"},
			"mysql_threads_running", false},
		{"scheduled disable with nothing to switch off",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 100\n",
				"tx.yaml": "tenants:\n  tx:\n    mysql_threads_running:\n      default: disable\n      overrides:\n        - {window: \"01:00-02:00\", value: disable}\n"},
			"mysql_threads_running", false},
		// The key verdict spells the key as written; the decode re-spells it.
		{"dimensional disable spelled out of canonical order",
			map[string]string{"_defaults.yaml": "dimensions:\n  pg_connections: [env, db]\ndefaults:\n  pg_connections: 80\n",
				"tx.yaml": "tenants:\n  tx:\n    pg_connections: disable\n    'pg_connections{env=\"p\",db=\"x\"}': disable\n"},
			`pg_connections{env="p",db="x"}`, false},
		// A `disable` only inside a window under a base the tenant switches
		// off: the key verdict reports the line (disable_cascade).
		{"disable only inside a window, base switched off",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 80\n  pg_connections_critical: 90\n",
				"tx.yaml": "tenants:\n  tx:\n    pg_connections: disable\n    pg_connections_critical:\n      default: \"90\"\n      overrides:\n        - {window: \"10:00-11:00\", value: disable}\n"},
			"pg_connections_critical", false},
		{"critical disable with no critical tier",
			map[string]string{"_defaults.yaml": "defaults:\n  pg_connections: 100\n",
				"tx.yaml": "tenants:\n  tx:\n    container_memory_critical: disable\n"},
			"container_memory_critical", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := allFindings(t, "--config-dir", writeReservedConfD(t, tc.files))
			reported := slices.Contains(got, "warn redundant_override tx "+tc.field)
			if reported != tc.redundant {
				t.Errorf("redundant_override on %s = %v, want %v (findings %q)", tc.field, reported, tc.redundant, got)
			}
		})
	}
}
