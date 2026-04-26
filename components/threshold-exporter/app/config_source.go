package main

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
// (`scanFromConfigSource`). The disk path keeps its own walker
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
// pass produces one map at the cost of holding tenant YAML bytes
// in memory briefly (peak ~few MB at 1000 tenants per benchmarks).

import (
	"crypto/sha256"
	"fmt"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// ConfigSource enumerates YAML files for hierarchical merge. See file
// header for why this is a single-method interface.
type ConfigSource interface {
	// YAMLFiles returns every *.yaml/*.yml file the source wants the
	// merge engine to consider, keyed by absolute filepath.Clean'd
	// path. The map is owned by the source — callers must treat byte
	// slices as read-only.
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
// under rootPath. Filtering is by string prefix on Cleaned paths;
// callers should use forward slashes for in-memory paths to match
// filepath.Clean on POSIX. On Windows the source still works because
// filepath.Clean canonicalises both.
func (s *InMemoryConfigSource) YAMLFiles(rootPath string) (map[string][]byte, error) {
	root := filepath.Clean(rootPath)
	out := make(map[string][]byte, len(s.files))
	for p, b := range s.files {
		clean := filepath.Clean(p)
		if clean != root && !strings.HasPrefix(clean, root+string(filepath.Separator)) {
			continue
		}
		lower := strings.ToLower(filepath.Base(clean))
		if !strings.HasSuffix(lower, ".yaml") && !strings.HasSuffix(lower, ".yml") {
			continue
		}
		out[clean] = b
	}
	return out, nil
}

// scanFromConfigSource is the in-memory cousin of scanDirHierarchical:
// it takes a corpus from a ConfigSource and produces the same outputs
// (tenants map, defaults set, per-file hashes, InheritanceGraph) using
// identical classification + dedup + chain rules.
//
// This is what the /simulate endpoint calls. Production reload still
// uses scanDirHierarchical because that path also gathers mtimes for
// change detection — a concern simulate doesn't share.
func scanFromConfigSource(src ConfigSource, rootPath string) (
	tenants map[string]string,
	defaults map[string]bool,
	hashes map[string]string,
	graph *InheritanceGraph,
	err error,
) {
	absRoot := filepath.Clean(rootPath)

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

	for path, data := range corpus {
		name := filepath.Base(path)
		// Hidden files skipped — match scanDirHierarchical.
		if strings.HasPrefix(name, ".") {
			continue
		}
		hashes[path] = fmt.Sprintf("%x", sha256.Sum256(data))

		lower := strings.ToLower(name)
		if strings.HasPrefix(name, "_") {
			if lower == "_defaults.yaml" || lower == "_defaults.yml" {
				defaults[path] = true
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
			return nil, nil, nil, nil, fmt.Errorf("parse %s: %w", path, perr)
		}
		if len(doc.Tenants) == 0 {
			continue
		}
		for tid := range doc.Tenants {
			decls = append(decls, tenantDecl{ID: tid, FilePath: path})
		}
	}

	for _, td := range decls {
		if prev, exists := tenants[td.ID]; exists && prev != td.FilePath {
			return nil, nil, nil, nil, fmt.Errorf(
				"duplicate tenant ID %q: defined in both %s and %s", td.ID, prev, td.FilePath)
		}
		tenants[td.ID] = td.FilePath
	}

	graph = NewInheritanceGraph()
	chainCache := make(map[string][]string)

	tenantIDs := make([]string, 0, len(tenants))
	for tid := range tenants {
		tenantIDs = append(tenantIDs, tid)
	}
	sortStrings(tenantIDs)

	for _, tid := range tenantIDs {
		srcPath := tenants[tid]
		dir := filepath.Dir(srcPath)
		chain, cached := chainCache[dir]
		if !cached {
			chain = collectDefaultsChain(dir, absRoot, defaults)
			chainCache[dir] = chain
		}
		graph.AddTenant(tid, chain)
	}

	return tenants, defaults, hashes, graph, nil
}
