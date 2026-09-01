package config

// ============================================================
// ConfigSource abstraction (v2.8.0 Phase .c C-7a)
// ============================================================
//
// Two production code paths share the hierarchical merge engine:
//
//  1. ConfigManager.Resolve / WatchLoop — reads from disk under
//     `--config-dir`, walks `_defaults.yaml` chains, computes
//     `merged_hash` per tenant. The InheritanceGraph is a snapshot of
//     what's currently on disk.
//
//  2. POST /api/v1/tenants/simulate (C-7b) — caller hands in raw YAML
//     bytes for a hypothetical tenant + its defaults chain and asks
//     "what would the effective config look like if I committed this?".
//     There is no disk to walk; the simulation must NOT touch the
//     WatchLoop's state nor leak temp files.
//
// `ConfigSource` lets both paths plug into the same scan + merge
// machinery. It exposes one capability — enumerating the YAML files
// the merge engine should consider — and leaves the parsing, hashing,
// dedup, and InheritanceGraph construction in one place
// (`ScanFromConfigSource`). The disk path keeps its own walker
// (`scanDirHierarchical`) for production because that walker also
// records mtimes for debounced-reload change detection — a concern
// the simulate path doesn't share.
//
// Design choice: ConfigSource returns a `map[absPath][]byte` rather
// than streaming through a callback. The hierarchy scan needs the
// whole file set to (a) detect duplicate tenant IDs across files
// and (b) build the defaults chain by walking dir parents — both
// require random access to the population. For the in-memory case
// the population is already a map; for the disk case the WalkDir
// pass produces one map at the cost of briefly holding all YAML
// bytes in memory (back-of-envelope: ~1 MB per 1000 tenants × 1 KiB
// average tenant.yaml; not measured under load).

import (
	"crypto/sha256"
	"fmt"
	"path"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// ConfigSource enumerates YAML files for hierarchical merge. See file
// header for why this is a single-method interface.
type ConfigSource interface {
	// YAMLFiles returns every *.yaml/*.yml file the source wants the
	// merge engine to consider, keyed by absolute Cleaned path. The
	// path-cleaning scheme MUST match the caller's expectation — for
	// InMemoryConfigSource, paths are POSIX (path.Clean); for any
	// future disk-backed source, OS-native (filepath.Clean) would be
	// appropriate. ScanFromConfigSource currently treats them as POSIX
	// (only InMemory caller exists today). The map is owned by the
	// source — callers must treat byte slices as read-only.
	//
	// rootPath is the conf.d/ root the caller intends to scan.
	// In-memory sources may use it to filter their corpus; disk
	// sources walk under it.
	YAMLFiles(rootPath string) (map[string][]byte, error)
}

// InMemoryConfigSource is a ConfigSource backed by a caller-supplied
// `{path: bytes}` map. Used by the /simulate endpoint and unit tests
// that want to drive the merge engine without touching disk.
//
// The map keys are treated as conceptual filesystem paths — they
// determine which file is a `_defaults.yaml` (basename match) and
// where the tenant sits in the hierarchy (directory ancestors).
// Callers should pass POSIX-style absolute paths under a synthetic
// root (e.g. `/sim/_defaults.yaml`, `/sim/dom-a/region-1/foo.yaml`).
type InMemoryConfigSource struct {
	files map[string][]byte
}

// NewInMemoryConfigSource takes ownership of `files` (does not copy).
// The caller must not mutate the map after handing it over.
func NewInMemoryConfigSource(files map[string][]byte) *InMemoryConfigSource {
	return &InMemoryConfigSource{files: files}
}

// YAMLFiles returns the subset of the corpus whose paths are at or
// under rootPath. Filtering is by string prefix on POSIX-Cleaned paths
// (path.Clean, not filepath.Clean) — InMemoryConfigSource is documented
// as POSIX-only (see type comment) and `filepath.Clean` would convert
// `/sim/x` to `\sim\x` on Windows, breaking the prefix match against
// caller-supplied POSIX keys.
//
// History: original implementation used filepath.Clean here. On Windows
// hosts that produced backslash-separated keys in the returned map,
// which then mismatched the POSIX-keyed `files` map in
// SimulateEffective when looking up DefaultsChain paths — the chain
// resolved to nil bytes and inherited keys silently dropped from the
// merged config. CI passed because Linux runners coincidentally share
// the POSIX separator. Tracked under "Simulate Windows-host flake"
// chip; fixed by switching to path.Clean.
func (s *InMemoryConfigSource) YAMLFiles(rootPath string) (map[string][]byte, error) {
	root := path.Clean(rootPath)
	out := make(map[string][]byte, len(s.files))
	for p, b := range s.files {
		clean := path.Clean(p)
		if clean != root && !strings.HasPrefix(clean, root+"/") {
			continue
		}
		lower := strings.ToLower(path.Base(clean))
		if !strings.HasSuffix(lower, ".yaml") && !strings.HasSuffix(lower, ".yml") {
			continue
		}
		out[clean] = b
	}
	return out, nil
}

// hasHiddenSegment reports whether any segment of the ALREADY-ROOT-RELATIVE
// path `rel` is dot-prefixed. It is half of the flat-map equivalent of the disk
// walker's `fs.SkipDir` pruning: that walker never descends into a hidden
// directory, so every file beneath one is invisible to it no matter what the
// file itself is called.
//
// ⛔ IT TAKES `rel`, NOT A FULL PATH, AND THAT IS LOAD-BEARING. Stripping the
// root is `relToRoot`'s job, and the walker never prunes its own starting
// point — "Never prune the root itself even if rootPath happens to start with
// '.'" — so a source rooted at `.config/conf.d` must get its whole tree, not
// nothing. Hand this function the full path instead and the root's own dot
// segment prunes everything. Measured: that substitution left the entire suite
// green until `TestDotPrefixedRootYieldsItsWholeTree` was added, because every
// root in the corpus was `/sim` or `/`. The promise lives at the CALL SITE;
// this doc describes it only because that is where a reader looks for it.
//
// ⛔ Every segment is tested, not just the file's immediate parent. The
// walker prunes the entire subtree, so `<root>/.cache/deep/x.yaml` is dropped
// by `.cache` even though `deep` and `x.yaml` are both plain.
//
// ⛔ This is a byte-prefix test on `.` — no case folding, nothing Unicode —
// which is exactly what the walker does (`strings.HasPrefix(name, ".")`).
// `internal/confdname.IsHidden` is the same byte-prefix test, but this package
// deliberately does not import it; see the note on the defaults comparison in
// ScanFromConfigSource for why that shared predicate is not a drop-in here.
func hasHiddenSegment(rel string) bool {
	if rel == "" {
		// `rel` is the root itself. The walker would be looking at its own
		// starting point, which it never prunes.
		//
		// ⛔ THIS BRANCH IS NOT LOAD-BEARING — it states intent, it does not
		// enforce it. Measured in Go: `strings.Split("", "/")` returns
		// `[]string{""}` (length 1, not an empty slice), and
		// `strings.HasPrefix("", ".")` is false, so deleting this branch
		// produces the identical answer by falling through the loop. A
		// mutation pass found exactly that: removing it is the one mutant of
		// nineteen that survives, and it survives because it is EQUIVALENT,
		// not because a guard is missing. Said out loud because a comment
		// that lets a reader believe an inert branch is holding something up
		// is the same defect class this file exists to fix.
		return false
	}
	for _, seg := range strings.Split(rel, "/") {
		if strings.HasPrefix(seg, ".") {
			return true
		}
	}
	return false
}

// relToRoot returns `p` expressed relative to `root`, and whether `p` is inside
// `root` AT A DIRECTORY BOUNDARY.
//
// ⛔ The boundary is the separator, not the byte prefix. `/sim-backup/x.yaml`
// shares the five bytes `/sim` with root `/sim` and is NOT inside it. An
// earlier version of the hidden test here computed `strings.TrimPrefix(p,
// root)` with no boundary check, which answered confidently about paths it had
// no business answering about — measured on that version:
//
//	p="/other/.git/a.yaml"  root="/sim"  -> "hidden" (true)   // not even under root
//	p="/simulate/a.yaml"    root="/sim"  -> rel "ulate/a.yaml" // a fabricated segment
//
// ⛔ Nothing in-tree could reach either case, because
// `InMemoryConfigSource.YAMLFiles` already filters on `clean == root ||
// HasPrefix(clean, root+"/")`. That is exactly the problem: the classifier's
// correctness rested on a caller doing the check, while `ConfigSource` is a
// PUBLIC interface whose doc does not require it (it even anticipates a
// "future disk-backed source"). A predicate that returns a confident wrong
// answer off its domain is the seed this whole family grows from, so the check
// lives here too rather than being assumed.
// ⛔ `"/"` IS NOT THE ONLY ROOT THAT CONTRIBUTES NO LEADING SEGMENT. `path.Clean`
// maps BOTH `""` and `"."` to `"."`, and a walker rooted there emits keys with no
// `./` prefix at all (`filepath.Join(".", x) == x`). An earlier version of this
// function special-cased `"/"` and let `"."` fall through to the `root+"/"` arm,
// which rejected every relative key — measured, on a corpus the previous
// implementation classified CORRECTLY:
//
//	root="."  before: tenants=map[a:a.yaml b:sub/b.yaml]   (matches a walker rooted there)
//	root="."  after:  tenants=map[]  hashes=0  err=<nil>   (a silent empty scan)
//
// ⛔ Silent is the operative word: no error, just nothing. Adding a boundary
// check to stop one confident wrong answer had produced a different confident
// wrong answer one root-shape over — which is the family reproducing inside its
// own fix, so the bare-root cases are enumerated rather than pattern-matched.
func relToRoot(p, root string) (rel string, inside bool) {
	if p == root {
		return "", true
	}
	switch root {
	case "/":
		if !strings.HasPrefix(p, "/") {
			return "", false
		}
		return p[1:], true
	case ".":
		// Both `path.Clean("")` and `path.Clean(".")` land here.
		if strings.HasPrefix(p, "./") {
			return p[2:], true
		}
		// An absolute key is not under a relative root.
		if strings.HasPrefix(p, "/") {
			return "", false
		}
		return p, true
	}
	if !strings.HasPrefix(p, root+"/") {
		return "", false
	}
	return p[len(root)+1:], true
}

// ScanFromConfigSource is the in-memory cousin of scanDirHierarchical:
// it takes a corpus from a ConfigSource and produces the same outputs
// (tenants map, defaults set, per-file hashes, InheritanceGraph) using
// identical classification + dedup + chain rules.
//
// This is what the /simulate endpoint calls. Production reload still
// uses scanDirHierarchical because that path also gathers mtimes for
// change detection — a concern simulate doesn't share.
func ScanFromConfigSource(src ConfigSource, rootPath string) (
	tenants map[string]string,
	defaults map[string]bool,
	hashes map[string]string,
	graph *InheritanceGraph,
	err error,
) {
	// path.Clean (POSIX) not filepath.Clean (OS-aware) — ScanFromConfigSource
	// only feeds InMemoryConfigSource today (verified via `grep -rn
	// ScanFromConfigSource components/threshold-exporter/app/`), and its
	// contract is POSIX-only paths. Same Windows-host bug as YAMLFiles:
	// filepath.Clean would convert `/sim` → `\sim` and break the prefix
	// match against POSIX-keyed callers.
	absRoot := path.Clean(rootPath)

	corpus, cerr := src.YAMLFiles(absRoot)
	if cerr != nil {
		return nil, nil, nil, nil, fmt.Errorf("source enumerate %q: %w", absRoot, cerr)
	}

	tenants = make(map[string]string)
	defaults = make(map[string]bool)
	hashes = make(map[string]string)

	type tenantDecl struct {
		ID       string
		FilePath string
	}
	var decls []tenantDecl

	for p, data := range corpus {
		// ⛔ The hidden test is on the whole PATH below the root, not on
		// the basename (#1589). The disk walker prunes hidden DIRECTORIES
		// with `fs.SkipDir`, so it never visits `<root>/.git/inside.yaml`
		// at all. This loop sees a flat map, where that path's basename is
		// `inside.yaml` — not dot-prefixed, so a basename test admitted it
		// and registered whatever `tenants:` key it declared. Measured, on
		// the corpus in config_source_oracle_parity_test.go: this scanner
		// answered `[fromcache fromgit nested plain]` where the walker
		// answered `[nested plain]`, at arbitrary depth (`.cache/deep/`).
		//
		// ⛔ The comment previously here said "match scanDirHierarchical"
		// while doing the opposite. That sentence is why the divergence
		// survived: every reader who checked took the claim for the check.
		rel, inRoot := relToRoot(p, absRoot)
		if !inRoot || hasHiddenSegment(rel) {
			continue
		}
		// path.Base (POSIX) not filepath.Base — the loop variable was
		// renamed from `path` to `p` to avoid shadowing the `path`
		// package; same Windows-host fix family as YAMLFiles +
		// CollectDefaultsChainPOSIX above.
		name := path.Base(p)
		hashes[p] = fmt.Sprintf("%x", sha256.Sum256(data))

		// ⛔ DELIBERATELY NOT `internal/confdname.IsDefaults`, even though
		// this is the fourth hand-written copy of the rule and collapsing
		// copies is the whole point of the #1339 family. That predicate uses
		// `strings.EqualFold`; the walker this scanner must agree with uses
		// `strings.ToLower(name) == "_defaults.yaml"`. MEASURED IN GO on this
		// toolchain (not reasoned about, and not measured in another language
		// — that mistake has its own scar in confdname's header):
		//
		//	name := "_defaultſ.yaml"                       // U+017F LATIN SMALL LETTER LONG S
		//	strings.ToLower(name) == "_defaults.yaml"      // false  ← the walker
		//	strings.EqualFold(name, "_defaults.yaml")      // true   ← confdname
		//
		// `unicode.SimpleFold` connects U+017F ↔ 'S' ↔ 's', and ToLower does
		// not. So adopting the shared predicate here would move this scanner
		// off the walker it is defined as reproducing — buying one fewer copy
		// by creating a fresh silent divergence, in the file whose job is to
		// not do that. The copy stays until the shared predicate and the
		// walker are reconciled; that reconciliation changes the write plane
		// (`internal/batchpr`, `internal/profile`) and is not this change's
		// blast radius.
		lower := strings.ToLower(name)
		if strings.HasPrefix(name, "_") {
			if lower == "_defaults.yaml" || lower == "_defaults.yml" {
				defaults[p] = true
			}
			// Other `_*.yaml` are hashed for completeness but not part
			// of the inheritance graph (mirrors scanDirHierarchical).
			continue
		}

		// Tenant file: parse `tenants:` block. Lightweight shape
		// matching scanDirHierarchical — full config re-parsed by
		// computeMergedHash on demand.
		var doc struct {
			Tenants map[string]yaml.Node `yaml:"tenants"`
		}
		if perr := yaml.Unmarshal(data, &doc); perr != nil {
			// In simulate mode we surface parse errors loudly: the
			// caller is interactively asking "what would happen if
			// I committed this?", a malformed YAML is the answer.
			// Production scanDirHierarchical logs+skips because a
			// single broken file shouldn't take down the WatchLoop;
			// here we want the 400 response.
			return nil, nil, nil, nil, fmt.Errorf("parse %s: %w", p, perr)
		}
		if len(doc.Tenants) == 0 {
			continue
		}
		for tid := range doc.Tenants {
			decls = append(decls, tenantDecl{ID: tid, FilePath: p})
		}
	}

	for _, td := range decls {
		if prev, exists := tenants[td.ID]; exists && prev != td.FilePath {
			// Typed so simulate / library callers can errors.As it (#127
			// C6-A); Error() string byte-identical to the former fmt.Errorf.
			return nil, nil, nil, nil, &DuplicateTenantError{
				TenantID: td.ID,
				PathA:    prev,
				PathB:    td.FilePath,
			}
		}
		tenants[td.ID] = td.FilePath
	}

	graph = NewInheritanceGraph()
	chainCache := make(map[string][]string)

	tenantIDs := make([]string, 0, len(tenants))
	for tid := range tenants {
		tenantIDs = append(tenantIDs, tid)
	}
	sort.Strings(tenantIDs)

	for _, tid := range tenantIDs {
		srcPath := tenants[tid]
		// path.Dir + CollectDefaultsChainPOSIX: in-memory contract is
		// POSIX-only; filepath.Dir on Windows would convert /sim/foo to
		// \sim and break the chain lookup against the POSIX-keyed
		// defaults map (Simulate Windows-host flake — see
		// CollectDefaultsChainPOSIX docstring for the full triage).
		dir := path.Dir(srcPath)
		chain, cached := chainCache[dir]
		if !cached {
			chain = CollectDefaultsChainPOSIX(dir, absRoot, defaults)
			chainCache[dir] = chain
		}
		graph.AddTenant(tid, chain)
	}

	return tenants, defaults, hashes, graph, nil
}
