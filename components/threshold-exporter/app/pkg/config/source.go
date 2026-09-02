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
		// ⛔ ONE copy of the root-boundary rule, shared with the classifier in
		// ScanFromConfigSource. A second hand-written copy lived here and the
		// two diverged at the bare roots — `root+"/"` is `"//"` for `/`, and for
		// `"."` path.Clean has already stripped the keys' `./` — so the only
		// production source shape returned a silent empty scan. See relToRoot.
		//
		// ⛔ Only the BOUNDARY half is shared: this method deliberately still
		// returns hidden files (TestInMemoryConfigSource_FiltersByRoot pins it;
		// dot-pruning belongs to the scan layer). Hence two functions, not one.
		if _, inside := relToRoot(clean, root); !inside {
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
// ⛔ IT TAKES `rel`, NOT A FULL PATH. The walker never prunes its own starting
// point, so a source rooted at `.config/conf.d` must get its whole tree; hand
// this function the full path and the root's own dot segment prunes
// everything. Pinned by TestDotPrefixedRootYieldsItsWholeTree — nothing else
// in the suite sees it, because every other root in the corpus is `/sim`.
//
// ⛔ EVERY segment, not just the file's immediate parent: the walker prunes
// the whole subtree, so `<root>/.cache/deep/x.yaml` goes by `.cache`.
//
// ⛔ Byte-prefix on `.`, no folding — what the walker does. `confdname.IsHidden`
// is byte-identical and importable here (measured: `pkg/config` compiles
// against `internal/confdname`; `internal/batchpr` already imports it), so this
// IS an unresolved duplicate. What is unresolved is a trade nobody has made:
// `pkg/` has no `internal/` dependency today, and one edge for two `HasPrefix`
// calls may not be worth it. Do not read this as a settled reason to leave it.
func hasHiddenSegment(rel string) bool {
	// `rel == ""` (the root itself, which the walker never prunes) needs no
	// branch: `strings.Split("", "/")` is `[]string{""}`, and `""` is not
	// dot-prefixed, so it falls through to false. An explicit early return
	// here was measured to be an equivalent mutant — inert code that reads
	// like a guard is the defect class this file exists to fix, so it is gone
	// rather than annotated.
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
// ⛔ THE ONLY COPY OF THE BOUNDARY RULE. `YAMLFiles` calls this too; do not
// re-hand-write it there. Both of the traps below were live at some point with
// two copies in the tree, and each was fixed in one copy only.
//
// ⛔ The boundary is the separator, not the byte prefix — `/sim-backup/x.yaml`
// shares `/sim` as bytes and is not inside it. A bare `TrimPrefix` answers
// confidently about paths it has no business answering about:
//
//	p="/other/.git/a.yaml"  root="/sim"  -> "hidden"            // not under root at all
//	p="/simulate/a.yaml"    root="/sim"  -> rel "ulate/a.yaml"  // a fabricated segment
//
// ⛔ `"/"` is not the only root contributing no leading segment: `path.Clean`
// maps BOTH `""` and `"."` to `"."`, and a walker rooted there emits keys with
// no `./` prefix (`filepath.Join(".", x) == x`). Letting `"."` fall through to
// the `root+"/"` arm rejects every relative key — `tenants=map[]` with
// `err=<nil>`, a silent empty scan where the previous code was correct. So the
// bare roots are enumerated, not pattern-matched.
//
// Both traps are pinned by TestRelToRootRequiresADirectoryBoundary and
// TestProductionSourceShapeScansAtBareRoots.
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
		//
		// ⛔ A cleaned relative path that ESCAPES the root keeps a leading `..`
		// segment, and a walker rooted at `.` never emits one — so it is not
		// inside. Without this arm `YAMLFiles(".")` returned `../evil.yaml` to
		// its caller. `ScanFromConfigSource` still dropped it, but only because
		// `..` begins with a dot and so reads as hidden: two contradictory
		// reasons arriving at the right answer, which is the arrangement this
		// file exists to remove.
		if p == ".." || strings.HasPrefix(p, "../") {
			return "", false
		}
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
		// collapsing copies is the point of the #1339 family. That predicate
		// uses `strings.EqualFold`; the walker this scanner must reproduce uses
		// `strings.ToLower(name) == "_defaults.yaml"`. Measured in Go:
		//
		//	name := "_defaultſ.yaml"                       // U+017F LONG S
		//	strings.ToLower(name) == "_defaults.yaml"      // false  ← the walker
		//	strings.EqualFold(name, "_defaults.yaml")      // true   ← confdname
		//
		// ⛔ And this is not two defensible designs: confdname cites
		// `tests/shared/confd_name_classification_matrix.json` as its contract,
		// and that file defines the field as "name LOWERCASED is exactly …" —
		// the walker's semantics. So `IsDefaults` does not satisfy the contract
		// it cites (issue #1670). Adopting it here would buy one fewer copy by
		// importing a known defect. Pinned by
		// TestSharedDefaultsPredicateStillDisagreesWithTheWalker.
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
