package main

// parse-tree's per-path index ("entries"), and the two modes that write it
// alone: --list-only --config-dir (list mode) and --list-only --names (names
// mode) (ADR-036 §2, §7 step 9; #3206). List mode answers "which conf.d
// files are there, what is each one, who reads it" without reading or
// parsing a byte of any file — for a reader that needs the file list and not
// the content (a tool that walks conf.d to copy, hash or count files), so it
// has no reason to keep a name rule of its own.
//
// ⛔ NO RULES OF ITS OWN, as for the tree mode: the walk is
// config.ScanDirTreeNames (the exporter's walker, files opened and closed
// but never read), names are pkg/confdname, what a path IS is config.Role,
// readers and carrier selection are tree mode's readersFor /
// DefaultsCarriers. walkEntries is the ONE builder of a walk's entries,
// shared with tree mode: the two modes cannot list a path differently.

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"path"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/vencil/threshold-exporter/pkg/confdname"
	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

// Closed set of parseTreeEntry.Class values.
const (
	// classTenant: a config file name that is neither reserved (`_`) nor
	// hidden — a tenant file; TenantNamed is the id its name carries.
	classTenant = "tenant"
	// classDefaults: `_defaults.yaml` / `.yml`, any case.
	classDefaults = "defaults"
	// classPlatform: any other `_`-prefixed config file name.
	classPlatform = "platform"
	// classDirectory: a directory, whatever its name — a config-named
	// directory, a symlink to a directory the walk does not follow, or a
	// directory that could not be listed.
	classDirectory = "directory"
	// classHidden: `.`-prefixed, or under a `.` directory (file and names
	// mode only; the walk never lists one).
	classHidden = "hidden"
	// classNotConfigName: no `.yaml` / `.yml` extension (file and names mode
	// only).
	classNotConfigName = "not_config_name"
)

// nameClass is what a base name alone makes a file, and the tenant id it
// carries (classTenant only).
func nameClass(base string) (string, string) {
	switch {
	case confdname.IsHidden(base):
		return classHidden, ""
	case !confdname.IsScanned(base):
		return classNotConfigName, ""
	case confdname.IsDefaults(base):
		return classDefaults, ""
	case confdname.IsReserved(base):
		return classPlatform, ""
	}
	id, _ := confdname.TenantNamedBy(base)
	return classTenant, id
}

func depthOf(rel string) int {
	if rel == "." {
		return 0
	}
	return strings.Count(rel, "/")
}

// nameEntry is the entry the NAME of rel makes, before any verdict on disk:
// role, readers and problem are the caller's. A path under a hidden
// directory is class hidden whatever its own name (file mode).
func nameEntry(rel string) parseTreeEntry {
	base := path.Base(rel)
	class, id := nameClass(base)
	if class != classHidden && confdname.HasHiddenSegment(rel) {
		class, id = classHidden, ""
	}
	return parseTreeEntry{Path: rel, Depth: depthOf(rel), Class: class, TenantNamed: id,
		DefaultsDocument: class != classHidden && confdname.IsDefaultsDocument(base)}
}

// withRole sets the entry's role; an entry that is not a config file is a
// document of no schema (DefaultsDocument false), and a directory has no
// name class.
func withRole(e parseTreeEntry, role config.Role) parseTreeEntry {
	e.Role = role
	if role != config.RoleConfig {
		e.DefaultsDocument = false
	}
	if role == config.RoleUnlistableDir {
		e.Class, e.TenantNamed = classDirectory, ""
	}
	return e
}

// unreadableAsEntry is the entry of one walk record of TreeScan.Unreadable.
func unreadableAsEntry(u config.UnreadableFile) parseTreeEntry {
	e := withRole(nameEntry(u.RelKey), u.Role())
	if u.Reason == config.UnreadableWalkError {
		e.Readers = []string{readerExporter, readerGenerator}
	} else {
		// Never an unselected carrier: selection runs over the files the
		// walk could read (the generator's readable_carriers too).
		e.Readers = readersFor(u.RelKey, true)
	}
	e.Problem = &parseTreeProblem{Kind: unreadableKind(u.Reason), Message: unreadableMessage(u.Reason)}
	return e
}

// notFileAsEntry is the entry of one walk record of TreeScan.NotFiles.
func notFileAsEntry(nf config.NotFile) parseTreeEntry {
	e := withRole(nameEntry(nf.RelKey), nf.Role())
	readers, msg := notFileEntry(nf.RelKey, nf.Reason)
	e.Class, e.TenantNamed, e.Readers = classDirectory, "", readers
	e.Problem = &parseTreeProblem{Kind: parsetree.KindNotRegularFile, Message: msg}
	return e
}

// walkEntries fills idx's defaults_carriers and entries from one walk: every
// file it keeps, every entry it dropped, every config-named path that is not
// a file, and the root when it could not be listed — sorted by path. The
// problems are those decided without reading; tree mode adds the content
// problems of the files it reads.
func walkEntries(scan *config.TreeScan, idx *parseTreeIndex) error {
	byDir := scan.DefaultsCarriers().ByDir
	selected := make(map[string]bool, len(byDir))
	for dir, carrier := range byDir {
		selected[carrier] = true
		d, err := relTo(scan.AbsRoot, dir)
		if err != nil {
			return err
		}
		c, err := relTo(scan.AbsRoot, carrier)
		if err != nil {
			return err
		}
		idx.DefaultsCarriers[d] = c
	}
	for _, rel := range scan.Keys {
		e := withRole(nameEntry(rel), config.RoleConfig)
		e.Readers = readersFor(rel, !confdname.IsDefaults(path.Base(rel)) || selected[scan.Files[rel].AbsPath])
		if len(e.Readers) == 0 {
			e.Readers, e.NotRead = []string{}, notReadUnselectedCarrier
		}
		idx.Entries = append(idx.Entries, e)
	}
	for _, u := range scan.Unreadable {
		idx.Entries = append(idx.Entries, unreadableAsEntry(u))
	}
	if scan.RootWalkErr != nil {
		e := withRole(parseTreeEntry{Path: "."}, config.RootWalkRole)
		e.Readers = []string{readerExporter, readerGenerator}
		e.Problem = &parseTreeProblem{Kind: parsetree.KindReadFailure,
			Message: unreadableMessage(config.UnreadableWalkError)}
		idx.Entries = append(idx.Entries, e)
	}
	for _, nf := range scan.NotFiles {
		idx.Entries = append(idx.Entries, notFileAsEntry(nf))
	}
	sort.SliceStable(idx.Entries, func(i, j int) bool { return idx.Entries[i].Path < idx.Entries[j].Path })
	return nil
}

// parseTreeListOf is the list-mode document of one ScanDirTreeNames walk.
func parseTreeListOf(scan *config.TreeScan) (*parseTreeIndex, error) {
	idx := newParseTreeIndex("list")
	if err := walkEntries(scan, &idx); err != nil {
		return nil, err
	}
	return &idx, nil
}

// parseTreeNamesOf is names mode: each given base name classified by
// pkg/confdname alone — for names that are not on a disk this process can
// walk (e.g. the keys of a ConfigMap built from a conf.d). No role, no
// readers: there is no disk and no tree.
func parseTreeNamesOf(names []string) (*parseTreeIndex, error) {
	idx := newParseTreeIndex("names")
	for _, n := range names {
		if n == "" || strings.ContainsAny(n, "/\\") || !utf8.ValidString(n) {
			return nil, fmt.Errorf("--names: %q is not a base name (empty, holds a separator, or not valid UTF-8)", n)
		}
		idx.Entries = append(idx.Entries, nameEntry(n))
	}
	return &idx, nil
}

// runParseTreeList is `parse-tree --list-only`, after the flags are parsed.
func runParseTreeList(f *parseTreeFlags, stdin io.Reader, stdout io.Writer, fail func(string, ...any) int) int {
	var idx *parseTreeIndex
	var err error
	if f.names != "" {
		var raw []byte
		if f.names == "-" {
			raw, err = io.ReadAll(stdin)
		} else {
			raw, err = os.ReadFile(f.names) // #nosec G304 -- the caller names the file to read
		}
		if err != nil {
			return fail("--names: %v", err)
		}
		var names []string
		if err := json.Unmarshal(raw, &names); err != nil {
			return fail("--names: not a JSON array of strings: %v", err)
		}
		idx, err = parseTreeNamesOf(names)
	} else {
		var scan *config.TreeScan
		scan, err = config.ScanDirTreeNames(f.configDir, log.New(io.Discard, "", 0))
		if err == nil {
			idx, err = parseTreeListOf(scan)
		}
	}
	if err != nil {
		return fail("%v", err)
	}
	if err := writeParseTree(stdout, idx.Entries, idx); err != nil {
		return fail("%v", err)
	}
	return exitOK
}
