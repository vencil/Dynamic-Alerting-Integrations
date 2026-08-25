package config

// aliases_registry_test.go — pins deprecatedKeyAliases to the threshold
// registry's deprecated_aliases section (#1231 commit 2).
//
// SSOT wiring: rule-packs/threshold-registry.yaml (generated from
// _registry_lib.DEPRECATED_KEY_ALIASES) is the alias SSOT; this package's
// deprecatedKeyAliases table is a runtime mirror. This test asserts the two
// are EQUAL — both directions — so a rename landing in the registry without
// the Go mirror (or an alias window closed in the registry but left open
// here) fails CI instead of silently drifting. The Python mirror
// (_grar_validate.DEPRECATED_KEY_ALIASES) has the same pin in
// tests/lint/test_check_threshold_registry.py.

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"gopkg.in/yaml.v3"
)

// registryDoc carries only the sections this pin needs.
type registryDoc struct {
	DeprecatedAliases map[string]string `yaml:"deprecated_aliases"`
	Keys              map[string]any    `yaml:"keys"`
}

// findRepoRootForRegistry walks up from the test working directory until it
// finds the directory containing both rule-packs/ and Makefile (repo root in
// a normal checkout and in a git worktree) — same convention as the app
// module's rulepack_contract_test.go.
func findRepoRootForRegistry(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		rp, mk := filepath.Join(dir, "rule-packs"), filepath.Join(dir, "Makefile")
		if fi, err := os.Stat(rp); err == nil && fi.IsDir() {
			if fi, err := os.Stat(mk); err == nil && !fi.IsDir() {
				return dir
			}
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatalf("could not locate repo root (rule-packs/ + Makefile) walking up from the test cwd")
		}
		dir = parent
	}
}

func loadRegistryDoc(t *testing.T) registryDoc {
	t.Helper()
	root := findRepoRootForRegistry(t)
	raw, err := os.ReadFile(filepath.Join(root, "rule-packs", "threshold-registry.yaml"))
	if err != nil {
		t.Fatalf("read threshold-registry.yaml: %v", err)
	}
	var doc registryDoc
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal threshold-registry.yaml: %v", err)
	}
	return doc
}

// TestDeprecatedKeyAliasesPinnedToRegistry asserts the Go alias table equals
// the registry's deprecated_aliases section exactly (both directions).
func TestDeprecatedKeyAliasesPinnedToRegistry(t *testing.T) {
	doc := loadRegistryDoc(t)
	want := doc.DeprecatedAliases
	if want == nil {
		want = map[string]string{}
	}
	got := deprecatedKeyAliases
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("deprecatedKeyAliases (Go mirror) != registry deprecated_aliases (SSOT)\n"+
			"  Go mirror: %v\n  registry : %v\n"+
			"fix: edit _registry_lib.DEPRECATED_KEY_ALIASES + regen the registry, then update aliases.go to match",
			got, want)
	}
}

// TestDeprecatedAliasTargetsAreRegistryKeys asserts every alias target is an
// active registry key and every retired spelling is NOT — the same fail-loud
// contract build_registry_doc enforces at generation time, re-checked here so
// a hand-edited registry cannot smuggle a dangling alias past the Go mirror.
func TestDeprecatedAliasTargetsAreRegistryKeys(t *testing.T) {
	doc := loadRegistryDoc(t)
	for old, canonical := range deprecatedKeyAliases {
		if _, ok := doc.Keys[canonical]; !ok {
			t.Errorf("alias %q targets %q which is not an active registry key", old, canonical)
		}
		if _, ok := doc.Keys[old]; ok {
			t.Errorf("alias %q is still an ACTIVE registry key — retired spellings must leave the contract", old)
		}
	}
}

// TestLegacySpellingForIsCanonicalKeyForsInverse pins the two directions
// against each other across all three shapes.
//
// ⛔ WHY IT EXISTS. The overlay's reachability check walks the alias relation
// BOTH ways, and the exported inverse was first written to handle only the
// base shape, with a comment claiming `_critical` and dimensional keys never
// reach it. They do. A differential over the reachable key space found no
// behaviour change today — only because the single alias in the registry has a
// base carrying none of the reserved prefixes that route keys down that path.
// That is a property of the current table, not of the code, so the next alias
// would have broken it silently. This asserts the relation instead of the
// accident. (#1569 blind review.)
func TestLegacySpellingForIsCanonicalKeyForsInverse(t *testing.T) {
	t.Parallel()
	for legacy, canonical := range deprecatedKeyAliases {
		for _, shape := range []struct{ name, l, c string }{
			{"exact", legacy, canonical},
			{"critical-suffixed", legacy + criticalSuffix, canonical + criticalSuffix},
			{"dimensional", legacy + `{env="prod"}`, canonical + `{env="prod"}`},
		} {
			t.Run(shape.name+"/"+legacy, func(t *testing.T) {
				gotCanon, wasAlias := CanonicalKeyFor(shape.l)
				if !wasAlias || gotCanon != shape.c {
					t.Fatalf("CanonicalKeyFor(%q) = %q,%v; want %q,true", shape.l, gotCanon, wasAlias, shape.c)
				}
				gotLegacy, hasLegacy := LegacySpellingFor(shape.c)
				if !hasLegacy || gotLegacy != shape.l {
					t.Fatalf("LegacySpellingFor(%q) = %q,%v; want %q,true — the exported inverse must "+
						"cover every shape CanonicalKeyFor does, or a caller that walks the relation "+
						"backwards silently misses these", shape.c, gotLegacy, hasLegacy, shape.l)
				}
			})
		}
	}
}
