package config

// subtree_reserved_test.go — #2388: ScopedTenants.SubtreeReserved lists, per
// in-scope tenant, the reserved keys (subtreeDefaultsRefusedKey) a SUBTREE
// `_defaults.yaml` of its chain writes, whatever the value. It and #1976's
// ScopedTenants.Undeliverable split one key space with one predicate: a key a
// subtree writes and the build cannot deliver is in exactly one of the two.
//
// Seams: none — t.TempDir() trees.

import (
	"reflect"
	"testing"
)

func TestScopeEffective_SubtreeReservedKeys(t *testing.T) {
	t.Parallel()
	dir := writeUndeliverableTree(t, map[string]string{
		// The root's own reserved key is not a subtree level: not listed.
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  _state_maintenance: 0\n" +
			"state_filters:\n  maintenance:\n    reasons: []\n    severity: warning\n    default_state: enable\n",
		// enable (dropped), a severity (dropped), disable (applied), an
		// undeclared filter (undeliverable), a null, an unrecognised `_` key
		// and a plain threshold (the last two are #1976's, not #2388's).
		"finance/_defaults.yaml": "defaults:\n  _state_maintenance: enable\n  _silent_mode: warning\n" +
			"  _severity_dedup: disable\n  _state_bogus: 5\n  _metadata: null\n  _myth: 5\n  redis_x: 5\n" +
			// `_routing*` is the routing checks' (routing_in_unread_location).
			"  _routing_profile: p1\n",
		"finance/us/_defaults.yaml": "defaults:\n  _state_maintenance: disable\n",
		"finance/us/tenant-a.yaml":  "tenants:\n  tenant-a:\n    _silent_mode: critical\n",
		"ops/tenant-b.yaml":         "tenants:\n  tenant-b: {}\n",
	})

	scoped, err := ScopeEffective(dir, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(scoped.ParseFailed) != 0 {
		t.Fatalf("ParseFailed = %v; the tree must load whole", scoped.ParseFailed)
	}
	wantReserved := map[string]map[string][]string{
		"tenant-a": {
			// Listed per level, root-first; listed although the tenant sets
			// `_silent_mode` itself.
			"_state_maintenance": {"finance/_defaults.yaml", "finance/us/_defaults.yaml"},
			"_silent_mode":       {"finance/_defaults.yaml"},
			"_severity_dedup":    {"finance/_defaults.yaml"},
			"_state_bogus":       {"finance/_defaults.yaml"},
			// `_metadata: null` is not listed: a null writes nothing (#2388 r1b).
		},
	}
	if !reflect.DeepEqual(scoped.SubtreeReserved, wantReserved) {
		t.Errorf("SubtreeReserved = %v, want %v", scoped.SubtreeReserved, wantReserved)
	}
	// The other half of the partition (#1976): the undeliverable keys the
	// predicate does not refuse, and none it does.
	wantUndeliverable := map[string][]string{"tenant-a": {"_myth", "redis_x"}}
	if !reflect.DeepEqual(scoped.Undeliverable, wantUndeliverable) {
		t.Errorf("Undeliverable = %v, want %v", scoped.Undeliverable, wantUndeliverable)
	}

	// Scope: tenant-a is outside ops/.
	ops, err := ScopeEffective(dir, "ops")
	if err != nil {
		t.Fatal(err)
	}
	if ops.SubtreeReserved != nil {
		t.Errorf("--scope ops: SubtreeReserved = %v, want nil", ops.SubtreeReserved)
	}
}

// #2388 r1b: a null reserved key in a subtree level is treated as not written
// (the #2518 ruling: null deletes that level's value, the next level's
// applies). The shape of the golden fixture mixed-mode/conf.d/reserved-null:
// the parent writes `_severity_dedup` and `_silent_mode`, the child nulls
// `_severity_dedup` — only the parent is listed. A tree whose only subtree
// mention of the key is the null lists nothing.
func TestScopeEffective_SubtreeReservedNullIsNotWritten(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name  string
		files map[string]string
		want  map[string]map[string][]string
	}{
		{
			name: "reserved-null",
			files: map[string]string{
				"_defaults.yaml":                     "defaults:\n  mysql_connections: 80\n",
				"reserved-null/_defaults.yaml":       "defaults:\n  _severity_dedup: \"disable\"\n  _silent_mode: \"warning\"\n",
				"reserved-null/child/_defaults.yaml": "defaults:\n  _severity_dedup: ~\n",
				"reserved-null/child/tenants.yaml":   "tenants:\n  tenant-reserved: {}\n",
			},
			want: map[string]map[string][]string{"tenant-reserved": {
				"_severity_dedup": {"reserved-null/_defaults.yaml"},
				"_silent_mode":    {"reserved-null/_defaults.yaml"},
			}},
		},
		{
			name: "null-only",
			files: map[string]string{
				"_defaults.yaml":         "defaults:\n  mysql_connections: 80\n",
				"finance/_defaults.yaml": "defaults:\n  _severity_dedup: ~\n  _silent_mode: null\n",
				"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			scoped, err := ScopeEffective(writeUndeliverableTree(t, tc.files), "")
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(scoped.SubtreeReserved, tc.want) {
				t.Errorf("SubtreeReserved = %v, want %v", scoped.SubtreeReserved, tc.want)
			}
		})
	}
}
