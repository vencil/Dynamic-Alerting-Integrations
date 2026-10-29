package config

// #3378: the subtree overlay delivers a `<base>_critical` only when
// resolveCriticalRows would serve it — the base holds a value in the root
// `defaults:` (criticalBaseValued, either #1231 spelling). Otherwise the key
// is the overlay's undeliverable value (subtree_default_undeliverable), not a
// value written into the tenant's map that no row reads. Each case is
// measured on the output: the rows /metrics serves (ResolveAtWithKeys over
// the exporter's own build, LoadDir).

import (
	"io"
	"log"
	"reflect"
	"testing"
)

func TestSubtreeCritical_DeliveredOnlyOverAValuedBase(t *testing.T) {
	t.Parallel()
	const sub = "defaults:\n  redis_memory_critical: 90\n"
	for _, tc := range []struct {
		name          string
		files         map[string]string
		key           string  // the critical key looked at, as the tree spells it
		undeliverable bool    // in LoadReport.Undeliverable / not_served undeliverable
		served        float64 // the key's row on /metrics (either reader); 0 = none
		notServed     map[string]NotServedKey
	}{
		{name: "S3 base only in optional_overrides", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  mysql_connections: 80\noptional_overrides: [redis_memory]\n",
			"sub/_defaults.yaml": sub, "sub/tx.yaml": "tenants:\n  tx: {}\n",
		}, key: "redis_memory_critical", undeliverable: true,
			notServed: map[string]NotServedKey{"redis_memory_critical": {Reason: NotServedUndeliverable, File: "sub/_defaults.yaml"}}},
		{name: "S3e base nowhere at the root", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  mysql_connections: 80\n",
			"sub/_defaults.yaml": sub, "sub/tx.yaml": "tenants:\n  tx: {}\n",
		}, key: "redis_memory_critical", undeliverable: true,
			notServed: map[string]NotServedKey{"redis_memory_critical": {Reason: NotServedUndeliverable, File: "sub/_defaults.yaml"}}},
		{name: "S3r base valued at the root: served", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  redis_memory: 60\n",
			"sub/_defaults.yaml": sub, "sub/tx.yaml": "tenants:\n  tx: {}\n",
		}, key: "redis_memory_critical", served: 90},
		{name: "S3t the tenant writes the base itself: still undeliverable", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  mysql_connections: 80\noptional_overrides: [redis_memory]\n",
			"sub/_defaults.yaml": sub, "sub/tx.yaml": "tenants:\n  tx:\n    redis_memory: \"70\"\n",
		}, key: "redis_memory_critical", undeliverable: true,
			notServed: map[string]NotServedKey{"redis_memory_critical": {Reason: NotServedUndeliverable, File: "sub/_defaults.yaml"}}},
		{name: "S3b the tenant writes the _critical, no base: the resolver's unconsumed", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"sub/tx.yaml":    "tenants:\n  tx:\n    redis_memory_critical: \"90\"\n",
		}, key: "redis_memory_critical",
			notServed: map[string]NotServedKey{"redis_memory_critical": {Reason: NotServedUnconsumed, File: "sub/tx.yaml"}}},
		{name: "root holds the retired base spelling, subtree the canonical critical", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  " + retiredCPU + ": 50\n",
			"sub/_defaults.yaml": "defaults:\n  mysql_threads_running_critical: 90\n", "sub/tx.yaml": "tenants:\n  tx: {}\n",
		}, key: "mysql_threads_running_critical", served: 90},
		{name: "root holds the canonical base, subtree the retired critical", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  mysql_threads_running: 50\n",
			"sub/_defaults.yaml": "defaults:\n  " + retiredCPU + "_critical: 90\n", "sub/tx.yaml": "tenants:\n  tx: {}\n",
		}, key: retiredCPU + "_critical", served: 90},
		// The other reader of a `_critical` key: a root `defaults:` that
		// values the key itself (no base) is walked by resolveBaseRows, which
		// serves the subtree's value of it.
		{name: "root values the _critical key itself, no base: the subtree's value is served", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  redis_memory_critical: 5\n",
			"sub/_defaults.yaml": sub, "sub/tx.yaml": "tenants:\n  tx: {}\n",
		}, key: "redis_memory_critical", served: 90},
		{name: "root values the retired spelling of the _critical key itself", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  " + retiredCPU + "_critical: 5\n",
			"sub/_defaults.yaml": "defaults:\n  mysql_threads_running_critical: 90\n", "sub/tx.yaml": "tenants:\n  tx: {}\n",
		}, key: "mysql_threads_running_critical", served: 90},
		// A root-null base: the subtree value is not delivered, so it is the
		// overlay's undeliverable (an error), not a tenant value the root
		// null leaves unserved (root_null_undeclared, a warning).
		{name: "subtree _critical over a root-null base", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  mysql_connections: 80\n  redis_memory: null\n",
			"sub/_defaults.yaml": sub, "sub/tx.yaml": "tenants:\n  tx: {}\n",
		}, key: "redis_memory_critical", undeliverable: true,
			notServed: map[string]NotServedKey{"redis_memory_critical": {Reason: NotServedUndeliverable, File: "sub/_defaults.yaml"}}},
		{name: "tenant _critical over a root-null base the root values the key of: served, nothing named", files: map[string]string{
			"_defaults.yaml": "defaults:\n  redis_memory: null\n  redis_memory_critical: 5\n",
			"sub/tx.yaml":    "tenants:\n  tx:\n    redis_memory_critical: \"90\"\n",
		}, key: "redis_memory_critical", served: 90},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			root := writeUnconsumedTree(t, tc.files)
			cfg, rep, err := LoadDirReport(root, log.New(io.Discard, "", 0))
			if err != nil {
				t.Fatal(err)
			}
			if _, got := rep.Undeliverable["tx"][tc.key]; got != tc.undeliverable {
				t.Errorf("undeliverable %v, want %v (%v)", got, tc.undeliverable, rep.Undeliverable)
			}
			keyed, _, err := cfg.ResolveAtWithKeys(fixedNow)
			if err != nil {
				t.Fatal(err)
			}
			var got float64
			canon, _ := canonicalKeyFor(tc.key)
			for _, k := range keyed {
				if k.Tenant == "tx" && k.Key == canon && !k.LegacyTwin {
					got = k.Value
				}
			}
			if got != tc.served {
				t.Errorf("/metrics critical row %v, want %v", got, tc.served)
			}
			ec, err := ResolveEffective(root, "tx", fixedNow)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(ec.NotServed, tc.notServed) {
				t.Errorf("not_served %v, want %v", ec.NotServed, tc.notServed)
			}
		})
	}
}
