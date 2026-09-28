package main

// #2368: da-guard's redundant-override advice across the #1231 alias
// spellings. MergedDefaults is "what deleting this override falls back to";
// once the platform `tenants:` layer and the tenant stack per THRESHOLD, a
// platform value under the OTHER spelling is that fallback, not the chain's
// value under the tenant's spelling. The oracle is /metrics itself
// (servedWarningAt: the flat plane, with and without the override):
// redundant ⇔ deleting the key leaves the served value unchanged.
//
// Its own file, on the forbid-legacy-mysql-cpu-key exclude list: its
// fixtures ARE the retired spelling as a live value by design.

import (
	"reflect"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

func TestGuard_RedundantOverrideAcrossAliasSpellings(t *testing.T) {
	t.Parallel()
	at := []time.Time{time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)}
	type aliasGuardCase struct {
		name      string
		files     map[string]string // platform files (tx.yaml is added)
		field     string            // the key tx.yaml writes
		value     string
		redundant bool
		extra     string // more tx.yaml lines, kept on both sides of the oracle
	}
	cases := []aliasGuardCase{
		// G1 (exposed by #2368's first commit): chain legacy 30, platform
		// canonical 70, tenant legacy 30 — deleting it serves 70.
		{"G1-chain-legacy-platform-canonical-tenant-legacy",
			map[string]string{"_defaults.yaml": "defaults:\n  mysql_cpu: 30\n  pg_connections: 100\n" +
				"tenants:\n  tx:\n    mysql_threads_running: 70\n"},
			"mysql_cpu", "30", false, ""},
		// G2 (same shape, the other direction): chain canonical 30,
		// platform legacy 70, tenant canonical 30 — deleting it serves 70.
		{"G2-chain-canonical-platform-legacy-tenant-canonical",
			map[string]string{"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n  pg_connections: 100\n" +
				"tenants:\n  tx:\n    mysql_cpu: 70\n"},
			"mysql_threads_running", "30", false, ""},
		// Two platform files: the later one's legacy 70 beats the earlier
		// one's canonical 60, so the tenant's canonical 60 is not the
		// fallback (platformInherited's per-threshold layering).
		{"later-platform-file-legacy-over-earlier-canonical",
			map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n  pg_connections: 100\n",
				"_a.yaml":        "tenants:\n  tx:\n    mysql_threads_running: 60\n",
				"_b.yaml":        "tenants:\n  tx:\n    mysql_cpu: 70\n",
			},
			"mysql_threads_running", "60", false, ""},
		// Controls, same spelling everywhere: still redundant.
		{"control-chain-value-same-spelling",
			map[string]string{"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n  pg_connections: 100\n"},
			"mysql_threads_running", "30", true, ""},
		{"control-platform-value-same-spelling",
			map[string]string{"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n  pg_connections: 100\n" +
				"tenants:\n  tx:\n    mysql_threads_running: 70\n"},
			"mysql_threads_running", "70", true, ""},
	}
	cases = append(cases,
		// #2368 round 2, F-1: ONE layer writes both spellings. Resolve
		// serves only its canonical one, so the losing spelling is no
		// fallback: platform entry 70 / 71, tenant legacy 71 → deleting it
		// serves 70.
		aliasGuardCase{"F1-platform-entry-writes-both-spellings",
			map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n  pg_connections: 100\n",
				"_a.yaml":        "tenants:\n  tx:\n    mysql_threads_running: 70\n    mysql_cpu: 71\n",
			},
			"mysql_cpu", "71", false, ""},
		// …and the chain writing both: tenant legacy 31 → deleting it
		// serves the chain's canonical 30.
		aliasGuardCase{"F1-chain-writes-both-spellings",
			map[string]string{"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n  mysql_cpu: 31\n  pg_connections: 100\n"},
			"mysql_cpu", "31", false, ""},
		// F-2: the TENANT writes both spellings. Deleting its canonical 70
		// serves its own legacy 99, whatever the inherited value is.
		aliasGuardCase{"F2-tenant-writes-both-spellings",
			map[string]string{"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n  pg_connections: 100\n" +
				"tenants:\n  tx:\n    mysql_threads_running: 70\n"},
			"mysql_threads_running", "70", false, "    mysql_cpu: 99\n"},
		// Round 3, B: chain and tenant both on the LEGACY spelling, equal
		// values — redundant. Pins "a lone legacy spelling is not renamed"
		// (dropShadowedSpellings): renaming it to the canonical key would
		// hide this hint, and the product test's missed-hint count alone
		// does not catch that (one lost, one gained).
		aliasGuardCase{"control-chain-and-tenant-both-legacy-same-value",
			map[string]string{"_defaults.yaml": "defaults:\n  mysql_cpu: 30\n  pg_connections: 100\n"},
			"mysql_cpu", "30", true, ""},
		// Round 3, C: a platform null on the canonical key drops nothing
		// (mergeOverSpellings' nil skip): the tenant's legacy 30 wins over
		// it, and deleting it falls back through the null to the chain's
		// legacy 30 — redundant.
		aliasGuardCase{"control-platform-null-canonical-keeps-chain-legacy",
			map[string]string{"_defaults.yaml": "defaults:\n  mysql_cpu: 30\n  pg_connections: 100\n" +
				"tenants:\n  tx:\n    mysql_threads_running: null\n"},
			"mysql_cpu", "30", true, ""},
	)
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			without := "tenants:\n  tx:\n    pg_connections: 100\n" + tc.extra
			tenant := without + "    " + tc.field + ": " + tc.value + "\n"
			dir := t.TempDir()
			testutil.WriteTree(t, dir, tc.files)

			withoutV := servedWarningAt(t, dir, without, "mysql_threads_running", at)
			withV := servedWarningAt(t, dir, tenant, "mysql_threads_running", at) // leaves tx.yaml WITH the override
			if unchanged := reflect.DeepEqual(withV, withoutV); unchanged != tc.redundant {
				t.Fatalf("premise: /metrics mysql_threads_running %v with the override, %v without — want redundant=%v", withV, withoutV, tc.redundant)
			}
			if got := redundantFields(t, dir)[tc.field]; got != tc.redundant {
				t.Errorf("redundant_override on %s = %v, want %v (/metrics %v with the override, %v without)", tc.field, got, tc.redundant, withV, withoutV)
			}
		})
	}
}
