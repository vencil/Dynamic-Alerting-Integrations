package config

// loaddir_undeliverable_test.go — #1976: a key only a subtree `_defaults.yaml`
// names (not the root `_defaults.yaml`, not `optional_overrides:`) is one the
// build cannot deliver (FlatBuild.Unreachable). LoadReport.Undeliverable and
// ScopedTenants.Undeliverable carry that build's verdict to the readers
// outside package main; the control is the same tree with the key declared
// at the root, which delivers it and reports nothing.
//
// Seams: none — t.TempDir() trees, nil (discarding) loggers.

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func writeUndeliverableTree(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for name, body := range files {
		p := filepath.Join(dir, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// undeliverableTree: tenant-a inherits redis_evicted_keys from finance/ only.
// rootDeclares adds the key to the root defaults (the control).
func undeliverableTree(t *testing.T, rootDeclares bool) string {
	root := "defaults:\n  mysql_connections: 80\n"
	if rootDeclares {
		root += "  redis_evicted_keys: 500\n"
	}
	return writeUndeliverableTree(t, map[string]string{
		"_defaults.yaml":         root,
		"finance/_defaults.yaml": "defaults:\n  mysql_connections: 60\n  redis_evicted_keys: 100\n",
		"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
		"ops/tenant-b.yaml":      "tenants:\n  tenant-b: {}\n",
	})
}

func TestLoadDirReport_SubtreeOnlyKeyIsUndeliverable(t *testing.T) {
	t.Parallel()
	cfg, rep, err := LoadDirReport(undeliverableTree(t, false), nil)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]map[string]ScheduledValue{
		"tenant-a": {"redis_evicted_keys": {Default: "100"}},
	}
	if !reflect.DeepEqual(rep.Undeliverable, want) {
		t.Errorf("Undeliverable = %#v, want %#v", rep.Undeliverable, want)
	}
	// The build leaves the key out of the tenant's map (nothing would read
	// it there) while the deliverable sibling key is overlaid as before.
	if _, has := cfg.Tenants["tenant-a"]["redis_evicted_keys"]; has {
		t.Errorf("tenant-a map carries the undeliverable key: %v", cfg.Tenants["tenant-a"])
	}
	if got := cfg.Tenants["tenant-a"]["mysql_connections"].Default; got != "60" {
		t.Errorf("tenant-a mysql_connections = %q, want the subtree's 60", got)
	}
}

func TestLoadDirReport_RootDeclaredKeyIsDelivered(t *testing.T) {
	t.Parallel()
	cfg, rep, err := LoadDirReport(undeliverableTree(t, true), nil)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Undeliverable != nil {
		t.Errorf("Undeliverable = %#v, want nil (the root declares the key)", rep.Undeliverable)
	}
	if got := cfg.Tenants["tenant-a"]["redis_evicted_keys"].Default; got != "100" {
		t.Errorf("tenant-a redis_evicted_keys = %q, want the subtree's 100", got)
	}
}

// The value is the one the deepest level naming the key hands down — the
// value the tenant's effective config shows.
func TestLoadDirReport_UndeliverableValueIsTheDeepestLevels(t *testing.T) {
	t.Parallel()
	dir := writeUndeliverableTree(t, map[string]string{
		"_defaults.yaml":            "defaults:\n  mysql_connections: 80\n",
		"finance/_defaults.yaml":    "defaults:\n  redis_evicted_keys: 100\n",
		"finance/us/_defaults.yaml": "defaults:\n  redis_evicted_keys: 70\n",
		"finance/us/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
	})
	_, rep, err := LoadDirReport(dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	if got := rep.Undeliverable["tenant-a"]["redis_evicted_keys"].Default; got != "70" {
		t.Errorf("Undeliverable value = %q, want the deepest level's 70; report %#v", got, rep.Undeliverable)
	}
}

// LoadReport.Undeliverable is the build's own map, not a second judgement:
// the same keys as FlatBuild.Unreachable on the same scan.
func TestLoadDirReport_UndeliverableIsTheBuildsUnreachable(t *testing.T) {
	t.Parallel()
	dir := undeliverableTree(t, false)
	_, rep, err := LoadDirReport(dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	scan, err := ScanDirTree(dir, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	built, err := loadDirBuild(scan, dir, discardLogger, nil)
	if err != nil {
		t.Fatal(err)
	}
	if got := unreachableKeys(rep.Undeliverable); !reflect.DeepEqual(got, built.Unreachable) {
		t.Errorf("LoadReport keys %v, FlatBuild.Unreachable %v", got, built.Unreachable)
	}
}

func TestScopeEffective_UndeliverableKeepsInScopeTenants(t *testing.T) {
	t.Parallel()
	dir := undeliverableTree(t, false)
	want := map[string][]string{"tenant-a": {"redis_evicted_keys"}}
	for _, tc := range []struct {
		scope string
		want  map[string][]string
	}{
		{scope: "", want: want},
		{scope: "finance", want: want},
		{scope: "ops", want: nil}, // tenant-a is outside the scope
	} {
		scoped, err := ScopeEffective(dir, tc.scope)
		if err != nil {
			t.Fatalf("scope %q: %v", tc.scope, err)
		}
		if !reflect.DeepEqual(scoped.Undeliverable, tc.want) {
			t.Errorf("scope %q: Undeliverable = %#v, want %#v", tc.scope, scoped.Undeliverable, tc.want)
		}
	}
	scoped, err := ScopeEffective(undeliverableTree(t, true), "")
	if err != nil {
		t.Fatal(err)
	}
	if scoped.Undeliverable != nil {
		t.Errorf("root declares the key: Undeliverable = %#v, want nil", scoped.Undeliverable)
	}
}
