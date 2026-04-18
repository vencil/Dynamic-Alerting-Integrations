package main

// ============================================================
// Hierarchical conf.d/ scanner (ADR-017)
// ============================================================
//
// v2.6.0 `loadDir` / `scanDirFileHashes` only scan the top-level conf.d/
// directory (flat mode). ADR-017 extends conf.d/ to a *hierarchical* layout
// where `_defaults.yaml` can appear at every directory level (L0 root, L1
// domain, L2 subdomain, L3 leaf) and is inherited by tenant files deeper in
// the tree. This file adds that recursive walk without disturbing the
// single-level `scanDirFileHashes` path — callers opt in by invoking
// `scanDirHierarchical` directly.
//
// Python reference implementation: scripts/tools/dx/describe_tenant.py
//   - ConfDScanner._scan()          → scanDirHierarchical (this file)
//   - ConfDScanner.effective_config → computeMergedHash   (config_inheritance.go)
//
// Go port is a *semantic translation*. Any divergence from describe_tenant.py
// that changes the 16-char merged_hash is a bug; fixtures in tests/golden/
// pin the expected hashes (see config_golden_parity_test.go).

import (
	"crypto/sha256"
	"fmt"
	"io/fs"
	"log"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// InheritanceGraph tracks the defaults↔tenants dependency for a hierarchical
// conf.d layout (ADR-017). Two maps, one per direction:
//
//   - TenantDefaults[tenantID]  → L0..Ln defaults paths (root first, leaf last).
//     Used by computeMergedHash and the /effective handler.
//   - DefaultsToTenants[path]   → tenant IDs whose merged_hash depends on this
//     defaults file. Used by the debounced reload path: when a
//     _defaults.yaml changes, we look up exactly which tenants need re-hash.
//
// All paths stored are absolute + filepath.Clean-ed so equality comparisons are
// stable across calls. The struct is treated as immutable once built — reload
// constructs a fresh graph and atomically swaps the pointer on ConfigManager.
type InheritanceGraph struct {
	DefaultsToTenants map[string][]string
	TenantDefaults    map[string][]string
}

// NewInheritanceGraph returns an empty graph with both directions initialized.
func NewInheritanceGraph() *InheritanceGraph {
	return &InheritanceGraph{
		DefaultsToTenants: make(map[string][]string),
		TenantDefaults:    make(map[string][]string),
	}
}

// AddTenant records a tenant's inheritance chain. defaultsChain MUST be
// ordered root-first, leaf-last (matching describe_tenant.py after its
// internal reverse). A defensive copy is made so the caller may reuse the
// slice.
func (g *InheritanceGraph) AddTenant(tenantID string, defaultsChain []string) {
	if g.TenantDefaults == nil {
		g.TenantDefaults = make(map[string][]string)
	}
	if g.DefaultsToTenants == nil {
		g.DefaultsToTenants = make(map[string][]string)
	}
	chain := make([]string, len(defaultsChain))
	copy(chain, defaultsChain)
	g.TenantDefaults[tenantID] = chain

	for _, dp := range chain {
		g.DefaultsToTenants[dp] = append(g.DefaultsToTenants[dp], tenantID)
	}
}

// TenantsAffectedBy returns the tenant IDs whose effective config depends on
// the given _defaults.yaml path. Returns nil when the path is unknown — the
// caller can distinguish "no tenants inherit this file" vs "unrelated file"
// via the defaults map returned by scanDirHierarchical.
func (g *InheritanceGraph) TenantsAffectedBy(defaultsPath string) []string {
	if g == nil {
		return nil
	}
	return g.DefaultsToTenants[defaultsPath]
}

// scanDirHierarchical walks a conf.d/ tree collecting every tenant file and
// every _defaults.yaml file at every nesting depth. It supports flat layouts
// (all files at root), deep hierarchies (L0..L3+), and mixed modes (a subtree
// with its own _defaults.yaml plus flat siblings at the root).
//
// The returned maps all use absolute, Clean-ed paths as keys so callers can
// compare across scans. Hashes are full 64-char SHA-256 hex strings (not the
// 16-char truncation used for merged_hash) — per-file hashes are internal
// change-detection state, not user-facing identifiers.
//
// Rules (intentional parity with describe_tenant.py ConfDScanner._scan):
//   - Every *.yaml / *.yml file is read & hashed. Basename starting with '.'
//     skips; basename starting with '_' → hashed but not treated as a tenant
//     file. Only `_defaults.yaml` / `_defaults.yml` are entered in `defaults`.
//   - Directories starting with '.' are pruned entirely (e.g. .git). Dirs
//     starting with '_' are *not* pruned — they may hold nested _defaults
//     (`_profiles/` style future extension).
//   - A tenant file is any non-'_' *.yaml whose top-level `tenants:` is a
//     mapping. Multiple tenants may live in one file; each tenant ID maps
//     back to the same source path.
//   - Duplicate tenant IDs across files → returns a typed error. Treating
//     the first file as canonical would mask config drift; failing loud is
//     the guardrail from §8.11.2 Phase 1 "防重".
//   - The defaults chain for a tenant is the ordered list of _defaults.yaml
//     files from the tenant-file's parent dir walked up to (and including)
//     the scan root. `.yaml` wins over `.yml` at the same level. Reversed so
//     L0 (root) is first in the returned slice (see describe_tenant.py line
//     152: `chain.reverse()`).
//
// `priorMtimes` is accepted for forward-compatibility with an mtime-guard
// optimization layered in Phase 3; Phase 1 always re-reads and re-hashes
// (parity first, perf later). When non-nil it is currently ignored; tests
// should pass nil.
func scanDirHierarchical(rootPath string, priorMtimes map[string]fileStat) (
	tenants map[string]string,
	defaults map[string]bool,
	hashes map[string]string,
	mtimes map[string]fileStat,
	graph *InheritanceGraph,
	err error,
) {
	_ = priorMtimes // reserved for Phase 3 (mtime-skip optimization)

	// v2.7.0 Phase 4: record wall-clock duration for the scan so operators
	// can alert on slow scans (e.g., >1s = filesystem regression). Done
	// first so even error returns are observed — a scan that errors out
	// fast is itself useful signal.
	defer ObserveScanDuration()()

	tenants = make(map[string]string)
	defaults = make(map[string]bool)
	hashes = make(map[string]string)
	mtimes = make(map[string]fileStat)

	absRoot, aerr := filepath.Abs(rootPath)
	if aerr != nil {
		return nil, nil, nil, nil, nil, fmt.Errorf("resolve root %q: %w", rootPath, aerr)
	}
	absRoot = filepath.Clean(absRoot)

	info, serr := os.Stat(absRoot)
	if serr != nil {
		return nil, nil, nil, nil, nil, fmt.Errorf("stat %q: %w", absRoot, serr)
	}
	if !info.IsDir() {
		return nil, nil, nil, nil, nil, fmt.Errorf("%q is not a directory", absRoot)
	}

	type tenantDecl struct {
		ID       string
		FilePath string
	}
	var decls []tenantDecl

	walkErr := filepath.WalkDir(absRoot, func(path string, d fs.DirEntry, werr error) error {
		if werr != nil {
			// Tolerate individual unreadable entries (e.g. permissions on a
			// junk dir). Log and continue — matches Python's rglob behavior
			// which silently skips on OS errors.
			log.Printf("WARN: walk error at %s: %v", path, werr)
			return nil
		}
		name := d.Name()

		if d.IsDir() {
			// Prune hidden dirs. Never prune the root itself even if rootPath
			// happens to start with '.' (e.g. `./conf.d` → absRoot is clean).
			if path != absRoot && strings.HasPrefix(name, ".") {
				return fs.SkipDir
			}
			return nil
		}

		// Hidden files skipped (matches Python rglob + name.startswith("_")
		// gate; dot-files aren't explicitly tested in Python but are implied
		// by conventional conf.d/ hygiene).
		if strings.HasPrefix(name, ".") {
			return nil
		}

		lower := strings.ToLower(name)
		if !strings.HasSuffix(lower, ".yaml") && !strings.HasSuffix(lower, ".yml") {
			return nil
		}

		data, rerr := os.ReadFile(path)
		if rerr != nil {
			log.Printf("WARN: cannot read %s: %v", path, rerr)
			return nil
		}

		entryInfo, ierr := d.Info()
		if ierr != nil {
			log.Printf("WARN: cannot stat %s: %v", path, ierr)
			return nil
		}

		clean := filepath.Clean(path)
		hashes[clean] = fmt.Sprintf("%x", sha256.Sum256(data))
		mtimes[clean] = fileStat{ModTime: entryInfo.ModTime().UnixNano(), Size: entryInfo.Size()}

		if strings.HasPrefix(name, "_") {
			if lower == "_defaults.yaml" || lower == "_defaults.yml" {
				defaults[clean] = true
			}
			// Other `_*.yaml` files (e.g. `_profiles.yaml` in flat mode) are
			// still hashed for change detection but are not part of the
			// hierarchical inheritance graph — they're merged at the top
			// level via the existing scanDirFileHashes path. Hierarchical
			// scan just records them in `hashes` + `mtimes`.
			return nil
		}

		// Tenant-file path: parse `tenants:` block. We use a lightweight
		// shape that just captures tenant IDs — full config is re-parsed by
		// computeMergedHash when needed. This keeps scan cheap when only one
		// tenant in a large tree has changed.
		var doc struct {
			Tenants map[string]yaml.Node `yaml:"tenants"`
		}
		if perr := yaml.Unmarshal(data, &doc); perr != nil {
			log.Printf("WARN: cannot parse %s: %v", path, perr)
			return nil
		}
		if len(doc.Tenants) == 0 {
			// File without a `tenants:` wrapper or with an empty block. Not
			// an error — could be a commented-out placeholder. Just skip.
			return nil
		}
		for tid := range doc.Tenants {
			decls = append(decls, tenantDecl{ID: tid, FilePath: clean})
		}
		return nil
	})
	if walkErr != nil {
		return nil, nil, nil, nil, nil, fmt.Errorf("walk %q: %w", absRoot, walkErr)
	}

	// Detect duplicate tenant IDs. A single file containing the same tenant
	// twice is caught by yaml.v3 (duplicate map key → parser error or
	// last-wins depending on strictness; we rely on strictness in describe_*
	// and accept the last-wins here since downstream sees one tenant either
	// way). Cross-file duplicates are the interesting case and are rejected.
	for _, td := range decls {
		if prev, exists := tenants[td.ID]; exists && prev != td.FilePath {
			return nil, nil, nil, nil, nil, fmt.Errorf(
				"duplicate tenant ID %q: defined in both %s and %s", td.ID, prev, td.FilePath)
		}
		tenants[td.ID] = td.FilePath
	}

	// Build the InheritanceGraph. Cache chain-per-dir because multiple
	// tenants in the same directory share the same chain — at 1000 tenants
	// with ~20 dirs this cuts the chain walk cost by ~50×.
	graph = NewInheritanceGraph()
	chainCache := make(map[string][]string)

	// Deterministic iteration → stable DefaultsToTenants slices (ordering
	// matters for debounced reload batching; tests rely on a stable order).
	tenantIDs := make([]string, 0, len(tenants))
	for tid := range tenants {
		tenantIDs = append(tenantIDs, tid)
	}
	// Sort for stability. We intentionally don't sort by source path because
	// tenants in the same file would reshuffle across scans.
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

	return tenants, defaults, hashes, mtimes, graph, nil
}

// collectDefaultsChain walks from `leafDir` up to `root` (both inclusive),
// collecting _defaults.yaml at each level. Returns root-first (L0..Ln) order.
// `defaults` is the set populated by the walker; this spares a second stat
// call per directory level.
//
// Precondition: `leafDir` must be `root` or a descendant. (The walker only
// visits entries under `root`, so this is always true for real callers.)
func collectDefaultsChain(leafDir, root string, defaults map[string]bool) []string {
	var chain []string
	current := filepath.Clean(leafDir)
	rootClean := filepath.Clean(root)

	for {
		// Prefer .yaml over .yml when both exist at the same level (same
		// precedence rule as describe_tenant.py's iteration order).
		yamlPath := filepath.Join(current, "_defaults.yaml")
		ymlPath := filepath.Join(current, "_defaults.yml")
		if defaults[yamlPath] {
			chain = append(chain, yamlPath)
		} else if defaults[ymlPath] {
			chain = append(chain, ymlPath)
		}

		if current == rootClean {
			break
		}
		parent := filepath.Dir(current)
		if parent == current {
			// Reached filesystem root without hitting rootClean — shouldn't
			// happen given the precondition, but don't infinite-loop.
			break
		}
		current = parent
	}

	// Reverse to make chain[0] the top-most (L0) defaults and the last entry
	// the nearest-to-tenant (Ln). Matches describe_tenant.py line 152.
	for i, j := 0, len(chain)-1; i < j; i, j = i+1, j-1 {
		chain[i], chain[j] = chain[j], chain[i]
	}
	return chain
}

// sortStrings is a small local helper so this file doesn't pull in "sort"
// just for a single call site. Keeping the dependency surface minimal makes
// it easier to spot accidental coupling when reading diffs.
func sortStrings(s []string) {
	// Insertion sort is fine: len(tenants) is typically <1000 and we call
	// this once per scan. Keeps binary size marginally smaller than pulling
	// sort.Strings. If profile shows it matters, swap to sort.Strings.
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j-1] > s[j]; j-- {
			s[j-1], s[j] = s[j], s[j-1]
		}
	}
}
