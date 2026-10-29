package main

// #3378: da-guard reports a subtree `<base>_critical` whose base holds no
// value in the root `defaults:` as subtree_default_undeliverable (error),
// as the exporter refuses it; with the base valued at the root nothing is
// reported. A tenant writing such a `_critical` itself is not the overlay's
// (the resolver names it unconsumed in not_served, which no finding selects).

import (
	"reflect"
	"testing"
)

func TestGuard_SubtreeCriticalOverAnUnvaluedBase(t *testing.T) {
	t.Parallel()
	const sub = "defaults:\n  redis_memory_critical: 90\n"
	for _, tc := range []struct {
		name  string
		files map[string]string
		code  int
		want  []string // kind/tenant/field
	}{
		{"S3 base only in optional_overrides", map[string]string{
			"_defaults.yaml":      "defaults:\n  mysql_connections: 80\noptional_overrides: [redis_memory]\n",
			"team/_defaults.yaml": sub, "team/tx.yaml": "tenants:\n  tx: {}\n",
		}, exitFindings, []string{"subtree_default_undeliverable/tx/redis_memory_critical"}},
		{"S3e base nowhere at the root", map[string]string{
			"_defaults.yaml":      "defaults:\n  mysql_connections: 80\n",
			"team/_defaults.yaml": sub, "team/tx.yaml": "tenants:\n  tx: {}\n",
		}, exitFindings, []string{"subtree_default_undeliverable/tx/redis_memory_critical"}},
		{"S3r base valued at the root", map[string]string{
			"_defaults.yaml":      "defaults:\n  redis_memory: 60\n",
			"team/_defaults.yaml": sub, "team/tx.yaml": "tenants:\n  tx: {}\n",
		}, exitOK, nil},
		{"S3t the tenant writes the base, the subtree the critical", map[string]string{
			"_defaults.yaml":      "defaults:\n  mysql_connections: 80\noptional_overrides: [redis_memory]\n",
			"team/_defaults.yaml": sub, "team/tx.yaml": "tenants:\n  tx:\n    redis_memory: \"70\"\n",
		}, exitFindings, []string{"subtree_default_undeliverable/tx/redis_memory_critical"}},
		{"S3b the tenant writes the critical, no base", map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"team/tx.yaml":   "tenants:\n  tx:\n    redis_memory_critical: \"90\"\n",
		}, exitOK, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			code, fs := guardFindingsOf(t, tc.files)
			var got []string
			for _, f := range fs {
				got = append(got, f.Kind+"/"+f.TenantID+"/"+f.Field)
			}
			if code != tc.code || !reflect.DeepEqual(got, tc.want) {
				t.Errorf("exit %d findings %v, want %d %v", code, got, tc.code, tc.want)
			}
		})
	}
}
