package config

// builtin_defaults_test.go — the generated built-in table (#3832) pinned to
// the threshold registry, and its digest pinned to the table.

import (
	"math"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

// registryArmedDefaults is every registry key with `arming: default` and its
// value, read from the committed registry.
func registryArmedDefaults(t *testing.T) map[string]float64 {
	t.Helper()
	root := findRepoRootForRegistry(t)
	raw, err := os.ReadFile(filepath.Join(root, "rule-packs", "threshold-registry.yaml"))
	if err != nil {
		t.Fatalf("read threshold-registry.yaml: %v", err)
	}
	var doc struct {
		Keys map[string]struct {
			Arming string   `yaml:"arming"`
			Value  *float64 `yaml:"value"`
		} `yaml:"keys"`
	}
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal threshold-registry.yaml: %v", err)
	}
	out := map[string]float64{}
	for k, e := range doc.Keys {
		if e.Arming != "default" {
			continue
		}
		if e.Value == nil {
			t.Fatalf("registry key %q is arming: default without a value", k)
		}
		out[k] = *e.Value
	}
	return out
}

// The generated table equals the registry's default-armed keys, both ways: a
// key armed in the registry without `--regen` is missing here, and a key the
// registry disarmed or retired is still here.
func TestBuiltinDefaults_PinnedToRegistry(t *testing.T) {
	t.Parallel()
	want, got := registryArmedDefaults(t), BuiltinDefaults()
	if len(want) == 0 {
		t.Fatal("the registry arms no key: the pin would pass on an empty table")
	}
	for k, w := range want {
		g, ok := got[k]
		if !ok {
			t.Errorf("registry arms %q (value %v) but the built-in table has no such key — run check_threshold_registry.py --regen", k, w)
			continue
		}
		if g != w {
			t.Errorf("%q: built-in table %v, registry %v — run check_threshold_registry.py --regen", k, g, w)
		}
	}
	for k := range got {
		if _, ok := want[k]; !ok {
			t.Errorf("built-in table holds %q, which the registry does not arm — run check_threshold_registry.py --regen", k)
		}
	}
}

// Every built-in value is one the resolver serves, and no key is a spelling
// the platform asserts no value for.
func TestBuiltinDefaults_AreFiniteBaseKeys(t *testing.T) {
	t.Parallel()
	for k, v := range BuiltinDefaults() {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			t.Errorf("%q: value %v is not finite", k, v)
		}
		if strings.HasSuffix(k, "_critical") || strings.ContainsAny(k, "{}") || strings.HasPrefix(k, "_") {
			t.Errorf("%q: the built-in table holds base threshold keys only", k)
		}
	}
}

// The generated entries are sorted by key and name each key once: a repeat
// would be dropped without a word when the table is keyed.
func TestBuiltinDefaultEntries_AreSortedAndUnique(t *testing.T) {
	t.Parallel()
	for i := 1; i < len(builtinDefaultEntries); i++ {
		if prev, cur := builtinDefaultEntries[i-1].key, builtinDefaultEntries[i].key; prev >= cur {
			t.Errorf("entry %d: %q does not sort after %q", i, cur, prev)
		}
	}
	if len(BuiltinDefaults()) != len(builtinDefaultEntries) {
		t.Errorf("the keyed table has %d keys, the generated one %d entries", len(BuiltinDefaults()), len(builtinDefaultEntries))
	}
}

// The generated digest constant is the digest of the generated table: the
// generator and this binary compute it the same way.
func TestBuiltinDefaultsDigest_IsOfTheTable(t *testing.T) {
	t.Parallel()
	if got, want := BuiltinDefaultsDigest(), builtinDefaultsDigestOf(BuiltinDefaults()); got != want {
		t.Fatalf("generated digest %q, digest of the generated table %q", got, want)
	}
}

// The digest names the table's content: it moves with a value, with a key
// set, and not with map order; and it writes a number the way the generator
// does (no exponent, no trailing fraction).
func TestBuiltinDefaultsDigestOf(t *testing.T) {
	t.Parallel()
	base := map[string]float64{"fixture_a": 80, "fixture_b": 0.5}
	same := map[string]float64{"fixture_b": 0.5, "fixture_a": 80}
	if builtinDefaultsDigestOf(base) != builtinDefaultsDigestOf(same) {
		t.Error("the digest depends on map order")
	}
	for name, other := range map[string]map[string]float64{
		"a value changed": {"fixture_a": 81, "fixture_b": 0.5},
		"a key removed":   {"fixture_a": 80},
		"a key added":     {"fixture_a": 80, "fixture_b": 0.5, "fixture_c": 1},
		"a key renamed":   {"fixture_a2": 80, "fixture_b": 0.5},
	} {
		if builtinDefaultsDigestOf(base) == builtinDefaultsDigestOf(other) {
			t.Errorf("%s: the digest did not move", name)
		}
	}
	// sha256("fixture_a=80\nfixture_b=0.5\n")[:12], computed by the generator
	// (scripts/tools/ops/_registry_lib.py builtin_defaults_digest).
	const want = "04515815be57"
	if got := builtinDefaultsDigestOf(base); got != want {
		t.Errorf("digest of the fixture table = %q, want %q (the generator's)", got, want)
	}
	if got, want := builtinDefaultsDigestOf(map[string]float64{"fixture_small": 1e-7, "fixture_big": 1e20}), "ff25ac41be7f"; got != want {
		t.Errorf("digest of the extreme-value table = %q, want %q (the generator's)", got, want)
	}
}

func TestBuiltinDefault_MatchesTheKeyExactly(t *testing.T) {
	t.Parallel()
	for k, v := range BuiltinDefaults() {
		if got, ok := BuiltinDefault(k); !ok || got != v {
			t.Errorf("BuiltinDefault(%q) = %v, %v; want %v, true", k, got, ok, v)
		}
		for _, spelling := range []string{k + "_critical", k + `{fixture="x"}`, k + "_x"} {
			if _, ok := BuiltinDefault(spelling); ok {
				t.Errorf("BuiltinDefault(%q) found a value: only the exact key is in the table", spelling)
			}
		}
	}
	// BuiltinDefaults hands out a copy: writing to it changes nothing here.
	m := BuiltinDefaults()
	for k := range m {
		m[k] = -1
	}
	for k, v := range BuiltinDefaults() {
		if v == -1 {
			t.Errorf("%q: BuiltinDefaults returned the table itself, not a copy", k)
		}
	}
}

// No key of the built-in table may have a #1231 alias spelling: the chain's
// spelling machinery (dropShallowerSpellings, noteSpellingWriters) is not
// taught a level with no file, so a root writing the other spelling would
// not beat the built-in value. Arming an aliased key needs that taught first.
func TestBuiltinDefaults_NoAliasSpelling(t *testing.T) {
	for k := range builtinDefaults {
		if _, s := SpellingsOf(k); len(s) != 1 {
			t.Errorf("built-in key %q has the spellings %v", k, s)
		}
	}
}

// A refused root value (refusedDefault's NaN) and an absent key both fall
// to the built-in value; a finite root value, 0 included, stays.
func TestSeedBuiltinDefaults(t *testing.T) {
	var k string
	for k = range builtinDefaults {
		break
	}
	want := builtinDefaults[k]
	for name, tc := range map[string]struct {
		in   map[string]float64
		want float64
	}{
		"absent":            {map[string]float64{}, want},
		"refused unparsed":  {map[string]float64{k: refusedDefault(RefusedUnparsed)}, want},
		"refused nonfinite": {map[string]float64{k: refusedDefault(RefusedNonFinite)}, want},
		"root value":        {map[string]float64{k: 7}, 7},
		"root zero":         {map[string]float64{k: 0}, 0},
	} {
		seeded := seedBuiltinDefaults(tc.in)
		if got := tc.in[k]; got != tc.want {
			t.Errorf("%s: %s = %v, want %v", name, k, got, tc.want)
		}
		// #3832: the keys it reports seeding are exactly the ones it gave
		// the platform's value — FlatBuild.BuiltinSeeded, which da-guard's
		// builtin_defaults reads as `builtin`.
		if slices.Contains(seeded, k) != (tc.want == want && name != "root value" && name != "root zero") {
			t.Errorf("%s: seeded %v, %s listed = %v", name, seeded, k, slices.Contains(seeded, k))
		}
		if !slices.IsSorted(seeded) {
			t.Errorf("%s: seeded %v not sorted", name, seeded)
		}
		if len(tc.in) != len(builtinDefaults) && name == "absent" {
			t.Errorf("%s: %d keys seeded, want %d", name, len(tc.in), len(builtinDefaults))
		}
	}
}

// The tenant-api write check (mergeTenantConfig) lays the built-in layer as
// /metrics does: under a root with no value, and not under a root that
// cannot be read (does not parse, or is a dangling link).
func TestMergeTenantWithRootDefaults_BuiltinLayer(t *testing.T) {
	tenant := []byte("tenants:\n  t1:\n    pg_connections: \"70\"\n")
	for name, tc := range map[string]struct {
		root    func(dir string) error
		builtin bool
	}{
		"no root": {func(string) error { return nil }, true},
		"root valued": {func(d string) error {
			return os.WriteFile(filepath.Join(d, "_defaults.yaml"), []byte("defaults:\n  container_memory: 70\n"), 0o644)
		}, true},
		"root syntax": {func(d string) error {
			return os.WriteFile(filepath.Join(d, "_defaults.yaml"), []byte("defaults: [x\n"), 0o644)
		}, false},
		"root link": {func(d string) error { return os.Symlink("nowhere.yaml", filepath.Join(d, "_defaults.yaml")) }, false},
		"root refused": {func(d string) error {
			return os.WriteFile(filepath.Join(d, "_defaults.yaml"), []byte("defaults:\n  pg_connections: [1, 2]\n"), 0o644)
		}, false},
	} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			if err := tc.root(dir); err != nil {
				t.Skipf("setup: %v", err)
			}
			m := MergeTenantWithRootDefaults(dir, "t1", tenant)
			for k, v := range builtinDefaults {
				got, ok := m.Defaults[k]
				switch {
				case !tc.builtin && ok:
					t.Errorf("%s = %v laid over a root that cannot be read", k, got)
				case tc.builtin && k == "container_memory" && name == "root valued":
					if got != 70 {
						t.Errorf("%s = %v, want the root's 70", k, got)
					}
				case tc.builtin && got != v:
					t.Errorf("%s = %v (present %v), want the built-in %v", k, got, ok, v)
				}
			}
		})
	}
}

// BuiltinDefaultStates gives no answer without a build (#3832): da-guard
// writes `builtin_defaults: null` then, never an empty or invented table.
func TestBuiltinDefaultStates_NoBuildIsNoAnswer(t *testing.T) {
	if got := (*ScopedTenants)(nil).BuiltinDefaultStates(); got != nil {
		t.Errorf("nil ScopedTenants: %v, want nil", got)
	}
	if got := (&ScopedTenants{}).BuiltinDefaultStates(); got != nil {
		t.Errorf("no build: %v, want nil", got)
	}
}

// RootDisableText is the one root opt-out (#3832 decision 3): the word
// `disable`, any letter case, for a built-in key only.
func TestRootDisableText(t *testing.T) {
	for _, tc := range []struct {
		key, text string
		want      bool
	}{
		{"pg_connections", "disable", true},
		{"pg_connections", " Disable ", true},
		{"pg_connections", "DISABLE", true},
		{"pg_connections", "false", false},
		{"pg_connections", "off", false},
		{"pg_connections", "disabled", false},
		{"pg_connections", "disable:critical", false},
		{"pg_connections_critical", "disable", false},
		{`pg_connections{db="a"}`, "disable", false},
		{"redis_connected_clients", "disable", false},
	} {
		if got := RootDisableText(tc.key, tc.text); got != tc.want {
			t.Errorf("RootDisableText(%q, %q) = %v, want %v", tc.key, tc.text, got, tc.want)
		}
	}
}

// A root `disable` is decoded as itself, not as a refusal, and the built-in
// layer leaves it in place.
func TestRootDisable_DecodeAndSeed(t *testing.T) {
	d, refused, err := readRootDefaults(mustYAMLNode(t, "pg_connections: disable\nredis_connected_clients: disable\n"))
	if err != nil {
		t.Fatal(err)
	}
	if !RootDisabledDefault(d["pg_connections"]) || refused["pg_connections"] != "" {
		t.Errorf("pg_connections: %v refused %q, want the root disable, not refused", d["pg_connections"], refused["pg_connections"])
	}
	if _, r := RefusedDefault(d["pg_connections"]); r {
		t.Error("RefusedDefault reports a root disable refused")
	}
	if refused["redis_connected_clients"] != "disable" {
		t.Errorf("redis_connected_clients: refused %q, want its text: no built-in value to switch off", refused["redis_connected_clients"])
	}
	seeded := seedBuiltinDefaults(d)
	if !RootDisabledDefault(d["pg_connections"]) || slices.Contains(seeded, "pg_connections") {
		t.Errorf("seedBuiltinDefaults replaced the root disable: %v, seeded %v", d["pg_connections"], seeded)
	}
}

func mustYAMLNode(t *testing.T, doc string) *yaml.Node {
	t.Helper()
	var n yaml.Node
	if err := yaml.Unmarshal([]byte(doc), &n); err != nil {
		t.Fatal(err)
	}
	return n.Content[0]
}

// The tenant-api merge (MergeTenantWithRootDefaults) reads a root `disable`
// as /metrics does: no row for the family of a tenant that gives the key no
// value, the family back for one that does.
func TestMergeTenantWithRootDefaults_RootDisable(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"), []byte("defaults:\n  pg_replication_lag: disable\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	rows := func(body string) map[string]bool {
		m := MergeTenantWithRootDefaults(dir, "t1", []byte(body))
		out := map[string]bool{}
		for _, r := range m.ResolveAt(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)) {
			if r.Component == "pg" && r.Metric == "replication_lag" {
				out[r.Severity] = true
			}
		}
		return out
	}
	if got := rows("tenants:\n  t1:\n    pg_replication_lag_critical: \"60\"\n"); len(got) != 0 {
		t.Errorf("inherited root disable: rows %v, want none", got)
	}
	if got := rows("tenants:\n  t1:\n    pg_replication_lag: \"20\"\n    pg_replication_lag_critical: \"60\"\n"); !got["warning"] || !got["critical"] {
		t.Errorf("tenant value: rows %v, want warning and critical", got)
	}
}

// InheritedRootDisables reads every part of the tenant's day: a time window
// whose value is no number leaves the family to the root `disable` then,
// whatever the default reads; a value that is a number all day does not.
func TestInheritedRootDisables_ReadsEveryPartOfTheDay(t *testing.T) {
	at := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	window := func(def, in string) ScheduledValue {
		return ScheduledValue{Default: def, Overrides: []TimeWindowOverride{{Window: "01:00-02:00", Value: in}}}
	}
	for name, tc := range map[string]struct {
		sv   *ScheduledValue
		want bool
	}{
		"not written":               {nil, true},
		"number all day":            {&ScheduledValue{Default: "20"}, false},
		"number, disable window":    {ptr(window("20", "disable")), true},
		"number, unreadable window": {ptr(window("20", "abc")), true},
		"disable, number window":    {ptr(window("disable", "20")), true},
		"number, number window":     {ptr(window("20", "25")), false},
	} {
		c := &ThresholdConfig{
			Defaults: map[string]float64{"pg_connections": rootDisabledDefault()},
			Tenants:  map[string]map[string]ScheduledValue{"t1": {}},
		}
		if tc.sv != nil {
			c.Tenants["t1"]["pg_connections"] = *tc.sv
		}
		got := slices.Contains(c.InheritedRootDisables("t1", at), "pg_connections")
		if got != tc.want {
			t.Errorf("%s: listed %v, want %v", name, got, tc.want)
		}
	}
}

func ptr[T any](v T) *T { return &v }
