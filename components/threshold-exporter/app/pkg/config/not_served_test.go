package config

// not_served_test.go — EffectiveConfig.NotServed / ChainParseFailed (#2296)
// per tree shape: every reason, and the shapes that must name nothing (the
// controls, the #1231 retired spellings, `"60:critical"`, an unparseable
// value whose fallback IS the value shown). Each row is checked through both
// readers that fill the fields — EffectiveTree (da-guard effective) and
// ResolveEffective (tenant-api /effective) — which must agree exactly.

import (
	"path/filepath"
	"reflect"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// retiredCPU is the #1231 retired spelling of mysql_threads_running, built
// so the repo's re-introduction guard (forbid-legacy-mysql-cpu-key) does not
// read these fixtures as live config.
const retiredCPU = "mysql_" + "cpu"

func TestNotServed_Table(t *testing.T) {
	t.Parallel()
	ns := func(reason, file string) NotServedKey { return NotServedKey{Reason: reason, File: file} }
	for _, tc := range []struct {
		name  string
		files map[string]string
		want  map[string]NotServedKey // tenant tx
		chain []string                // tenant tx's ChainParseFailed
	}{
		{name: "control", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  " + retiredCPU + ": 5\n",
			"tx.yaml":        "tenants:\n  tx: {}\n",
		}},
		{name: "L0 root value not a number", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: abc\n  " + retiredCPU + ": 5\n",
			"tx.yaml":        "tenants:\n  tx: {}\n",
		}, want: map[string]NotServedKey{
			"mysql_connections": ns(NotServedParseFailed, "_defaults.yaml"),
			retiredCPU:          ns(NotServedParseFailed, "_defaults.yaml"),
		}},
		{name: "root defaults a list", files: map[string]string{
			"_defaults.yaml": "defaults: [1, 2]\n",
			"tx.yaml":        "tenants:\n  tx: {}\n",
		}, want: map[string]NotServedKey{"defaults": ns(NotServedParseFailed, "_defaults.yaml")}},
		{name: "L1 subtree value rejected", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  mysql_connections: 80\n",
			"sub/_defaults.yaml": "defaults: {mysql_connections: abc}\n",
			"sub/tx.yaml":        "tenants:\n  tx: {}\n",
		}, want: map[string]NotServedKey{"mysql_connections": ns(NotServedValueRejected, "sub/_defaults.yaml")}},
		{name: "L1 syntax error", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  mysql_connections: 80\n",
			"sub/_defaults.yaml": "defaults: [\n",
			"sub/tx.yaml":        "tenants:\n  tx: {}\n",
		}, chain: []string{"sub/_defaults.yaml"}},
		{name: "root syntax error", files: map[string]string{
			"_defaults.yaml":     "defaults: [\n",
			"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n",
			"sub/tx.yaml":        "tenants:\n  tx: {}\n",
		}, chain: []string{"_defaults.yaml"}, want: map[string]NotServedKey{
			// The root no longer declares it, so the subtree value is the
			// build's undeliverable verdict.
			"mysql_connections": ns(NotServedUndeliverable, "sub/_defaults.yaml"),
		}},
		{name: "inline merge string", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  mysql_connections: 30\n",
			"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n",
			"sub/tx.yaml":        "tenants:\n  tx:\n    mysql_connections:\n      <<: {default: \"55\"}\n",
		}, want: map[string]NotServedKey{"mysql_connections": ns(NotServedValueUnparsed, "sub/tx.yaml")}},
		{name: "inline merge null", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  mysql_connections: 30\n",
			"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n",
			"sub/tx.yaml":        "tenants:\n  tx:\n    mysql_connections:\n      <<: {default: null}\n",
		}, want: map[string]NotServedKey{"mysql_connections": ns(NotServedValueUnparsed, "sub/_defaults.yaml")}},
		{name: "inline merge null, no subtree level", files: map[string]string{
			// /metrics falls back to the root's 30, which is the value shown.
			"_defaults.yaml": "defaults:\n  mysql_connections: 30\n",
			"tx.yaml":        "tenants:\n  tx:\n    mysql_connections:\n      <<: {default: null}\n",
		}},
		{name: "unparseable in one window only", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 30\n",
			"tx.yaml": "tenants:\n  tx:\n    mysql_connections:\n      default: \"60\"\n" +
				"      overrides:\n        - window: \"00:00-00:01\"\n          value: \"abc\"\n",
		}, want: map[string]NotServedKey{"mysql_connections": ns(NotServedValueUnparsed, "tx.yaml")}},
		{name: "critical row dropped", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 30\n",
			"tx.yaml":        "tenants:\n  tx:\n    mysql_connections_critical: \"abc\"\n",
		}, want: map[string]NotServedKey{"mysql_connections_critical": ns(NotServedValueUnparsedDropped, "tx.yaml")}},
		{name: "value with severity", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 30\n",
			"tx.yaml":        "tenants:\n  tx:\n    mysql_connections: \"60:critical\"\n",
		}},
		{name: "subtree key undeliverable", files: map[string]string{
			"_defaults.yaml":     "defaults:\n  mysql_connections: 30\n",
			"sub/_defaults.yaml": "defaults:\n  redis_memory: 50\n",
			"sub/tx.yaml":        "tenants:\n  tx: {}\n",
		}, want: map[string]NotServedKey{"redis_memory": ns(NotServedUndeliverable, "sub/_defaults.yaml")}},
		{name: "root null undeclared", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 30\n  redis_memory: null\n",
			"tx.yaml":        "tenants:\n  tx:\n    redis_memory: 60\n",
		}, want: map[string]NotServedKey{"redis_memory": ns(NotServedRootNullUndeclared, "tx.yaml")}},
		{name: "A0 canonical spelling deeper", files: map[string]string{
			"_defaults.yaml":      "defaults:\n  mysql_threads_running: 80\n",
			"team/_defaults.yaml": "defaults:\n  mysql_threads_running: 50\n",
			"team/tx.yaml":        "tenants:\n  tx: {}\n",
		}},
		{name: "A1 retired spelling deeper", files: map[string]string{
			"_defaults.yaml":      "defaults:\n  mysql_threads_running: 80\n",
			"team/_defaults.yaml": "defaults:\n  " + retiredCPU + ": 50\n",
			"team/tx.yaml":        "tenants:\n  tx: {}\n",
		}},
		{name: "F3 retired spelling in the tenant", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_threads_running: 80\n",
			"tx.yaml":        "tenants:\n  tx:\n    " + retiredCPU + ": \"60\"\n",
		}},
		{name: "F2c root wrapped", files: map[string]string{
			"_defaults.yaml": "defaults:\n  pg_connections: 100\n",
			"tx.yaml":        "tenants:\n  tx: {}\n",
		}},
		{name: "F2 root unwrapped", files: map[string]string{
			"_defaults.yaml": "pg_connections: 100\n",
			"tx.yaml":        "tenants:\n  tx: {}\n",
		}, want: map[string]NotServedKey{"pg_connections": ns(NotServedRootDefaultsUnwrapped, "_defaults.yaml")}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tmp := t.TempDir()
			files := map[string]string{}
			for k, v := range tc.files {
				files[filepath.Join("conf.d", k)] = v
			}
			// A second tenant with nothing of its own: the tables are per
			// tenant, and it must not borrow tx's.
			files[filepath.Join("conf.d", "zz", "ty.yaml")] = "tenants:\n  ty: {}\n"
			testutil.WriteTree(t, tmp, files)
			root := filepath.Join(tmp, "conf.d")

			tree, err := EffectiveTree(root)
			if err != nil {
				t.Fatalf("EffectiveTree: %v", err)
			}
			byID := map[string]*EffectiveConfig{}
			for _, ec := range tree.Tenants {
				byID[ec.TenantID] = ec
			}
			tx := byID["tx"]
			if tx == nil {
				t.Fatalf("tenant tx missing from EffectiveTree (tenants %d)", len(tree.Tenants))
			}
			if !reflect.DeepEqual(tx.NotServed, tc.want) {
				t.Errorf("EffectiveTree NotServed = %v, want %v (effective_config %v)", tx.NotServed, tc.want, tx.EffectiveConfig)
			}
			if !reflect.DeepEqual(tx.ChainParseFailed, tc.chain) {
				t.Errorf("EffectiveTree ChainParseFailed = %v, want %v", tx.ChainParseFailed, tc.chain)
			}
			for id, ec := range byID {
				one, err := ResolveEffective(root, id)
				if err != nil {
					t.Fatalf("ResolveEffective(%s): %v", id, err)
				}
				if !reflect.DeepEqual(one.NotServed, ec.NotServed) || !reflect.DeepEqual(one.ChainParseFailed, ec.ChainParseFailed) {
					t.Errorf("%s: ResolveEffective NotServed %v ChainParseFailed %v, EffectiveTree %v %v",
						id, one.NotServed, one.ChainParseFailed, ec.NotServed, ec.ChainParseFailed)
				}
				if one.MergedHash != ec.MergedHash {
					t.Errorf("%s: merged_hash %s vs %s", id, one.MergedHash, ec.MergedHash)
				}
			}
			if ty := byID["ty"]; ty == nil {
				t.Error("tenant ty missing")
			} else if tc.name == "control" && (ty.NotServed != nil || ty.ChainParseFailed != nil) {
				t.Errorf("ty: NotServed %v ChainParseFailed %v, want none", ty.NotServed, ty.ChainParseFailed)
			}
		})
	}
}

// TestNotServed_ScopeEffectiveFillsNone: the gate (ScopeEffective) does not
// pay for the tables, and keeps stopping on an undecodable chain file.
func TestNotServed_ScopeEffectiveFillsNone(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":     "defaults:\n  mysql_connections: 80\n",
		"conf.d/sub/_defaults.yaml": "defaults: {mysql_connections: abc}\n",
		"conf.d/sub/tx.yaml":        "tenants:\n  tx: {}\n",
	})
	got, err := ScopeEffective(filepath.Join(tmp, "conf.d"), "")
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Tenants) != 1 || got.Tenants[0].NotServed != nil {
		t.Fatalf("ScopeEffective tenants %v: want tx with no NotServed", got.Tenants)
	}
}

// TestRejectRecorder_ChangesNoRow: the scrape path passes a nil recorder;
// resolving with one changes no row and records each unparseable value once.
func TestRejectRecorder_ChangesNoRow(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 30},
		Tenants: map[string]map[string]ScheduledValue{
			"tx": {"mysql_connections": {Default: "abc"}, "mysql_connections_critical": {Default: "x"}},
		},
	}
	at := scheduleDay
	plain, _ := cfg.resolveAtWithStats(at, nil, nil, nil)
	rec := &rejectRecorder{}
	recorded, _ := cfg.resolveAtWithStats(at, nil, nil, rec)
	if !reflect.DeepEqual(plain, recorded) {
		t.Errorf("rows differ with a recorder:\n%v\n%v", plain, recorded)
	}
	want := map[string]map[string]string{"tx": {
		"mysql_connections":          NotServedValueUnparsed,
		"mysql_connections_critical": NotServedValueUnparsedDropped,
	}}
	if !reflect.DeepEqual(rec.byTenant, want) {
		t.Errorf("recorded %v, want %v", rec.byTenant, want)
	}
}
