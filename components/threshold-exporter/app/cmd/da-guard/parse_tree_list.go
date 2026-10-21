package main

// parse-tree --list-only (ADR-036 §2, §7 step 9; #3206): which conf.d files
// there are, what each one's NAME makes it, and who reads it — without
// reading or parsing a byte of any file. For a reader that needs the file
// list and not the content (a tool that walks conf.d to copy, hash or count
// files), so it has no reason to keep a name rule of its own.
//
// ⛔ NO RULES OF ITS OWN, as for the tree mode: the walk is
// config.ScanDirTreeNames (the exporter's walker, files opened and closed
// but never read), names are pkg/confdname, readers and carrier selection
// are tree mode's readersFor / DefaultsCarriers. A separate schema value, so
// a reader of the full document cannot take this one (no "files") for a
// tree whose files are all unreadable or empty.

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

// parseTreeListSchema names the --list-only document's shape; a reader
// refuses any other value, as for parseTreeSchema.
const parseTreeListSchema = "da-guard.parse-tree-list/v1"

// Closed set of parseTreeListEntry.Class values.
const (
	// classTenant: a config file name that is neither reserved (`_`) nor
	// hidden — a tenant file; TenantNamed is the id its name carries.
	classTenant = "tenant"
	// classDefaults: `_defaults.yaml` / `.yml`, any case.
	classDefaults = "defaults"
	// classPlatform: any other `_`-prefixed config file name.
	classPlatform = "platform"
	// classDirectory: not a file — a config-named directory, a symlink to a
	// directory the walk does not follow, or a directory that could not be
	// listed (tree listing only).
	classDirectory = "directory"
	// classHidden: `.`-prefixed (names mode only; the walk never lists one).
	classHidden = "hidden"
	// classNotConfigName: no `.yaml` / `.yml` extension (names mode only).
	classNotConfigName = "not_config_name"
)

// parseTreeListDoc is the --list-only document on stdout.
type parseTreeListDoc struct {
	Schema         string `json:"schema"`
	DaGuardVersion string `json:"da_guard_version"`
	// Mode is "list" (--config-dir) or "names" (--names).
	Mode string `json:"mode"`
	// DefaultsCarriers is tree mode's: directory ("." for the root) → the
	// one defaults carrier its chain reads. Empty in names mode.
	DefaultsCarriers map[string]string `json:"defaults_carriers"`
	// Entries: every entry the walk records, sorted by Path (list mode), or
	// every given name in the order given (names mode).
	Entries []parseTreeListEntry `json:"entries"`
}

// parseTreeListEntry is one listed path or one given name.
type parseTreeListEntry struct {
	// Path is root-relative with '/' ("." for the root itself); in names
	// mode, the name as given.
	Path string `json:"path"`
	// Depth is the number of directories between the root and Path (0 for a
	// file in the root). Names mode: always 0.
	Depth int `json:"depth"`
	// Class is what the name makes the entry (closed set, class*).
	Class string `json:"class"`
	// TenantNamed is the tenant id the name carries (confdname.TenantNamedBy)
	// — set only for class "tenant". ⛔ The write plane's naming
	// convention, not who is served: the exporter takes tenant ids from the
	// `tenants:` keys inside the file.
	TenantNamed string `json:"tenant_named,omitempty"`
	// Readers is who reads it, as tree mode's "selected" says; [] when no
	// reader does (then NotRead says why). null in names mode, which has no
	// tree.
	Readers []string `json:"readers"`
	// NotRead is tree mode's not_read reason (unselected_defaults_carrier).
	NotRead string `json:"not_read,omitempty"`
	// Unreadable is set when the entry cannot be read: a kind from tree
	// mode's closed list that is decided without reading (read_failure,
	// not_regular_file) and a message for a person.
	Unreadable *parseTreeListProblem `json:"unreadable,omitempty"`
}

type parseTreeListProblem struct {
	Kind    parsetree.Kind `json:"kind"`
	Message string         `json:"message"`
}

func newParseTreeListDoc(mode string) *parseTreeListDoc {
	return &parseTreeListDoc{
		Schema: parseTreeListSchema, DaGuardVersion: Version, Mode: mode,
		DefaultsCarriers: map[string]string{}, Entries: []parseTreeListEntry{},
	}
}

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

// parseTreeListOf is the --list-only document of one ScanDirTreeNames walk.
// Selection, readers and every unreadable entry are tree mode's, decided the
// same way; only the content verdicts (which need the bytes) are absent.
func parseTreeListOf(scan *config.TreeScan) (*parseTreeListDoc, error) {
	doc := newParseTreeListDoc("list")
	byDir := scan.DefaultsCarriers().ByDir
	selected := make(map[string]bool, len(byDir))
	for dir, carrier := range byDir {
		selected[carrier] = true
		d, err := relTo(scan.AbsRoot, dir)
		if err != nil {
			return nil, err
		}
		c, err := relTo(scan.AbsRoot, carrier)
		if err != nil {
			return nil, err
		}
		doc.DefaultsCarriers[d] = c
	}
	fileEntry := func(rel string) parseTreeListEntry {
		class, id := nameClass(path.Base(rel))
		return parseTreeListEntry{Path: rel, Depth: depthOf(rel), Class: class, TenantNamed: id}
	}
	for _, rel := range scan.Keys {
		e := fileEntry(rel)
		e.Readers = readersFor(rel, !confdname.IsDefaults(path.Base(rel)) || selected[scan.Files[rel].AbsPath])
		if len(e.Readers) == 0 {
			e.Readers, e.NotRead = []string{}, notReadUnselectedCarrier
		}
		doc.Entries = append(doc.Entries, e)
	}
	for _, u := range scan.Unreadable {
		e := fileEntry(u.RelKey)
		e.Readers = []string{readerExporter, readerGenerator}
		if u.Reason == config.UnreadableWalkError {
			e.Class, e.TenantNamed = classDirectory, ""
		} else {
			e.Readers = readersFor(u.RelKey, true)
		}
		e.Unreadable = &parseTreeListProblem{Kind: unreadableKind(u.Reason), Message: unreadableMessage(u.Reason)}
		doc.Entries = append(doc.Entries, e)
	}
	if scan.RootWalkErr != nil {
		doc.Entries = append(doc.Entries, parseTreeListEntry{
			Path: ".", Class: classDirectory, Readers: []string{readerExporter, readerGenerator},
			Unreadable: &parseTreeListProblem{Kind: parsetree.KindReadFailure, Message: unreadableMessage(config.UnreadableWalkError)},
		})
	}
	for _, nf := range scan.NotFiles {
		readers, msg := notFileEntry(nf.RelKey, nf.Reason)
		doc.Entries = append(doc.Entries, parseTreeListEntry{
			Path: nf.RelKey, Depth: depthOf(nf.RelKey), Class: classDirectory, Readers: readers,
			Unreadable: &parseTreeListProblem{Kind: parsetree.KindNotRegularFile, Message: msg},
		})
	}
	sort.SliceStable(doc.Entries, func(i, j int) bool { return doc.Entries[i].Path < doc.Entries[j].Path })
	return doc, nil
}

// parseTreeNamesOf is names mode: each given base name classified by
// pkg/confdname alone — for names that are not on a disk this process can
// walk (e.g. the keys of a ConfigMap built from a conf.d).
func parseTreeNamesOf(names []string) (*parseTreeListDoc, error) {
	doc := newParseTreeListDoc("names")
	for _, n := range names {
		if n == "" || strings.ContainsAny(n, "/\\") || !utf8.ValidString(n) {
			return nil, fmt.Errorf("--names: %q is not a base name (empty, holds a separator, or not valid UTF-8)", n)
		}
		class, id := nameClass(n)
		doc.Entries = append(doc.Entries, parseTreeListEntry{Path: n, Class: class, TenantNamed: id})
	}
	return doc, nil
}

// runParseTreeList is `parse-tree --list-only`, after the flags are parsed.
func runParseTreeList(f *parseTreeFlags, stdin io.Reader, stdout io.Writer, fail func(string, ...any) int) int {
	var doc *parseTreeListDoc
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
		doc, err = parseTreeNamesOf(names)
	} else {
		var scan *config.TreeScan
		scan, err = config.ScanDirTreeNames(f.configDir, log.New(io.Discard, "", 0))
		if err == nil {
			doc, err = parseTreeListOf(scan)
		}
	}
	if err != nil {
		return fail("%v", err)
	}
	for _, e := range doc.Entries {
		if !utf8.ValidString(e.Path) {
			return fail("the path %q is not valid UTF-8; JSON cannot carry it", e.Path)
		}
	}
	enc := json.NewEncoder(stdout)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(doc); err != nil {
		return fail("%v", err)
	}
	return exitOK
}
