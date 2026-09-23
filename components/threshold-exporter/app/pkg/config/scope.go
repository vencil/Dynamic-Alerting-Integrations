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
// at a time. This file turns "directory-of-tenants" into
// "list-of-EffectiveConfig" over ONE ScanDirTree walk (tree_scan.go, the
// exporter's own walker; W2, #1677): the in-scope tenants are read off
// the scan's files, and each is resolved from the same scan by the
// resolver ResolveEffective uses — no re-walk per tenant.
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
// from disk as-is (once: every tenant is resolved from the bytes of
// the one scan, so the result describes one snapshot of the tree). CI flows running on a PR head commit see the
// post-edit state. Pre-commit hooks should run after `git add`
// since the disk state is what gets read; staged-but-not-checked-in
// edits only show up if the caller's working tree has them.
// Speculative simulation (apply a hypothetical edit without writing
// it to disk first) is out of scope here — that's the in-memory
// path C-7b /simulate handles for one tenant at a time.

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
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
//   - a tenant ID under the scope is defined in two different files
//     (returned as the raw *DuplicateTenantError, matching
//     ResolveEffective's loud-failure stance — duplicates are usually a
//     copy-paste bug). A duplicate that involves no in-scope tenant does
//     not fail the scope.
//   - any in-scope tenant fails to resolve (e.g. a tenant body that is
//     not a mapping), wrapped as `resolve tenant %q: ...`.
//
// A tenant file that is unreadable or not valid YAML is not an error: the
// walker logs (here: discards) and skips it, as the exporter does.
//
// configDir and scopeDir are both symlink-resolved (AbsScanRoot) before
// the containment check, so a symlinked --config-dir and a --scope spelled
// through either the link or the real path describe the same tree.
//
// A scope that contains zero tenants is NOT an error — Tenants
// will simply be nil. The caller (CLI) prints a friendly message
// and exits success in that case (vacuously safe defaults change).
func ScopeEffective(configDir, scopeDir string) (*ScopedTenants, error) {
	// The configDir checks keep their historical messages (callers and the
	// CLI print them); ScanDirTree below repeats the same stat on the same
	// resolved path, so the two cannot disagree.
	absRoot := AbsScanRoot(configDir)
	info, err := os.Stat(absRoot)
	if err != nil {
		return nil, fmt.Errorf("stat configDir %q: %w", absRoot, err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("configDir %q is not a directory", absRoot)
	}

	// ONE walk, the exporter's own (W2, #1677): tenant attribution, the
	// defaults set and the bytes all come from this scan.
	scan, err := ScanDirTree(absRoot, nil, nil, discardLogger)
	if err != nil {
		return nil, err
	}
	absRoot = scan.AbsRoot

	// ⛔ The scope is symlink-resolved exactly like the root. Comparing a
	// resolved root with an unresolved scope made every mixed spelling fail
	// (#1677 F1, measured: root=link + scope=link/sub → "tenant not found";
	// root=link + scope=real/sub and root=real + scope=link/sub →
	// "outside configDir").
	absScope := absRoot
	if scopeDir != "" {
		absScope = AbsScanRoot(scopeDir)
	}

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

	// In-scope tenants: every tenant declared by a kept file at-or-below
	// absScope. Read off scan.Files (not scan.Tenants, which is nil under a
	// duplicate anywhere in the tree), so a duplicate fails the scope iff an
	// IN-SCOPE tenant is involved — a duplicate entirely outside the scope
	// does not. A hidden scope yields zero tenants: the walker prunes hidden
	// directories, so no kept file lives under it.
	inScope := make(map[string]struct{})
	for _, f := range scan.Files {
		if len(f.TenantIDs) == 0 || !pathAtOrBelow(f.AbsPath, absScope, rel == ".") {
			continue
		}
		for _, id := range f.TenantIDs {
			inScope[id] = struct{}{}
		}
	}
	if len(inScope) == 0 {
		return &ScopedTenants{}, nil
	}

	// Sort tenant IDs for deterministic output. The CLI's exit-code
	// decision and the guard library's findings sort don't depend
	// on this order, but stable output makes diff-based golden
	// tests possible.
	tenantIDs := make([]string, 0, len(inScope))
	for id := range inScope {
		tenantIDs = append(tenantIDs, id)
	}
	sort.Strings(tenantIDs)

	// Resolve every tenant from the SAME scan: no re-walk per tenant (this
	// used to call ResolveEffective per tenant, re-walking configDir each
	// time — O(files × tenants)); the defaults selection is computed once.
	resolver := newEffectiveResolver(scan)
	out := &ScopedTenants{
		Tenants: make([]*EffectiveConfig, 0, len(tenantIDs)),
	}
	seenFiles := make(map[string]struct{}, len(tenantIDs))
	for _, id := range tenantIDs {
		ec, err := resolver.resolve(id)
		if err != nil {
			// A duplicate is returned as the raw typed error (the message
			// operators already know); anything else keeps the per-tenant
			// wrap and still fails the whole scope — loud, not skipped.
			var dup *DuplicateTenantError
			if errors.As(err, &dup) {
				return nil, err
			}
			return nil, fmt.Errorf("resolve tenant %q: %w", id, err)
		}
		out.Tenants = append(out.Tenants, ec)
		seenFiles[ec.SourceFile] = struct{}{}
	}

	// SourceFiles, root-relative for friendliness, sorted.
	files := make([]string, 0, len(seenFiles))
	for f := range seenFiles {
		files = append(files, f)
	}
	sort.Strings(files)
	out.SourceFiles = files

	return out, nil
}

// pathAtOrBelow reports whether p lies under dir (both Clean absolute
// paths). wholeTree short-circuits the root scope.
func pathAtOrBelow(p, dir string, wholeTree bool) bool {
	if wholeTree {
		return true
	}
	return strings.HasPrefix(p, dir+string(filepath.Separator))
}
