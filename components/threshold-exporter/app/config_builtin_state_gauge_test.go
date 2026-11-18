package main

// #3831: da_config_builtin_default_state{key,state} and
// da_config_builtin_defaults_info{digest}. Observed through the metrics seam
// (freshMetrics + SetMetrics; test-map.md), on every load path of the
// exporter: the full directory build, the flat incremental reload (both its
// rebuild and its tenant-only patch branch), the hierarchical reload and the
// single-file mode.

import (
	"io"
	"log"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// builtinStateGauge is, per built-in key, the one state whose series is 1;
// "" when none is, "!" when more than one is.
func builtinStateGauge(t *testing.T, cm *configMetrics) map[string]string {
	t.Helper()
	out := map[string]string{}
	for _, k := range config.BuiltinDefaultKeys() {
		for _, s := range config.BuiltinStates() {
			switch v := testutil.ToFloat64(cm.builtinDefaultState.WithLabelValues(k, s)); v {
			case 1:
				if out[k] != "" {
					out[k] = "!"
				} else {
					out[k] = s
				}
			case 0:
			default:
				t.Fatalf("%s{state=%q} = %v, want 0 or 1", k, s, v)
			}
		}
	}
	return out
}

// wantBuiltinStates is every built-in key in state "builtin", except those
// named in except.
func wantBuiltinStates(except map[string]string) map[string]string {
	out := map[string]string{}
	for _, k := range config.BuiltinDefaultKeys() {
		out[k] = config.BuiltinStateBuiltin
		if s, ok := except[k]; ok {
			out[k] = s
		}
	}
	return out
}

func assertBuiltinStates(t *testing.T, what string, got, want map[string]string) {
	t.Helper()
	for k, w := range want {
		if got[k] != w {
			t.Errorf("%s: %s state %q, want %q", what, k, got[k], w)
		}
	}
	if len(got) != len(want) {
		t.Errorf("%s: %d keys on the gauge, want %d", what, len(got), len(want))
	}
}

func newGaugedManager(t *testing.T, path string) (*ConfigManager, *configMetrics) {
	t.Helper()
	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(path, 0)
	t.Cleanup(m.Close)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(io.Discard, "", 0))
	return m, fresh
}

func TestBuiltinStateGauge_HierarchicalTree(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  pg_connections: 70\n  container_memory: \"disable\"\n  redis_connected_clients: 1000\n")
	writeTestYAML(t, filepath.Join(dir, "team", "t1.yaml"), "tenants:\n  t1:\n    container_cpu: \"disable\"\n")
	m, fresh := newGaugedManager(t, dir)
	if got := builtinStateGauge(t, fresh); len(got) != 0 {
		t.Fatalf("before the first commit: %v, want no 1 anywhere", got)
	}
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	want := wantBuiltinStates(map[string]string{
		"pg_connections":   config.BuiltinStateOverridden,
		"container_memory": config.BuiltinStateOptedOut,
		// A tenant's own disable is below the root: the key is still builtin.
	})
	assertBuiltinStates(t, "cold load", builtinStateGauge(t, fresh), want)

	// The same answer `da-guard effective` gives for the tree (one
	// derivation, config.BuiltinStatesOf).
	tree, err := config.ScopeEffective(dir, dir, time.Now())
	if err != nil {
		t.Fatalf("ScopeEffective: %v", err)
	}
	guard := map[string]string{}
	for k, st := range tree.BuiltinDefaultStates() {
		guard[k] = st.State
	}
	assertBuiltinStates(t, "da-guard effective", guard, want)

	// The root stops writing pg_connections: it moves to builtin on the
	// reload, its overridden series back to 0.
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
		"defaults:\n  container_memory: \"disable\"\n  redis_connected_clients: 1000\n")
	if _, _, err := m.diffAndReload(); err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}
	assertBuiltinStates(t, "after the root drops pg_connections", builtinStateGauge(t, fresh),
		wantBuiltinStates(map[string]string{"container_memory": config.BuiltinStateOptedOut}))

	// A root that cannot be read withholds the layer: every key withheld.
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults: [\n")
	if _, _, err := m.diffAndReload(); err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}
	withheld := map[string]string{}
	for _, k := range config.BuiltinDefaultKeys() {
		withheld[k] = config.BuiltinStateWithheld
	}
	assertBuiltinStates(t, "after the root breaks", builtinStateGauge(t, fresh), withheld)
}

// A tree with no root carrier reloads through the flat incremental path. Its
// tenant-only patch keeps the previous commit's Defaults, so it must keep
// what the built-in layer did to them: every key builtin, not overridden.
func TestBuiltinStateGauge_FlatIncrementalReload(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "t1.yaml"), "tenants:\n  t1:\n    redis_connected_clients: \"5\"\n")
	writeTestYAML(t, filepath.Join(dir, "_ops.yaml"), "state_filters: {}\n")
	m, fresh := newGaugedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	all := wantBuiltinStates(nil)
	assertBuiltinStates(t, "cold load", builtinStateGauge(t, fresh), all)

	writeTestYAML(t, filepath.Join(dir, "t1.yaml"), "tenants:\n  t1:\n    redis_connected_clients: \"6\"\n")
	m.tickOnce()
	if got := m.GetConfig().Tenants["t1"]["redis_connected_clients"].Default; got != "6" {
		t.Fatalf("the tenant-only change was not committed (redis_connected_clients=%q)", got)
	}
	assertBuiltinStates(t, "after a tenant-only change (patch branch)", builtinStateGauge(t, fresh), all)

	writeTestYAML(t, filepath.Join(dir, "_ops.yaml"), "# touched\nstate_filters: {}\n")
	m.tickOnce()
	assertBuiltinStates(t, "after a platform-file change (rebuild branch)", builtinStateGauge(t, fresh), all)

	// A dangling root carrier appears: the layer is withheld on the next
	// tenant-only tick.
	if err := os.Symlink("nowhere.yaml", filepath.Join(dir, "_defaults.yaml")); err != nil {
		t.Skipf("symlink: %v", err)
	}
	writeTestYAML(t, filepath.Join(dir, "t1.yaml"), "tenants:\n  t1:\n    redis_connected_clients: \"7\"\n")
	m.tickOnce()
	withheld := map[string]string{}
	for _, k := range config.BuiltinDefaultKeys() {
		withheld[k] = config.BuiltinStateWithheld
	}
	assertBuiltinStates(t, "after a dangling root carrier appears", builtinStateGauge(t, fresh), withheld)
}

func TestBuiltinStateGauge_SingleFileMode(t *testing.T) {
	t.Parallel()
	f := filepath.Join(t.TempDir(), "single.yaml")
	writeTestYAML(t, f, "defaults:\n  container_memory: 70\n  pg_replication_lag: \"disable\"\ntenants:\n  t1: {}\n")
	m, fresh := newGaugedManager(t, f)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	assertBuiltinStates(t, "single file", builtinStateGauge(t, fresh), wantBuiltinStates(map[string]string{
		"container_memory":   config.BuiltinStateOverridden,
		"pg_replication_lag": config.BuiltinStateOptedOut,
	}))
}

// da_config_builtin_defaults_info is one series, 1, labelled with the digest
// of the table this binary was compiled with.
func TestBuiltinDefaultsInfo_IsTheCompiledDigest(t *testing.T) {
	t.Parallel()
	fresh, reg := freshMetrics(t)
	if got := testutil.ToFloat64(fresh.set.BuiltinDefaultsInfo.WithLabelValues(config.BuiltinDefaultsDigest())); got != 1 {
		t.Fatalf("da_config_builtin_defaults_info{digest=%q} = %v, want 1", config.BuiltinDefaultsDigest(), got)
	}
	n, err := testutil.GatherAndCount(reg, "da_config_builtin_defaults_info")
	if err != nil || n != 1 {
		t.Fatalf("da_config_builtin_defaults_info: %d series (err %v), want 1", n, err)
	}
}
