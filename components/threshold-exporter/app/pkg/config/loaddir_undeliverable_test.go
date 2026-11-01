package config

// loaddir_undeliverable_test.go — #1976: a key only a subtree `_defaults.yaml`
// names (not the root `_defaults.yaml`, not `optional_overrides:`) is one the
// build cannot deliver (FlatBuild.Undeliverable). LoadReport.Undeliverable and
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
// FlatBuild.Undeliverable on the same scan, values included (#3379).
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
	if rep.Undeliverable == nil || !reflect.DeepEqual(rep.Undeliverable, built.Undeliverable) {
		t.Errorf("LoadReport %v, FlatBuild.Undeliverable %v", rep.Undeliverable, built.Undeliverable)
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
		scoped, err := ScopeEffective(dir, tc.scope, fixedNow)
		if err != nil {
			t.Fatalf("scope %q: %v", tc.scope, err)
		}
		if !reflect.DeepEqual(scoped.Undeliverable, tc.want) {
			t.Errorf("scope %q: Undeliverable = %#v, want %#v", tc.scope, scoped.Undeliverable, tc.want)
		}
	}
	scoped, err := ScopeEffective(undeliverableTree(t, true), "", fixedNow)
	if err != nil {
		t.Fatal(err)
	}
	if scoped.Undeliverable != nil {
		t.Errorf("root declares the key: Undeliverable = %#v, want nil", scoped.Undeliverable)
	}
}

// #1976 r2/r3: the report keeps only keys for which "declare it at the
// root or in optional_overrides:" is the fix (undeliverableThresholds). The
// shapes below are checked in FlatBuild.Undeliverable (the exporter's ERROR
// and gauge read it since #3379) and FlatBuild.DisabledUndeclared (the
// exporter's WARN), then in LoadReport.Undeliverable and
// ScopedTenants.Undeliverable — all four from the one build.
// Each exclusion has a case only it catches, so dropping any one of them
// turns a case red: routing-key (subtreeDefaultsRefusedKey — since #3341 a
// subtree hands no other `_` key down, so a routing key is the only `_` key
// that can be unreachable), switched-off (disabledEverywhere). The `_` keys a
// subtree does not carry are in no unreachable set at all (reserved-declared,
// reserved-keys, silent-bogus, unrecognised-underscore-key): da-guard's
// subtree_default_reserved_key error reports them.

// reservedOnlyRoot / reservedOnlySubtree: `_metadata` and a scheduled
// `_state_maintenance` (both in the root's optional_overrides), `_profile`,
// and `_silent_bogus`, which IsReservedKey does not recognise.
const reservedOnlyRoot = "defaults:\n  mysql_connections: 80\n" +
	"state_filters:\n  maintenance:\n    reasons: [\"x\"]\n    severity: warning\n" +
	"optional_overrides:\n  - _state_maintenance\n  - _metadata\n"

const reservedOnlySubtree = "defaults:\n  _metadata: 5\n" +
	"  _state_maintenance:\n    default: enable\n    overrides:\n" +
	"      - window: \"00:00-23:59\"\n        value: disable\n" +
	"  _profile: disable\n  _silent_bogus: 5\n"

const plainRoot = "defaults:\n  mysql_connections: 80\n"

// scheduleThenScalarStateFiles: `_state_maintenance` as a schedule at
// finance/ and a scalar `disable` at finance/us/. Before #3295 the shallow
// schedule was refused as unreachable and stayed in the exporter's set
// although the deeper `disable` was delivered; since #3341 a subtree carries
// neither, so it is in no unreachable set at all.
func scheduleThenScalarStateFiles() map[string]string {
	return map[string]string{
		"_defaults.yaml": plainRoot +
			"state_filters:\n  maintenance:\n    reasons: [\"x\"]\n    severity: warning\n",
		"finance/_defaults.yaml": "defaults:\n  _state_maintenance:\n    default: enable\n" +
			"    overrides:\n      - window: \"00:00-23:59\"\n        value: disable\n",
		"finance/us/_defaults.yaml": "defaults:\n  _state_maintenance: disable\n",
		"finance/us/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
	}
}

// subtreeTree is a root, one finance/ subtree defaults file and tenant-a
// under it.
func subtreeTree(root, subtree string) map[string]string {
	return map[string]string{
		"_defaults.yaml":         root,
		"finance/_defaults.yaml": subtree,
		"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
	}
}

// buildUnreachable is the build's own two answers for dir: the undeliverable
// keys (the exporter's ERROR and gauge) and the switched-off undeclared keys
// (its WARN).
func buildUnreachable(t *testing.T, dir string) (undeliverable, disabled map[string][]string) {
	t.Helper()
	scan, err := ScanDirTree(dir, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	built, err := loadDirBuild(scan, dir, discardLogger, nil)
	if err != nil {
		t.Fatal(err)
	}
	return built.UndeliverableKeys(), built.DisabledUndeclared
}

func TestUndeliverable_ReportedKeys(t *testing.T) {
	t.Parallel()
	if IsReservedKey("_silent_bogus") || IsReservedKey("_myth2") {
		t.Fatal("precondition: IsReservedKey accepts _silent_bogus or _myth2; the cases below no longer separate the exclusions")
	}
	for _, tc := range []struct {
		name         string
		files        map[string]string
		wantDisabled []string // tenant-a's FlatBuild.DisabledUndeclared (the exporter's WARN)
		wantReported []string // tenant-a's reported keys (exporter and da-guard); nil = tenant absent
	}{
		{
			// #3341: a subtree carries none of these, listed in
			// optional_overrides or not, so the overlay asks no reachability
			// question of them (before, `_profile` / `_silent_bogus` were
			// unreachable here).
			name:  "reserved-declared",
			files: subtreeTree(reservedOnlyRoot, reservedOnlySubtree),
		},
		{
			name:  "reserved-keys",
			files: subtreeTree(plainRoot, "defaults:\n  _metadata: 5\n  _namespaces: 3\n"),
		},
		{
			name:  "silent-bogus",
			files: subtreeTree(plainRoot, "defaults:\n  _silent_bogus: 5\n"),
		},
		{
			// Only subtreeDefaultsRefusedKey leaves it out: a routing key a
			// subtree still hands down (the route generator's checks own
			// it), unreachable — `ResolveRouting` reads only `_routing` — and
			// declaring it at the root is no fix. Since #3379 the exporter
			// does not count it either, and a disabled one is no WARN.
			name:  "routing-key",
			files: subtreeTree(plainRoot, "defaults:\n  _routing_bogus: 5\n  _routing_off: disable\n"),
		},
		{
			name:  "schedule-then-scalar-state",
			files: scheduleThenScalarStateFiles(),
		},
		{
			// Only disabledEverywhere leaves `redis_evicted_keys` out. A
			// schedule disabled by default but numeric in a window serves a
			// row then, so `redis_s` is reported; the other values are plain.
			name: "switched-off",
			// `redis_u` / `redis_v` pin the case / space normalisation the
			// resolver applies before isDisabled.
			files: subtreeTree(plainRoot, "defaults:\n  redis_evicted_keys: disable\n"+
				"  redis_q: \"1e6\"\n  redis_r: 1e6\n"+
				"  redis_s:\n    default: disable\n    overrides:\n"+
				"      - window: \"01:00-02:00\"\n        value: 50\n"+
				"  redis_u: Disable\n  redis_v: \" DISABLE \"\n"),
			wantDisabled: []string{"redis_evicted_keys", "redis_u", "redis_v"},
			wantReported: []string{"redis_q", "redis_r", "redis_s"},
		},
		{
			// A `_` key the registry does not list is no threshold to the
			// exporter in any layer (#3045: the root's `_myth` serves no
			// row), and a subtree does not carry it (#3341): in no
			// unreachable set — subtreeReservedKeys (#2388) reports it, as
			// `_silent_bogus`. Before #3045 it was reported here.
			name:  "unrecognised-underscore-key",
			files: subtreeTree(plainRoot+"  _myth: 5\n", "defaults:\n  _myth2: 7\n"),
		},
		{
			// The finding's fix applied: declared at the root, nothing is
			// unreachable and nothing reported.
			name: "unrecognised-underscore-key-declared",
			files: subtreeTree(plainRoot+"  _myth2: 1\n  _silent_bogus: 1\n",
				"defaults:\n  _myth2: 7\n  _silent_bogus: 7\n"),
		},
		{
			// A threshold key beside the `_` ones: the filter is per key.
			name:         "mixed",
			files:        subtreeTree(reservedOnlyRoot, reservedOnlySubtree+"  redis_evicted_keys: 100\n"),
			wantReported: []string{"redis_evicted_keys"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := writeUndeliverableTree(t, tc.files)
			var want map[string][]string
			if tc.wantReported != nil {
				want = map[string][]string{"tenant-a": tc.wantReported}
			}
			undeliverable, disabled := buildUnreachable(t, dir)
			if !reflect.DeepEqual(undeliverable, want) {
				t.Errorf("FlatBuild.UndeliverableKeys = %v, want %v", undeliverable, want)
			}
			if got := disabled["tenant-a"]; !reflect.DeepEqual(got, tc.wantDisabled) {
				t.Errorf("FlatBuild.DisabledUndeclared[tenant-a] = %v, want %v", got, tc.wantDisabled)
			}
			_, rep, err := LoadDirReport(dir, nil)
			if err != nil {
				t.Fatal(err)
			}
			if got := unreachableKeys(rep.Undeliverable); !reflect.DeepEqual(got, want) {
				t.Errorf("LoadReport.Undeliverable keys = %v, want %v", got, want)
			}
			scoped, err := ScopeEffective(dir, "", fixedNow)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(scoped.Undeliverable, want) {
				t.Errorf("ScopedTenants.Undeliverable = %v, want %v", scoped.Undeliverable, want)
			}
		})
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

// ADR-038 §11: a dimensional key a subtree `_defaults.yaml` writes reaches
// the output plane only when the platform names its base, as the resolver
// asks (dimensionalBaseNamed) — else it is undeliverable like a flat key,
// not overlaid into a map whose resolver then serves it no row.
func TestLoadDirReport_SubtreeDimensionalKeyNeedsItsBaseNamed(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		root        string
		deliverable bool
	}{
		"base not named": {"defaults:\n  mysql_connections: 80\n", false},
		"base valued":    {"defaults:\n  mysql_connections: 80\n  redis_mem: 70\n", true},
		"base declared":  {"defaults:\n  mysql_connections: 80\noptional_overrides:\n  - redis_mem\n", true},
		"retired spelling of the base valued": {
			"defaults:\n  mysql_cpu: 30\n", true},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			key := `redis_mem{db="0"}`
			if name == "retired spelling of the base valued" {
				key = `mysql_threads_running{db="0"}`
			}
			cfg, rep, err := LoadDirReport(writeUndeliverableTree(t, map[string]string{
				"_defaults.yaml":      tc.root,
				"team/_defaults.yaml": "defaults:\n  '" + key + "': 60\n",
				"team/t1.yaml":        "tenants:\n  t1: {}\n",
			}), nil)
			if err != nil {
				t.Fatal(err)
			}
			_, undeliverable := rep.Undeliverable["t1"][key]
			_, overlaid := cfg.Tenants["t1"][key]
			if undeliverable == tc.deliverable || overlaid != tc.deliverable {
				t.Errorf("undeliverable=%v overlaid=%v, want deliverable=%v (report %v)",
					undeliverable, overlaid, tc.deliverable, rep.Undeliverable)
			}
		})
	}
}
