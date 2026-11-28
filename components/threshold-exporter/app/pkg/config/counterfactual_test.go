package config

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

// counterfactualTree is a tree whose tenants inherit from the root, a subtree
// and a profile, with a schedule, several candidates per tenant, and lines
// that are and are not redundant.
func counterfactualTree(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  pg_connections: 80\n  mysql_slow_queries: 5\n  container_cpu: 70\n",
		"_profiles.yaml": "profiles:\n  std:\n    container_cpu: 75\n",
		"a/_defaults.yaml": "defaults:\n  pg_connections:\n    default: \"60\"\n    overrides:\n" +
			"      - {window: \"01:00-02:00\", value: \"90\"}\n",
		"a/t1.yaml": "tenants:\n  t1:\n    pg_connections:\n      default: \"60\"\n      overrides:\n" +
			"        - {window: \"01:00-02:00\", value: \"90\"}\n    mysql_slow_queries: \"5\"\n    container_cpu: \"71\"\n",
		"a/t2.yaml": "tenants:\n  t2:\n    pg_connections: \"60\"\n    mysql_slow_queries: \"6\"\n",
		"t3.yaml":   "tenants:\n  t3:\n    _profile: std\n    container_cpu: \"75\"\n    pg_connections: \"80\"\n",
		"t4.yaml":   "tenants:\n  t4:\n    mysql_slow_queries: \"5\"\n",
	}
	for rel, body := range files {
		path := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// One build per round answers the round's candidate of every tenant at once,
// which holds only if a tenant's rows depend on nothing another tenant's file
// writes. One build per candidate is the reference.
func TestServedUnchangedWithout_RoundsMatchOneAtATime(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)
	scope, err := EffectiveTree(counterfactualTree(t), now)
	if err != nil {
		t.Fatal(err)
	}
	cands := map[string][]string{
		"t1": {"container_cpu", "mysql_slow_queries", "pg_connections"},
		"t2": {"mysql_slow_queries", "pg_connections"},
		"t3": {"container_cpu", "pg_connections"},
		"t4": {"mysql_slow_queries"},
	}
	batched, err := scope.ServedUnchangedWithout(cands, now)
	if err != nil {
		t.Fatal(err)
	}
	single := map[string]map[string]bool{}
	for tenant, keys := range cands {
		single[tenant] = map[string]bool{}
		for _, k := range keys {
			one, err := scope.ServedUnchangedWithout(map[string][]string{tenant: {k}}, now)
			if err != nil {
				t.Fatal(err)
			}
			single[tenant][k] = one[tenant][k]
		}
	}
	if !reflect.DeepEqual(batched, single) {
		t.Fatalf("one build per round %v, one build per candidate %v", batched, single)
	}
	// The premise of the tree: it holds lines of both verdicts.
	want := map[string]map[string]bool{
		"t1": {"container_cpu": false, "mysql_slow_queries": true, "pg_connections": true},
		// A plain 60 over the subtree's schedule: deleting it serves 90 from
		// 01:00 to 02:00.
		"t2": {"mysql_slow_queries": false, "pg_connections": false},
		"t3": {"container_cpu": true, "pg_connections": true},
		"t4": {"mysql_slow_queries": true},
	}
	if !reflect.DeepEqual(batched, want) {
		t.Errorf("verdicts %v, want %v", batched, want)
	}
	// The scope is left as it was: the counterfactual swaps no partial for good.
	again, err := scope.ServedUnchangedWithout(cands, now)
	if err != nil || !reflect.DeepEqual(again, batched) {
		t.Errorf("a second run gave %v (err %v), want %v", again, err, batched)
	}
}
