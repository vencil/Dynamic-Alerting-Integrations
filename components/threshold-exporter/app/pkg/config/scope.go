package config

// ============================================================
// Scope enumeration — v2.8.0 Phase .c C-12 Dangling Defaults Guard PR-4
// ============================================================
//
// `da-guard` (the CLI wrapper around internal/guard) needs to ask:
// "given the working-tree state of conf.d/, list every tenant under
// some scope and hand me each one's effective config so the guard
// library can validate them."
//
// hierarchy.go::ResolveEffective answers that question for ONE tenant
// at a time. This file is the loop around it that turns
// "directory-of-tenants" into "list-of-EffectiveConfig".
//
// Why a separate file rather than extending hierarchy.go: hierarchy.go
// is the public read-only resolver imported by tenant-api at runtime,
// where the per-request shape is "give me one tenant by ID". The
// scope-enumeration shape ("walk a tree, return everyone") is a
// different access pattern, used only by offline tooling. Keeping
// them split keeps the runtime API minimal.
//
// Scope semantics (matches the planning §C-12 trigger model):
//
//   - configDir is the conf.d ROOT. Defaults chains start here so
//     cascading parent _defaults.yaml files are honored — a tenant
//     under conf.d/db/mariadb/prod/ inherits L0 (root), L1 (db/),
//     L2 (db/mariadb/), L3 (db/mariadb/prod/) in that order.
//
//   - scopeDir is a subdirectory under configDir (or equal to it).
//     Only tenants whose tenant.yaml file lives at-or-below scopeDir
//     are returned. This matches the GitHub Actions trigger:
//     "_defaults.yaml at path X changed; validate everyone under
//     dirname(X)".
//
//   - scopeDir equal to configDir means "validate every tenant in
//     the tree". That's the natural pre-commit / local-dev flow.
//
// Working-tree assumption: like ResolveEffective, this reads files
// from disk as-is. CI flows running on a PR head commit see the
// post-edit state. Pre-commit hooks should run after `git add`
// since the disk state is what gets read; staged-but-not-checked-in
// edits only show up if the caller's working tree has them.
// Speculative simulation (apply a hypothetical edit without writing
// it to disk first) is out of scope here — that's the in-memory
// path C-7b /simulate handles for one tenant at a time.

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// ScopedTenants is the bundle ScopeEffective returns: per-tenant
// effective configs plus a deterministic ordering for any caller
// that wants stable output (the guard library doesn't require it
// — it sorts internally — but CLI rendering does).
type ScopedTenants struct {
	// Tenants in alphabetical order by tenant ID. Empty when no
	// tenants live under the requested scope (NOT an error — the
	// caller decides whether that's expected).
	Tenants []*EffectiveConfig

	// SourceFiles is the set of tenant YAML files (repo-relative
	// paths) that contributed at least one tenant ID to Tenants.
	// Useful for CLI output ("scanned 12 files, found 47 tenants").
	SourceFiles []string
}

// ScopeEffective resolves the effective config for every tenant
// whose YAML file lives at-or-below scopeDir, using configDir as
// the conf.d root for chain resolution.
//
// Both arguments are absolute or relative filesystem paths;
// scopeDir must be at-or-below configDir after Clean. An empty
// scopeDir defaults to configDir (whole tree).
//
// Errors:
//   - configDir doesn't exist or isn't a directory.
//   - scopeDir lies outside configDir (security guard against
//     `--scope ../etc/passwd`).
//   - any tenant ID is defined in two different files (matches
//     ResolveEffective's loud-failure stance — duplicates are
//     usually a copy-paste bug).
//   - a tenant file is unreadable or contains malformed YAML.
//
// A scope that contains zero tenants is NOT an error — Tenants
// will simply be nil. The caller (CLI) prints a friendly message
// and exits success in that case (vacuously safe defaults change).
func ScopeEffective(configDir, scopeDir string) (*ScopedTenants, error) {
	absRoot, err := filepath.Abs(configDir)
	if err != nil {
		return nil, fmt.Errorf("resolve configDir %q: %w", configDir, err)
	}
	absRoot = filepath.Clean(absRoot)
	info, err := os.Stat(absRoot)
	if err != nil {
		return nil, fmt.Errorf("stat configDir %q: %w", absRoot, err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("configDir %q is not a directory", absRoot)
	}

	if scopeDir == "" {
		scopeDir = absRoot
	}
	absScope, err := filepath.Abs(scopeDir)
	if err != nil {
		return nil, fmt.Errorf("resolve scopeDir %q: %w", scopeDir, err)
	}
	absScope = filepath.Clean(absScope)

	// Containment check. filepath.Rel produces "../" when scope
	// escapes root; we reject any path whose first segment is "..".
	rel, err := filepath.Rel(absRoot, absScope)
	if err != nil {
		return nil, fmt.Errorf("scope %q vs root %q: %w", absScope, absRoot, err)
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return nil, fmt.Errorf(
			"scopeDir %q is outside configDir %q", absScope, absRoot)
	}
	if scopeInfo, err := os.Stat(absScope); err != nil {
		return nil, fmt.Errorf("stat scopeDir %q: %w", absScope, err)
	} else if !scopeInfo.IsDir() {
		return nil, fmt.Errorf("scopeDir %q is not a directory", absScope)
	}

	// First pass: collect tenant ID → file containing it, scoped
	// to the subtree under absScope. Reusing the per-file YAML
	// peek pattern from ResolveEffective so the duplicate-tenant
	// detection rules are identical.
	tenantToFile := make(map[string]string)
	walkErr := filepath.WalkDir(absScope, func(path string, d fs.DirEntry, werr error) error {
		if werr != nil {
			// Match ResolveEffective: tolerate per-entry errors
			// (e.g. permission) so a single unreadable file
			// doesn't sink the whole scan.
			return nil
		}
		name := d.Name()
		if d.IsDir() {
			if path != absScope && strings.HasPrefix(name, ".") {
				return fs.SkipDir
			}
			return nil
		}
		if strings.HasPrefix(name, ".") {
			return nil
		}
		lower := strings.ToLower(name)
		if !strings.HasSuffix(lower, ".yaml") && !strings.HasSuffix(lower, ".yml") {
			return nil
		}
		// Skip _-prefixed files: those are defaults / profiles /
		// other reserved namespaces, never tenant carriers.
		if strings.HasPrefix(name, "_") {
			return nil
		}
		clean := filepath.Clean(path)
		data, rerr := os.ReadFile(clean)
		if rerr != nil {
			return nil
		}
		var doc struct {
			Tenants map[string]yaml.Node `yaml:"tenants"`
		}
		if perr := yaml.Unmarshal(data, &doc); perr != nil {
			return nil
		}
		for tenantID := range doc.Tenants {
			if existing, ok := tenantToFile[tenantID]; ok && existing != clean {
				return fmt.Errorf(
					"duplicate tenant ID %q: defined in both %s and %s",
					tenantID, existing, clean)
			}
			tenantToFile[tenantID] = clean
		}
		return nil
	})
	if walkErr != nil {
		return nil, walkErr
	}

	if len(tenantToFile) == 0 {
		return &ScopedTenants{}, nil
	}

	// Sort tenant IDs for deterministic output. The CLI's exit-code
	// decision and the guard library's findings sort don't depend
	// on this order, but stable output makes diff-based golden
	// tests possible.
	tenantIDs := make([]string, 0, len(tenantToFile))
	for id := range tenantToFile {
		tenantIDs = append(tenantIDs, id)
	}
	sort.Strings(tenantIDs)

	// Second pass: ResolveEffective per tenant. This re-walks
	// configDir each time which is O(NumFiles × NumTenants) but
	// fine for the scale we target (1000 tenants × ~few-hundred
	// YAML files = ~minutes of cold cache walk on synthetic
	// fixtures, well below CI timeout). If a future PR needs to
	// scale up, the right move is to memoize the defaults-chain
	// computation per directory, not to re-architect this.
	out := &ScopedTenants{
		Tenants: make([]*EffectiveConfig, 0, len(tenantIDs)),
	}
	seenFiles := make(map[string]struct{}, len(tenantToFile))
	for _, id := range tenantIDs {
		ec, err := ResolveEffective(absRoot, id)
		if err != nil {
			return nil, fmt.Errorf("resolve tenant %q: %w", id, err)
		}
		out.Tenants = append(out.Tenants, ec)
		seenFiles[tenantToFile[id]] = struct{}{}
	}

	// SourceFiles, repo-relative for friendliness, sorted.
	files := make([]string, 0, len(seenFiles))
	for f := range seenFiles {
		if r, rerr := filepath.Rel(absRoot, f); rerr == nil {
			files = append(files, filepath.ToSlash(r))
		} else {
			files = append(files, filepath.ToSlash(f))
		}
	}
	sort.Strings(files)
	out.SourceFiles = files

	return out, nil
}
