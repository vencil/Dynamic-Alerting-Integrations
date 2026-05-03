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
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// DuplicateTenantError signals that the same tenant ID was discovered in two
// different files during a hierarchical scan. This is a misconfig (e.g. forgot
// to delete the old flat copy after `git mv` to the nested layout) that the
// platform should reject hard rather than silently last-wins-merge.
//
// Returned by `scanDirHierarchical` and detected at higher layers via
// `errors.As(err, &DuplicateTenantError{})` (issue #127, v2.8.x hardening).
//
// Before v2.8.x: scanDirHierarchical returned a generic fmt.Errorf, and Load()
// swallowed it with a WARN log. Customers could deploy with a duplicate tenant
// silently merged via map last-wins iteration — easy to miss in production.
//
// After v2.8.x: typed error lets Load() / fullDirLoad() reject the misconfig
// at the boundary; other scan errors (permissions, malformed file) keep the
// log-and-continue policy because hierarchical mode is opt-in and shouldn't
// tear down a flat-only deploy.
type DuplicateTenantError struct {
	TenantID string
	PathA    string // First-discovered file
	PathB    string // Second-discovered file (the one rejected)
}

func (e *DuplicateTenantError) Error() string {
	return fmt.Sprintf("duplicate tenant ID %q: defined in both %s and %s", e.TenantID, e.PathA, e.PathB)
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
			// v2.8.0 A-8d: expose parse failure as a Prometheus counter so
			// ops can alert on "tenant file persistently broken". label is
			// basename (not full path) to cap cardinality.
			IncParseFailure(filepath.Base(path))
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
	// way). Cross-file duplicates are the interesting case and are rejected
	// via the typed `*DuplicateTenantError` so callers can distinguish a
	// misconfig from generic scan errors (issue #127 hardening).
	for _, td := range decls {
		if prev, exists := tenants[td.ID]; exists && prev != td.FilePath {
			return nil, nil, nil, nil, nil, &DuplicateTenantError{
				TenantID: td.ID,
				PathA:    prev,
				PathB:    td.FilePath,
			}
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
	sort.Strings(tenantIDs)

	for _, tid := range tenantIDs {
		srcPath := tenants[tid]
		dir := filepath.Dir(srcPath)
		chain, cached := chainCache[dir]
		if !cached {
			chain = config.CollectDefaultsChain(dir, absRoot, defaults)
			chainCache[dir] = chain
		}
		graph.AddTenant(tid, chain)
	}

	// v2.8.0 B-1.P2-a: stamp the last-successful-scan gauge for the e2e
	// harness anchor T1 + production stuck-scanner detection. Stamped only
	// on success — error returns above leave the gauge at its previous
	// value so a transient failure doesn't look like a completion.
	SetLastScanComplete(time.Now())
	return tenants, defaults, hashes, mtimes, graph, nil
}

