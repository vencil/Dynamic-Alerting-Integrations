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
	"log"
	"os"
	"path"
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

	// ParseFailed is every file the exporter's own load of this tree drops
	// (LoadDir's parseFailed — FlatBuild.ParseFailed) that bears on the
	// scope: at-or-below it, or a `_` file in a directory above it. Root-
	// relative slash paths, sorted; nil when there are none (#2123, #2179).
	// See scopeParseFailed.
	//
	// ⛔ Not an error of ScopeEffective, on purpose: the walker skips such a
	// file and serves the rest of the tree, and callers that mirror the
	// exporter rely on err == nil here. But a skipped file declares no
	// tenant, so Tenants silently omits whatever it held — a caller that
	// turns Tenants into a verdict must read this field too, or a broken
	// file reads as "nothing in scope" (da-guard's vacuously-safe exit 0,
	// before #2123).
	ParseFailed []string

	// DefaultsFiles is every defaults carrier a defaults chain reads (the
	// per-directory selection, DefaultsCarriers().ByDir) that bears on the
	// scope — at-or-below it, or in a directory above it — with the bytes
	// the scan read, sorted by Name (#2291). da-guard checks them for
	// routing keys the route generator never reads
	// (routingpolicy.UnreadRouting). A file in ParseFailed is listed too;
	// the caller skips it.
	DefaultsFiles []DefaultsFile

	// Unreadable is every entry of the walk's TreeScan.Unreadable — a
	// config-named file whose stat or read failed (a dangling symlink, a file
	// the process may not read) and a directory below the root it could not
	// list — that bears on the scope. A file (stat_error / read_error) bears
	// by ParseFailed's rule: at or below the scope, or a `_` file in a
	// directory above it. A directory (walk_error) bears only by the
	// directory rule: it is the scope, lies below it, or contains it — a
	// `_`-named directory beside the scope does not. Same entries, same order (by RelKey) as the exporter's own load
	// (LoadReport.Unreadable) over the whole tree; nil when there are none
	// (#2588). A symlink to a directory is not listed.
	//
	// ⛔ Not an error, like ParseFailed and for the same reason: the walker
	// skips the entry and serves the rest. Tenants silently omits the tenants
	// such a file declares, and an effective config silently lacks what an
	// unreadable `_defaults.yaml` would have given it — a caller that turns
	// this result into a verdict must read this field too.
	//
	// ⚠️ Also filled when ScopeEffective returns a *DecodeError: the result
	// then carries this field (and ParseFailed, DefaultsFiles) and no tenant,
	// so a run stopped by one undecodable file still names the unreadable
	// ones. Every other error returns a nil result.
	Unreadable []UnreadableFile
}

// DefaultsFile is one defaults carrier of a scan: its root-relative slash
// path and its bytes.
type DefaultsFile struct {
	Name string
	Data []byte
}

// ScopeEffective resolves the effective config for every tenant
// whose YAML file lives at-or-below scopeDir, using configDir as
// the conf.d root for chain resolution.
//
// configDir is an absolute path or one relative to the working directory.
// scopeDir is an absolute path or one relative to configDir — NOT to the
// working directory (#2588: `--config-dir conf.d --scope db`, "." = the
// root). It must be at-or-below configDir after Clean. An empty scopeDir
// defaults to configDir (whole tree).
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
// walker logs (here: discards) and skips it, as the exporter does. A file
// the decode rejects is listed in ScopedTenants.ParseFailed (#2123), and a
// file or directory the walk cannot stat, read or list in
// ScopedTenants.Unreadable (#2588), so the caller can still tell "no
// tenants" from "tenants it could not read".
//
// A *DecodeError is returned WITH a non-nil result that holds no tenant
// (see ScopedTenants.Unreadable); every other error with a nil one.
//
// configDir and scopeDir are both symlink-resolved (AbsScanRoot) before
// the containment check, so a symlinked --config-dir and a --scope spelled
// through either the link or the real path describe the same tree.
//
// A scope that contains zero tenants is NOT an error — Tenants
// will simply be nil. The caller (CLI) prints a friendly message
// and exits success in that case (vacuously safe defaults change).
func ScopeEffective(configDir, scopeDir string) (*ScopedTenants, error) {
	return scopeEffective(configDir, scopeDir, false)
}

// EffectiveTree is ScopeEffective over the whole tree (scopeDir = configDir)
// with every tenant's KeySources filled (#2564): the per-tenant /effective
// answer — the same resolver, the same one walk, the same merge — plus, per
// key, the layer and file its value came from. `da-guard effective` prints
// it for the Python readers.
//
// Errors are ScopeEffective's, plus one: a tree with no .yaml file at all is
// refused with the exporter's own load's message (LoadDir), since the
// exporter refuses to serve it — an empty result would read as "no tenants".
func EffectiveTree(configDir string) (*ScopedTenants, error) {
	return scopeEffective(configDir, "", true)
}

// scopeEffective is ScopeEffective; wholeTree is EffectiveTree's mode (key
// attribution on, an empty tree refused).
func scopeEffective(configDir, scopeDir string, wholeTree bool) (*ScopedTenants, error) {
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
	// defaults set and the bytes all come from this scan. A cold scan (no
	// prior) that decodes every tenant file in full since #1957; reusing a
	// prior across runs is #1977.
	scan, err := ScanDirTree(absRoot, nil, nil, discardLogger)
	if err != nil {
		return nil, err
	}
	absRoot = scan.AbsRoot
	if wholeTree && len(scan.Files) == 0 {
		return nil, fmt.Errorf("no .yaml files found in %s", configDir)
	}
	// ⛔ A root the walk cannot list is not an empty tree (#2588). The walker
	// records no Unreadable entry for the root itself (TreeScan.RootWalkErr),
	// so the scoped mode — where an empty tree is a valid, vacuously-safe
	// scope — would read it as "nothing in scope". Refuse it, as the
	// exporter's load does.
	if len(scan.Files) == 0 && scan.RootWalkErr != nil {
		return nil, fmt.Errorf("cannot list configDir %q: %w", absRoot, scan.RootWalkErr)
	}

	// ⛔ The scope is symlink-resolved exactly like the root. Comparing a
	// resolved root with an unresolved scope made every mixed spelling fail
	// (#1677 F1, measured: root=link + scope=link/sub → "tenant not found";
	// root=link + scope=real/sub and root=real + scope=link/sub →
	// "outside configDir").
	absScope := absRoot
	if scopeDir != "" {
		// A relative scope is relative to configDir (#2588), joined to it as
		// given so the symlink resolution below treats both spellings alike.
		s := scopeDir
		if !filepath.IsAbs(s) {
			s = filepath.Join(configDir, s)
		}
		absScope = AbsScanRoot(s)
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
		if !pathAtOrBelow(f.AbsPath, absScope, rel == ".") {
			continue
		}
		for _, id := range f.TenantIDs {
			inScope[id] = struct{}{}
		}
	}
	parseFailed, err := scopeParseFailed(scan, filepath.ToSlash(rel))
	if err != nil {
		return nil, err
	}
	defaultsFiles := scopeDefaultsFiles(scan, filepath.ToSlash(rel))
	unreadable := scopeUnreadable(scan, filepath.ToSlash(rel))
	if len(inScope) == 0 {
		return &ScopedTenants{ParseFailed: parseFailed, DefaultsFiles: defaultsFiles, Unreadable: unreadable}, nil
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
	resolver.withSources = wholeTree
	out := &ScopedTenants{
		Tenants:       make([]*EffectiveConfig, 0, len(tenantIDs)),
		ParseFailed:   parseFailed,
		DefaultsFiles: defaultsFiles,
		Unreadable:    unreadable,
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
			err = fmt.Errorf("resolve tenant %q: %w", id, err)
			// A file that does not decode stops the scope; the files the walk
			// could not read are still the caller's to name (#2588).
			var de *DecodeError
			if errors.As(err, &de) {
				return &ScopedTenants{ParseFailed: parseFailed, DefaultsFiles: defaultsFiles, Unreadable: unreadable}, err
			}
			return nil, err
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

// scopeParseFailed is ScopedTenants.ParseFailed: the files the exporter's own
// load of this scan drops (FlatBuild.ParseFailed via loadDirBuild — the list
// LoadDir returns as parseFailed), kept when they bear on the scope (#2179).
//
// ⛔ ONE VERDICT. Which files count as broken is the exporter's build's
// answer, never re-judged here: a root `_defaults.yaml` with a type error, a
// root `_platform.yaml` / `_profiles.yaml` the decode rejects, a nested
// `_defaults.yaml` with a syntax error, a tenant file the walker rejected.
// A nested `_defaults.yaml` whose values have the wrong type is NOT in the
// list — the exporter skips that key and keeps the file.
//
// scopeRel is the scope relative to the root, slash-separated ("." = whole
// tree). A dropped file bears on the scope when it lies at-or-below it, or it
// is a `_` file in a directory above it (the root's platform files, a chain
// `_defaults.yaml`) — those shape every tenant under the scope.
func scopeParseFailed(scan *TreeScan, scopeRel string) ([]string, error) {
	if len(scan.Files) == 0 {
		return nil, nil // the exporter refuses an empty tree; nothing was dropped
	}
	// ⚠️ log.Printf, not discardLogger, for the profile WARNs (#2513): they
	// reached the process log here before the build took its logger for them,
	// and on da-guard's stderr that line is the only place a tenant electing
	// an unknown profile is named (the report does not list it).
	built, err := loadDirBuild(scan, scan.AbsRoot, discardLogger, log.Printf)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, key := range built.ParseFailed {
		if bearsOnScope(key, scopeRel) {
			out = append(out, key)
		}
	}
	sort.Strings(out)
	return out, nil
}

// scopeDefaultsFiles is ScopedTenants.DefaultsFiles: the selected carrier of
// each directory (the chain rule), kept when it bears on the scope and the
// scan holds its bytes.
func scopeDefaultsFiles(scan *TreeScan, scopeRel string) []DefaultsFile {
	var out []DefaultsFile
	for _, abs := range scan.DefaultsCarriers().ByDir {
		rel, err := filepath.Rel(scan.AbsRoot, abs)
		if err != nil {
			continue
		}
		key := filepath.ToSlash(rel)
		f, ok := scan.Files[key]
		if !ok || f.Data == nil || !bearsOnScope(key, scopeRel) {
			continue
		}
		out = append(out, DefaultsFile{Name: key, Data: f.Data})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// scopeUnreadable is ScopedTenants.Unreadable: the scan's Unreadable entries
// that bear on the scope. A file follows bearsOnScope (ParseFailed's rule); a
// directory the walk could not list (walk_error) bears only when it is the
// scope, lies below it, or contains it — everything under it is lost. A
// directory never takes the `_`-file branch of bearsOnScope: an unlistable
// `_archive/` beside the scope holds nothing the scope reads.
func scopeUnreadable(scan *TreeScan, scopeRel string) []UnreadableFile {
	var out []UnreadableFile
	for _, u := range scan.Unreadable { // sorted by RelKey (ScanDirTree)
		var bears bool
		if u.Reason == UnreadableWalkError {
			bears = scopeRel == "." || u.RelKey == scopeRel ||
				strings.HasPrefix(u.RelKey, scopeRel+"/") || strings.HasPrefix(scopeRel+"/", u.RelKey+"/")
		} else {
			bears = bearsOnScope(u.RelKey, scopeRel)
		}
		if bears {
			out = append(out, u)
		}
	}
	return out
}

// bearsOnScope: key (a root-relative slash scan key) at-or-below scopeRel, or
// a `_` file in a directory that is an ancestor of scopeRel.
func bearsOnScope(key, scopeRel string) bool {
	if scopeRel == "." || strings.HasPrefix(key, scopeRel+"/") {
		return true
	}
	dir := path.Dir(key)
	if !strings.HasPrefix(scanKeyBase(key), "_") {
		return false
	}
	return dir == "." || strings.HasPrefix(scopeRel+"/", dir+"/")
}

// pathAtOrBelow reports whether p lies under dir (both Clean absolute
// paths). wholeTree short-circuits the root scope.
func pathAtOrBelow(p, dir string, wholeTree bool) bool {
	if wholeTree {
		return true
	}
	return strings.HasPrefix(p, dir+string(filepath.Separator))
}
