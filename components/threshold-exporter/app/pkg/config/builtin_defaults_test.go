package config

// builtin_defaults_test.go — the generated built-in table (#3832) pinned to
// the threshold registry, and its digest pinned to the table.

import (
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"

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
		seedBuiltinDefaults(tc.in)
		if got := tc.in[k]; got != tc.want {
			t.Errorf("%s: %s = %v, want %v", name, k, got, tc.want)
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
