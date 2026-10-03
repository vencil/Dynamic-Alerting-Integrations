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

// #1976 r2: reserved / `_` keys refused in a subtree are #2388's, not this
// report's. The shapes below are measured in FlatBuild.Unreachable (the
// exporter's ERROR and gauge, unchanged) and must not reach
// LoadReport.Undeliverable or ScopedTenants.Undeliverable — both pass through
// undeliverableThresholds.

// reservedOnlyRoot / reservedOnlySubtree: a subtree `_defaults.yaml` holding
// only `_` keys — `_metadata` and a scheduled `_state_maintenance` (both in
// the root's optional_overrides), `_profile`, and `_silent_bogus`, which
// IsReservedKey does not recognise but the build refuses too.
const reservedOnlyRoot = "defaults:\n  mysql_connections: 80\n" +
	"state_filters:\n  maintenance:\n    reasons: [\"x\"]\n    severity: warning\n" +
	"optional_overrides:\n  - _state_maintenance\n  - _metadata\n"

const reservedOnlySubtree = "defaults:\n  _metadata: 5\n" +
	"  _state_maintenance:\n    default: enable\n    overrides:\n" +
	"      - window: \"00:00-23:59\"\n        value: disable\n" +
	"  _profile: disable\n  _silent_bogus: 5\n"

// scheduleThenScalarStateTree: `_state_maintenance` as a schedule at
// finance/ (refused) and a scalar `disable` at finance/us/ (delivered). The
// shallow refusal stays in FlatBuild.Unreachable although the key is
// delivered.
func scheduleThenScalarStateTree(t *testing.T) string {
	t.Helper()
	return writeUndeliverableTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n" +
			"state_filters:\n  maintenance:\n    reasons: [\"x\"]\n    severity: warning\n",
		"finance/_defaults.yaml": "defaults:\n  _state_maintenance:\n    default: enable\n" +
			"    overrides:\n      - window: \"00:00-23:59\"\n        value: disable\n",
		"finance/us/_defaults.yaml": "defaults:\n  _state_maintenance: disable\n",
		"finance/us/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
	})
}

// buildUnreachable is the build's own, unfiltered set for dir.
func buildUnreachable(t *testing.T, dir string) map[string][]string {
	t.Helper()
	scan, err := ScanDirTree(dir, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	built, err := loadDirBuild(scan, dir, discardLogger, nil)
	if err != nil {
		t.Fatal(err)
	}
	return built.Unreachable
}

func TestUndeliverable_ReservedKeysAreLeftOut(t *testing.T) {
	t.Parallel()
	if IsReservedKey("_silent_bogus") {
		t.Fatal("precondition: IsReservedKey(_silent_bogus) is true; the `_` prefix test is no longer the wider one")
	}
	for _, tc := range []struct {
		name         string
		dir          string
		wantUnreach  []string // tenant-a's FlatBuild.Unreachable (precondition)
		wantReported map[string][]string
	}{
		{
			name: "reserved-only",
			dir: writeUndeliverableTree(t, map[string]string{
				"_defaults.yaml":         reservedOnlyRoot,
				"finance/_defaults.yaml": reservedOnlySubtree,
				"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
			}),
			// `_metadata` and `_state_maintenance` are in optional_overrides,
			// which the build counts as declared — the shape in which the
			// root-declaration advice would silence the verdict without making
			// the subtree value take effect.
			wantUnreach: []string{"_profile", "_silent_bogus"},
		},
		{
			name:        "schedule-then-scalar-state",
			dir:         scheduleThenScalarStateTree(t),
			wantUnreach: []string{"_state_maintenance"},
		},
		{
			// A threshold key beside the `_` ones: the filter is per key.
			name: "mixed",
			dir: writeUndeliverableTree(t, map[string]string{
				"_defaults.yaml":         reservedOnlyRoot,
				"finance/_defaults.yaml": reservedOnlySubtree + "  redis_evicted_keys: 100\n",
				"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
			}),
			wantUnreach:  []string{"_profile", "_silent_bogus", "redis_evicted_keys"},
			wantReported: map[string][]string{"tenant-a": {"redis_evicted_keys"}},
		},
	} {
		if got := buildUnreachable(t, tc.dir)["tenant-a"]; !reflect.DeepEqual(got, tc.wantUnreach) {
			t.Fatalf("%s: precondition: FlatBuild.Unreachable[tenant-a] = %v, want %v", tc.name, got, tc.wantUnreach)
		}
		_, rep, err := LoadDirReport(tc.dir, nil)
		if err != nil {
			t.Fatal(err)
		}
		if got := unreachableKeys(rep.Undeliverable); !reflect.DeepEqual(got, tc.wantReported) {
			t.Errorf("%s: LoadReport.Undeliverable keys = %v, want %v", tc.name, got, tc.wantReported)
		}
		scoped, err := ScopeEffective(tc.dir, "")
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(scoped.Undeliverable, tc.wantReported) {
			t.Errorf("%s: ScopedTenants.Undeliverable = %v, want %v", tc.name, scoped.Undeliverable, tc.wantReported)
		}
	}
}

// #1976 r2 (F4/F5): the value is the deepest level that writes the key in a
// THRESHOLD shape — a deeper non-threshold value (a YAML bool) is skipped —
// and it is the build's normalised rendering, not the file's text.
func TestLoadDirReport_UndeliverableValueShape(t *testing.T) {
	t.Parallel()
	dir := writeUndeliverableTree(t, map[string]string{
		"_defaults.yaml":            "defaults:\n  mysql_connections: 80\n",
		"finance/_defaults.yaml":    "defaults:\n  redis_evicted_keys: 100\n  redis_x: 1e6\n",
		"finance/us/_defaults.yaml": "defaults:\n  redis_evicted_keys: true\n",
		"finance/us/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
	})
	_, rep, err := LoadDirReport(dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	got := rep.Undeliverable["tenant-a"]
	if v := got["redis_evicted_keys"].Default; v != "100" {
		t.Errorf("redis_evicted_keys = %q, want 100 (the deeper `true` is not threshold-shaped)", v)
	}
	if v := got["redis_x"].Default; v != "1e+06" {
		t.Errorf("redis_x = %q, want the normalised \"1e+06\" (the file says 1e6)", v)
	}
}
