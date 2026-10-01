package main

// #2153 (D): da_config_max_tenants_per_file, da_config_max_mapping_keys and
// da_config_initial_load_duration_seconds. Every test observes through the
// metrics seam (freshMetrics + SetMetrics, test-map.md) — never the
// package singleton.

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

// tenantsYAML renders one `tenants:` file: n tenants named <prefix>-<i>,
// each with `keys` override keys.
func tenantsYAML(prefix string, n, keys int) string {
	var b strings.Builder
	b.WriteString("tenants:\n")
	for i := 0; i < n; i++ {
		fmt.Fprintf(&b, "  %s-%d:\n", prefix, i)
		for k := 0; k < keys; k++ {
			fmt.Fprintf(&b, "    key_%d: \"%d\"\n", k, k+1)
		}
	}
	return b.String()
}

func shapeGauges(fresh *configMetrics) (tenants, keys float64) {
	return testutil.ToFloat64(fresh.maxTenantsPerFile), testutil.ToFloat64(fresh.maxMappingKeys)
}

func TestConfigShapeOf_PerMapping(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name        string
		cfg         ThresholdConfig
		wantTenants int
		wantKeys    int
	}{
		{name: "empty", cfg: ThresholdConfig{}},
		{
			name: "tenants block is the widest mapping",
			cfg: ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{
				"t1": {"a": {}}, "t2": {"a": {}}, "t3": {"a": {}},
			}},
			wantTenants: 3, wantKeys: 3,
		},
		{
			name: "one tenant's overrides are wider than the tenants block",
			cfg: ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{
				"t1": {"a": {}, "b": {}, "c": {}, "d": {}, "e": {}},
				"t2": {"a": {}},
			}},
			wantTenants: 2, wantKeys: 5,
		},
		{
			name: "defaults block",
			cfg: ThresholdConfig{
				Defaults: map[string]float64{"a": 1, "b": 2, "c": 3, "d": 4},
				Tenants:  map[string]map[string]ScheduledValue{"t1": {"a": {}}},
			},
			wantTenants: 1, wantKeys: 4,
		},
		{
			name: "one profile",
			cfg: ThresholdConfig{Profiles: map[string]map[string]ScheduledValue{
				"gold": {"a": {}, "b": {}, "c": {}, "d": {}, "e": {}, "f": {}},
			}},
			wantTenants: 0, wantKeys: 6,
		},
		{
			name: "state_filters block",
			cfg: ThresholdConfig{StateFilters: map[string]StateFilter{
				"f1": {}, "f2": {},
			}},
			wantTenants: 0, wantKeys: 2,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := configShapeOf(&tc.cfg)
			if got.maxTenantsPerFile != tc.wantTenants || got.maxMappingKeys != tc.wantKeys {
				t.Errorf("configShapeOf = {tenants %d, keys %d}, want {tenants %d, keys %d}",
					got.maxTenantsPerFile, got.maxMappingKeys, tc.wantTenants, tc.wantKeys)
			}
		})
	}
}

// The maxima are taken across files independently: the widest `tenants:`
// block and the widest single mapping may come from different files.
func TestConfigShapeOfFiles_MaxAcrossFiles(t *testing.T) {
	t.Parallel()
	got := configShapeOfFiles(map[string]ThresholdConfig{
		"a.yaml": {Tenants: map[string]map[string]ScheduledValue{
			"t1": {"a": {}}, "t2": {"a": {}}, "t3": {"a": {}}, "t4": {"a": {}},
		}},
		"b.yaml": {Tenants: map[string]map[string]ScheduledValue{
			"t5": {"a": {}, "b": {}, "c": {}, "d": {}, "e": {}, "f": {}, "g": {}},
		}},
	})
	if got.maxTenantsPerFile != 4 || got.maxMappingKeys != 7 {
		t.Errorf("configShapeOfFiles = {tenants %d, keys %d}, want {tenants 4, keys 7}",
			got.maxTenantsPerFile, got.maxMappingKeys)
	}
}

// Directory mode end to end: cold Load publishes the maxima over every file,
// and a reload that shrinks the tree moves the gauges DOWN (they are re-Set
// on each commit, not ratcheted).
func TestLoad_DirMode_PublishesConfigShape(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  key_0: 10\n  key_1: 20\n")
	writeTestYAML(t, filepath.Join(dir, "shared.yaml"), tenantsYAML("shared", 12, 2))
	nested := filepath.Join(dir, "team-a")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	writeTestYAML(t, filepath.Join(nested, "wide.yaml"), tenantsYAML("wide", 1, 30))

	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetMetrics(fresh)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if tenants, keys := shapeGauges(fresh); tenants != 12 || keys != 30 {
		t.Fatalf("after Load: max_tenants_per_file=%v max_mapping_keys=%v, want 12 and 30", tenants, keys)
	}

	// Shrink both axes and reload through the hierarchical reload path.
	writeTestYAML(t, filepath.Join(dir, "shared.yaml"), tenantsYAML("shared", 5, 2))
	writeTestYAML(t, filepath.Join(nested, "wide.yaml"), tenantsYAML("wide", 1, 3))
	if _, _, err := m.diffAndReload(); err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}
	if tenants, keys := shapeGauges(fresh); tenants != 5 || keys != 5 {
		t.Fatalf("after reload: max_tenants_per_file=%v max_mapping_keys=%v, want 5 and 5", tenants, keys)
	}
}

// Flat directory mode (no `_defaults.yaml`) reloads through incrementalLoadFrom,
// whose commit carries the unchanged files' cached partials — an untouched
// file must still count.
func TestIncrementalLoad_PublishesConfigShape(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "big.yaml"), tenantsYAML("big", 9, 1))
	writeTestYAML(t, filepath.Join(dir, "small.yaml"), tenantsYAML("small", 2, 1))

	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetMetrics(fresh)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	writeTestYAML(t, filepath.Join(dir, "small.yaml"), tenantsYAML("small", 3, 1))
	if err := watchReload(m); err != nil {
		t.Fatalf("reload: %v", err)
	}
	if tenants, keys := shapeGauges(fresh); tenants != 9 || keys != 9 {
		t.Fatalf("after reload: max_tenants_per_file=%v max_mapping_keys=%v, want 9 and 9", tenants, keys)
	}
}

// Single-file mode measures the file as written, before ApplyProfiles folds
// the profile's keys into each tenant map.
func TestLoad_SingleFile_PublishesConfigShapeBeforeProfiles(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "config.yaml")
	writeTestYAML(t, path, `defaults:
  key_0: 10
profiles:
  gold:
    p_0: "1"
    p_1: "2"
    p_2: "3"
    p_3: "4"
tenants:
  single-0:
    _profile: gold
    key_0: "5"
  single-1:
    key_0: "6"
`)
	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(path, 0)
	m.SetMetrics(fresh)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	// Widest mapping as written: the gold profile (4). After ApplyProfiles
	// single-0 would hold 6 keys; that must not be what is reported.
	if tenants, keys := shapeGauges(fresh); tenants != 2 || keys != 4 {
		t.Fatalf("max_tenants_per_file=%v max_mapping_keys=%v, want 2 and 4", tenants, keys)
	}
}

// Directory mode with a profile: the per-file partials the gauge reads must
// not carry the profile's keys that ApplyProfiles folds into the MERGED
// config — the tenant file as written has 1 key, the profile 4.
func TestLoad_DirMode_ConfigShapeIgnoresProfileExpansion(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_profiles.yaml"), `profiles:
  gold:
    p_0: "1"
    p_1: "2"
    p_2: "3"
    p_3: "4"
`)
	writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  prof-0:\n    _profile: gold\n")

	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetMetrics(fresh)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if tenants, keys := shapeGauges(fresh); tenants != 1 || keys != 4 {
		t.Fatalf("max_tenants_per_file=%v max_mapping_keys=%v, want 1 and 4", tenants, keys)
	}
}

// LoadInitial times the startup load; plain Load (the watch loop's
// single-file reload) must not touch the gauge.
func TestLoadInitial_RecordsInitialLoadDuration(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "a.yaml"), tenantsYAML("init", 3, 1))

	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetMetrics(fresh)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := testutil.ToFloat64(fresh.initialLoadDuration); got != 0 {
		t.Fatalf("plain Load set initial_load_duration to %v, want it untouched (0)", got)
	}

	fresh2, _ := freshMetrics(t)
	m2 := NewConfigManagerWithDebounce(dir, 0)
	m2.SetMetrics(fresh2)
	if err := m2.LoadInitial(); err != nil {
		t.Fatalf("LoadInitial: %v", err)
	}
	if got := testutil.ToFloat64(fresh2.initialLoadDuration); got <= 0 {
		t.Fatalf("initial_load_duration = %v after LoadInitial, want > 0", got)
	}
	if !m2.IsLoaded() {
		t.Fatal("LoadInitial did not load the config")
	}
}

// A failed startup load leaves the gauge at 0 (the process exits anyway;
// the gauge must not claim a load that did not happen).
func TestLoadInitial_FailureLeavesGaugeUnset(t *testing.T) {
	t.Parallel()
	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(filepath.Join(t.TempDir(), "missing.yaml"), 0)
	m.SetMetrics(fresh)
	if err := m.LoadInitial(); err == nil {
		t.Fatal("LoadInitial on a missing file returned nil error")
	}
	if got := testutil.ToFloat64(fresh.initialLoadDuration); got != 0 {
		t.Fatalf("initial_load_duration = %v after a failed load, want 0", got)
	}
}

// The appended buckets (#2153) are present, so a minutes-long reload lands
// in a finite bucket instead of +Inf.
func TestDurationHistograms_CoverMinuteScaleLoads(t *testing.T) {
	t.Parallel()
	fresh, reg := freshMetrics(t)
	fresh.reloadDuration.Observe(200) // 3m20s
	fresh.scanDuration.Observe(90)

	families, err := reg.Gather()
	if err != nil {
		t.Fatal(err)
	}
	// name → upper bound → cumulative count
	buckets := map[string]map[float64]uint64{}
	for _, mf := range families {
		for _, metric := range mf.GetMetric() {
			h := metric.GetHistogram()
			if h == nil {
				continue
			}
			bs := map[float64]uint64{}
			for _, b := range h.GetBucket() {
				bs[b.GetUpperBound()] = b.GetCumulativeCount()
			}
			buckets[mf.GetName()] = bs
		}
	}
	check := func(name string, le float64, want uint64) {
		t.Helper()
		got, ok := buckets[name][le]
		if !ok {
			t.Errorf("%s has no le=%v bucket", name, le)
			return
		}
		if got != want {
			t.Errorf("%s{le=%v} = %d, want %d", name, le, got, want)
		}
	}
	check("da_config_reload_duration_seconds", 30, 0)
	check("da_config_reload_duration_seconds", 300, 1)
	check("da_config_reload_duration_seconds", 600, 1)
	check("da_config_scan_duration_seconds", 60, 0)
	check("da_config_scan_duration_seconds", 120, 1)
}
