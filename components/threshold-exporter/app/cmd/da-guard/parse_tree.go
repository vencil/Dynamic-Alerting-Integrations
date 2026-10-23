package main

// parse-tree: the conf.d "parse result" of ADR-036 §2, as JSON (#2897) —
// one entry per path the exporter's walk records (what the path is, who
// reads it, what stops a read), every readable file's content as yaml.v3
// decodes it, and, for the platform files, which render a key there reaches.
// Every Python reader of conf.d reads it (scripts/tools/_lib_parse_tree.py).
//
// ⛔ NO RULES OF ITS OWN. Which files exist is the exporter's walker
// (config.ScanDirTree: hidden names, extensions in any case, directories
// below the root, the one defaults carrier per directory); what each walked
// path IS — a config file, no file the walk can reach, a directory that could not
// be listed — is config.Role, the one verdict (#3206); which files the route
// generator reads besides is stated once, in readersFor, from
// `_grar_parse._parse_config_files`; what a file holds is pkg/parsetree.
// The one table of its own is rendered_by (rendered_by.go): which render a
// platform carrier reaches where it is written, held to the renders by
// tests/shared/test_parse_tree_rendered_by.py.
//
// ONE DOCUMENT SHAPE, FOUR MODES (schema da-guard.parse-tree/v2): tree and
// file mode read content ("files", "rendered_by"); list mode
// (--list-only --config-dir) is the same walk and the same "entries" without
// reading a byte; names mode (--list-only --names) classifies names that are
// not on a disk. Tree and list mode build "entries" with one function
// (walkEntries), so the two cannot disagree about a path.

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"path"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"github.com/vencil/threshold-exporter/pkg/confdname"
	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

// parseTreeCmd is the subcommand name: `da-guard parse-tree ...`.
const parseTreeCmd = "parse-tree"

// parseTreeSchema names the document's shape, in every mode. A reader
// refuses any other value (and a document without one), as for
// served-values. v1 (and the separate --list-only schema beside it) had no
// per-path verdict; both were never in a release.
const parseTreeSchema = "da-guard.parse-tree/v2"

// The readers a file can have, in the order an entry lists them.
const (
	readerExporter  = "exporter"
	readerGenerator = "generator"
)

// Closed set of parseTreeEntry.NotRead values.
const (
	// notReadUnselectedCarrier: a defaults carrier (`_defaults.yaml` /
	// `.yml`, any case) that its directory's chain does not select — at the
	// root or below it (#1674: every plane ignores it whole).
	notReadUnselectedCarrier = "unselected_defaults_carrier"
	// notReadHidden (file mode): the path, or a directory on it, starts
	// with '.'.
	notReadHidden = "hidden"
	// notReadNotConfigName (file mode): the name does not end in .yaml /
	// .yml (any case).
	notReadNotConfigName = "not_config_name"
	// notReadParentNotWalked (file mode): a directory on the path exists but
	// is not a real directory the walk descends — a symlink (followed by no
	// walk), a regular file, a dangling link.
	notReadParentNotWalked = "parent_not_walked"
)

// parseTreeIndex is what every mode writes: the envelope and the per-path
// entries. List and names mode write it alone.
type parseTreeIndex struct {
	Schema         string `json:"schema"`
	DaGuardVersion string `json:"da_guard_version"`
	// Mode is "tree" (--config-dir alone), "file" (--path / --input),
	// "list" (--list-only --config-dir) or "names" (--list-only --names).
	Mode string `json:"mode"`
	// DefaultsCarriers: directory ("." for the root) → the one defaults
	// carrier its chain reads. File mode: only --path's directory, as it is
	// once --path is written. Names mode: {}.
	DefaultsCarriers map[string]string `json:"defaults_carriers"`
	// Entries: tree and list mode, every path the walk records, sorted by
	// Path; file mode, --path alone; names mode, every given name in the
	// order given.
	Entries []parseTreeEntry `json:"entries"`
}

// parseTreeDoc is the tree- and file-mode document: the index plus content.
type parseTreeDoc struct {
	parseTreeIndex
	// Files: each readable file some reader reads (and, in file mode, the
	// text of --path whoever reads it) — parsetree.Content. A config entry
	// with readers is in Files exactly when it has no Problem.
	Files map[string]parsetree.Content `json:"files"`
	// RenderedBy (#2962): for each readable `_` file in Files, the platform
	// carriers it holds (renderedByKeys) → the readers whose render a change
	// to that key can alter; [] = no render reads it there. A key outside
	// renderedByKeys is absent (not answered).
	RenderedBy map[string]map[string][]string `json:"rendered_by"`
}

// parseTreeEntry is one walked path, the one --path, or one given name.
// Every field is written in every mode; a field a mode cannot answer is null
// (see MarshalJSON), so a reader can tell a document that lacks a field
// from one that answers "none".
type parseTreeEntry struct {
	// Path is root-relative with '/' ("." for the root itself); in names
	// mode, the name as given.
	Path string
	// Depth is the number of directories between the root and Path (0 for a
	// file in the root). Names mode: always 0.
	Depth int
	// Class is what the name makes the entry (closed set, class*);
	// "directory" for every entry that is one (whatever its name).
	Class string
	// TenantNamed is the tenant id the name carries (confdname.TenantNamedBy)
	// — class "tenant" only, else null. ⛔ The write plane's naming
	// convention, not who is served: the exporter takes tenant ids from the
	// `tenants:` keys inside the file.
	TenantNamed string
	// DefaultsDocument: the entry is a platform-defaults DOCUMENT
	// (confdname.IsDefaultsDocument: a `_defaults` prefix, wider than class
	// "defaults", the exact chain carrier — `_defaults-multidb.yaml` is class
	// "platform" with this true), held to the platform-defaults schema.
	// ⛔ False for every entry whose role is not "config" (a directory, a
	// FIFO, a dangling link hold no document any schema could validate).
	// Names mode answers by the name alone.
	DefaultsDocument bool
	// ConfigExtension: the base name carries a config file's extension
	// (`.yaml` / `.yml`, any case — confdname.SplitCarrier), whatever else
	// the name or the path is: true for a hidden `.x.yaml`, for `.yaml`
	// itself and for a directory named `x.yaml`; false for the root ".".
	// Names mode answers it too, so a caller that holds a hidden name can
	// ask whether it looks like config without composing the rule (#3206).
	ConfigExtension bool
	// Readers is who reads it; [] when no reader does (then NotRead says
	// why — except for role dir and unlistable_dir: a directory is read by
	// no one and nothing is passed over, so NotRead stays empty). null in
	// names mode.
	Readers []string
	// NotRead is why no reader reads it (notRead*), else null.
	NotRead string
	// Role is config.Role's verdict on the path — the one place it is made.
	// null in names mode (no disk to judge).
	Role config.Role
	// Problem is why a read of the entry fails, else null: a parsetree.Kind
	// and a message for a person. List mode gives only what is decided
	// without reading (read_failure, not_regular_file); tree and file mode
	// add the content kinds (syntax_error, …) for a config file they read.
	Problem *parseTreeProblem
	// Verdict is what the path makes da-guard's validation (no --scope) do
	// (verdict.go, a closed set): "unservable" — validation names it under
	// exit 3 (parse_failed or unreadable: the exporter cannot serve it) —,
	// "tree_refused" — under exit 4 as unreadable_outside_run (the route
	// generator refuses the whole tree on it) —, else null. Tree mode: of the
	// tree on disk, every entry. File mode: the full verdict of --path once
	// its text is written (setFileVerdict; no load of the tree). List mode
	// reads no byte, so it knows only the walk's half of exit 3 (unreadable):
	// a file whose content the exporter refuses (parse_failed) is null there
	// and unservable in tree mode. Names mode: always null (no tree).
	// ⚠️ Per path only: null on every entry is not "validation passes" — a
	// tenant declared twice (exit 2) or routing evidence (exit 4) is no
	// path's verdict. An entry can be unselected_defaults_carrier AND
	// unservable: a broken carrier below the root that its directory does not
	// select is still probed, and dropped, by the exporter's build.
	Verdict string
}

type parseTreeProblem struct {
	Kind    parsetree.Kind `json:"kind"`
	Message string         `json:"message"`
}

// MarshalJSON writes every field, an empty TenantNamed / NotRead / Role /
// Verdict as null, and does not escape HTML characters (a path is data, not markup).
func (e parseTreeEntry) MarshalJSON() ([]byte, error) {
	orNull := func(s string) *string {
		if s == "" {
			return nil
		}
		return &s
	}
	var role *config.Role
	if e.Role != "" {
		r := e.Role
		role = &r
	}
	w := struct {
		Path             string            `json:"path"`
		Depth            int               `json:"depth"`
		Class            string            `json:"class"`
		TenantNamed      *string           `json:"tenant_named"`
		DefaultsDocument bool              `json:"defaults_document"`
		ConfigExtension  bool              `json:"config_extension"`
		Readers          []string          `json:"readers"`
		NotRead          *string           `json:"not_read"`
		Role             *config.Role      `json:"role"`
		Problem          *parseTreeProblem `json:"problem"`
		Verdict          *string           `json:"verdict"`
	}{e.Path, e.Depth, e.Class, orNull(e.TenantNamed), e.DefaultsDocument, e.ConfigExtension, e.Readers,
		orNull(e.NotRead), role, e.Problem, orNull(e.Verdict)}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(w); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n")), nil
}

type parseTreeFlags struct {
	configDir string
	path      string
	input     string
	listOnly  bool
	names     string
}

func parseParseTreeFlags(args []string, errOut io.Writer) (*parseTreeFlags, error) {
	fs := flag.NewFlagSet(programName+" "+parseTreeCmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	f := &parseTreeFlags{}
	fs.StringVar(&f.configDir, "config-dir", "",
		"Path to the conf.d/ root. Required but with --names.")
	fs.StringVar(&f.path, "path", "",
		"File mode: the path, relative to --config-dir, the text of --input is meant for.\n"+
			"Its readers are judged in the tree as it is once that file is written.")
	fs.StringVar(&f.input, "input", "",
		"File mode: the file whose text to read ('-' = stdin). Required with --path.")
	fs.BoolVar(&f.listOnly, "list-only", false,
		"List mode: the same document without \"files\" and \"rendered_by\", reading no\n"+
			"file's content. With --config-dir, or with --names.")
	fs.StringVar(&f.names, "names", "",
		"Names mode (with --list-only, without --config-dir): classify the base names in\n"+
			"this file ('-' = stdin), a JSON array of strings, by name alone. Known limit: a\n"+
			"lone UTF-16 surrogate escape in a string (\"\\ud800\") is read as U+FFFD, as\n"+
			"encoding/json reads it, so the entry answers for that other name (exit 0); a\n"+
			"caller that may hold such a name refuses it before asking (no ConfigMap key or\n"+
			"file name can hold one).")
	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s %s --config-dir <dir>\n", programName, parseTreeCmd)
		fmt.Fprintf(errOut, "       %s %s --config-dir <dir> --path <rel> --input <file|->\n", programName, parseTreeCmd)
		fmt.Fprintf(errOut, "       %s %s --list-only --config-dir <dir>\n", programName, parseTreeCmd)
		fmt.Fprintf(errOut, "       %s %s --list-only --names <file|->\n", programName, parseTreeCmd)
		fmt.Fprintf(errOut, "Print, as JSON, every conf.d path the exporter's walk records — what it is, who\n"+
			"reads it, what stops a read — and what each readable file holds, as yaml.v3\n"+
			"decodes it (ADR-036 §2). The document's \"schema\" is "+parseTreeSchema+" in every\n"+
			"mode; a reader refuses any other value.\n\n"+
			"Document (one line on stdout):\n"+
			"  {\"schema\", \"da_guard_version\", \"mode\": \"tree\"|\"file\"|\"list\"|\"names\",\n"+
			"   \"defaults_carriers\": {<dir>: <path>},  the one _defaults file each directory's chain reads\n"+
			"   \"entries\": [<entry>],                  one per path (below)\n"+
			"   \"files\": {<path>: <content>},          tree and file mode only\n"+
			"   \"rendered_by\": {<path>: {<key>: [\"exporter\"|\"generator\"]}}}  tree and file mode only\n"+
			"<entry> holds every field, null where it has no value:\n"+
			"  {\"path\", \"depth\", \"class\", \"tenant_named\", \"defaults_document\",\n"+
			"   \"config_extension\", \"readers\",\n"+
			"   \"not_read\", \"role\", \"problem\": {\"kind\", \"message\"}, \"verdict\"}\n"+
			"Paths are relative to --config-dir, with '/'; <dir> is \".\" for the root.\n"+
			"\"depth\": directories between the root and the path. \"class\": tenant, defaults,\n"+
			"platform (another `_` name) or directory (file and names mode also hidden,\n"+
			"not_config_name). \"tenant_named\" (class tenant only): the id the name carries (the\n"+
			"name minus .yaml / .yml, case kept) — not who is served: the exporter takes tenant\n"+
			"ids from the file's `tenants:` keys. \"defaults_document\": the name starts with\n"+
			"`_defaults` (any case) — a platform-defaults document, held to the platform-defaults\n"+
			"schema; wider than class defaults (`_defaults-multidb.yaml` is platform with this\n"+
			"true); false whenever \"role\" is not config.\n"+
			"\"config_extension\": the base name ends in .yaml / .yml (any case), whatever the\n"+
			"path is — true for a hidden .x.yaml and a directory named x.yaml; false for \".\".\n"+
			"\"readers\": the exporter reads every tenant file, the root's platform (`_`) files and\n"+
			"each directory's selected defaults carrier; the route generator reads those and every\n"+
			"other `_` file below the root (e.g. team/_domain_policy.yaml); [] with \"not_read\" set\n"+
			"when no reader does: unselected_defaults_carrier (at the root or below it; names mode\n"+
			"too, readers null: the names are one directory's, selected as the walk selects), and in\n"+
			"file mode also hidden, not_config_name, parent_not_walked. A role dir or\n"+
			"unlistable_dir entry has readers [] and not_read null: a directory is no file\n"+
			"anyone reads.\n"+
			"\"role\", the verdict on what the path is, a closed list:\n"+
			"  config          a config file. Readers read it; a \"problem\" says their read fails\n"+
			"                  (a stat or open refused for any reason but the ones below, EIO, a\n"+
			"                  content kind in tree and file mode).\n"+
			"  dir             a directory the walk goes into and lists — every one has an entry;\n"+
			"                  a config-named one (x.yaml/) too, with a not_regular_file problem.\n"+
			"                  File mode gives an existing directory --path the same entry.\n"+
			"  not_a_file      the exporter's walk cannot reach a regular file there and does not\n"+
			"                  go into it: a symlink to a directory, a FIFO / socket / device (or a\n"+
			"                  link to one), a stat that finds nothing (a dangling link, a looping\n"+
			"                  link, a chain of more than 40 links even when it ends at a file).\n"+
			"  unlistable_dir  a directory whose contents could not be listed (\".\" for the root):\n"+
			"                  nothing under it was seen, so the document is incomplete.\n"+
			"Only those make a path not_a_file; anything else that fails (permission denied, EIO,\n"+
			"a name too long, …) is config.\n"+
			"\"problem\": a kind from the closed list below; list mode decides only read_failure and\n"+
			"not_regular_file (files are opened and closed, never read), so a file that opens but\n"+
			"fails mid-read has no problem there.\n"+
			"\"verdict\": what the path makes `da-guard --config-dir <dir>` (no --scope) do, a\n"+
			"closed list, null when nothing. Always the whole tree's judgement: a superset of what a\n"+
			"--scope'd run lists under parse_failed / unreadable — compare it with unscoped validation.\n"+
			"  unservable      named under exit 3 (parse_failed or unreadable): the exporter cannot\n"+
			"                  serve it.\n"+
			"  tree_refused    named under exit 4 as unreadable_outside_run: the route generator\n"+
			"                  refuses the whole tree on it.\n"+
			"Tree mode: every entry, of the tree on disk. File mode: the full verdict of --path\n"+
			"once its text is written (null when the walk would not record it: hidden,\n"+
			"not_config_name, parent_not_walked); file mode judges no other path — read tree mode\n"+
			"for the rest. List mode reads no byte and so knows only the walk's half of exit 3: a\n"+
			"file the exporter cannot decode is null there and unservable in tree mode — a reader\n"+
			"that must name every path validation names reads tree mode. Names mode: always null.\n"+
			"An unselected_defaults_carrier below the root can be unservable (the exporter still\n"+
			"probes it). Per path only: null everywhere is not \"validation passes\" — a tenant\n"+
			"declared twice (exit 2) or routing content (exit 4) is no path's verdict; a reader that\n"+
			"needs the whole verdict runs validation.\n\n"+
			"\"files\" holds every config entry with readers and no problem (tree mode), or the\n"+
			"text of --path (file mode). <content> is the top-level mapping, aliases and unquoted\n"+
			"`<<` merges expanded. A mapping is a JSON object keyed by each key's source text\n"+
			"(`010:` is \"010\"); a sequence is an array; a scalar is {\"text\", \"tag\", \"value\"}: its\n"+
			"source text, its yaml.v3 tag (!!str !!int !!float !!bool !!null !!timestamp), and its\n"+
			"value — true/false/null for !!bool/!!null, the decimal text for !!int/!!float\n"+
			"(\"0777\" is \"511\"), the string for !!str, absent for !!timestamp and for .inf/.nan. A\n"+
			"scalar is the only object whose \"text\" is a JSON string. An empty file, or one\n"+
			"holding only null, is {}. One file's content may encode to at most %d bytes of\n"+
			"JSON (aliases expanded); past that the file is a decode_error.\n"+
			"A file nested close to yaml.v3's limit (10,000 levels) nests the JSON as deep, which a\n"+
			"JSON decoder may refuse (measured: Python 3.13's json raises RecursionError at 10,000\n"+
			"nested arrays): a reader that cannot decode the document must treat the whole\n"+
			"document as unusable, never a part of it as read.\n"+
			"\"rendered_by\": for each readable `_` file, its platform carriers (defaults,\n"+
			"state_filters, optional_overrides, profiles, tenants, _routing_defaults,\n"+
			"_routing_enforced, routing_profiles) and the renders a change to one there can\n"+
			"alter; [] = read by nothing there; other keys are absent.\n\n"+
			"Problem kinds, a closed list (anything else is read as yaml.v3 reads it, even where\n"+
			"today's route generator refuses the file): %s.\n\n"+
			"File mode reads --input as the text of --path: one entry, --path with its readers,\n"+
			"and \"files\" holds the text or the entry a content problem. --path is also judged on\n"+
			"disk: a directory on it that exists but is not a real directory (a symlink, a file) is\n"+
			"parent_not_walked; an existing --path that is not a regular file (or a link to one)\n"+
			"gets the entry tree mode gives the same path, and the text is not read.\n"+
			"List mode (--list-only --config-dir): the walk of tree mode and the same entries\n"+
			"(the same function builds both), but for the problems only reading finds.\n"+
			"Names mode (--list-only --names): each given base name in the order given, \"depth\" 0,\n"+
			"\"readers\" and \"role\" null — for names that are not on a disk (e.g. the keys of a\n"+
			"ConfigMap built from a conf.d).\n\n",
			parsetree.MaxContentBytes, kindList())
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  ok — an unreadable file is data in the document, not a verdict\n"+
			"  2  caller error: bad flags, --config-dir missing or not a directory, a --path that is\n"+
			"     absolute or leaves --config-dir, --input or --names not readable, --names not a\n"+
			"     JSON array of base names or holding one twice, a file name that is not valid UTF-8\n"+
			"     (JSON cannot carry it), or stdout not writable\n")
	}
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(errOut, "%s %s: unexpected argument %q\n", programName, parseTreeCmd, fs.Arg(0))
		return nil, errors.New("unexpected argument")
	}
	return f, nil
}

func kindList() string {
	kinds := parsetree.Kinds()
	names := make([]string, len(kinds))
	for i, k := range kinds {
		names[i] = string(k)
	}
	return strings.Join(names, ", ")
}

// runParseTree is the subcommand's entry point. stdin is read only for
// `--input -` and `--names -`.
func runParseTree(args []string, stdin io.Reader, stdout, errOut io.Writer) int {
	f, err := parseParseTreeFlags(args, errOut)
	if errors.Is(err, flag.ErrHelp) {
		return exitOK
	}
	if err != nil {
		return exitCallerErr
	}
	fail := func(format string, a ...any) int {
		fmt.Fprintf(errOut, "%s %s: "+format+"\n", append([]any{programName, parseTreeCmd}, a...)...)
		return exitCallerErr
	}
	if f.listOnly {
		switch {
		case f.path != "" || f.input != "":
			return fail("--list-only reads no file: --path and --input do not go with it")
		case f.names != "" && f.configDir != "":
			return fail("--names classifies names, not a tree: give --config-dir or --names, not both")
		case f.names == "" && f.configDir == "":
			return fail("--list-only needs --config-dir or --names")
		}
		return runParseTreeList(f, stdin, stdout, fail)
	}
	if f.names != "" {
		return fail("--names goes with --list-only")
	}
	if f.configDir == "" {
		return fail("--config-dir is required")
	}
	if (f.path == "") != (f.input == "") {
		return fail("--path and --input go together (file mode), or neither is given")
	}
	var input []byte
	var rel string
	if f.path != "" {
		if rel, err = cleanRelPath(f.path); err != nil {
			return fail("--path %q: %v", f.path, err)
		}
		if f.input == "-" {
			input, err = io.ReadAll(stdin)
		} else {
			input, err = os.ReadFile(f.input) // #nosec G304 -- the caller names the file to read
		}
		if err != nil {
			return fail("--input: %v", err)
		}
	}

	// ScanDirTreeFiles, not ScanDirTree: the exporter's walk without its
	// tenant decode (yaml.v3 panics on some files parse-tree must name as
	// unreadable, e.g. a collection key beside a merge). Like every mode of
	// the walk it never opens a special file (#2939).
	scan, err := config.ScanDirTreeFiles(f.configDir, log.New(io.Discard, "", 0))
	if err != nil {
		return fail("%v", err)
	}
	if p := nonUTF8Path(scan); p != "" {
		return fail("%v", nonUTF8Error(p))
	}
	var doc *parseTreeDoc
	if f.path == "" {
		if doc, err = parseTreeOf(scan); err == nil {
			err = setVerdicts(doc.Entries, treeDropped(scan))
		}
	} else {
		var asOnDisk, selected bool
		if doc, asOnDisk, selected, err = parseFileIn(scan, rel, input); err == nil {
			setFileVerdict(scan, &doc.Entries[0], asOnDisk, selected, rel, input)
		}
	}
	if err != nil {
		return fail("%v", err)
	}
	if err := writeParseTree(stdout, doc.Entries, doc); err != nil {
		return fail("%v", err)
	}
	return exitOK
}

func newParseTreeIndex(mode string) parseTreeIndex {
	return parseTreeIndex{
		Schema: parseTreeSchema, DaGuardVersion: Version, Mode: mode,
		DefaultsCarriers: map[string]string{}, Entries: []parseTreeEntry{},
	}
}

func newParseTreeDoc(mode string) *parseTreeDoc {
	return &parseTreeDoc{parseTreeIndex: newParseTreeIndex(mode),
		Files: map[string]parsetree.Content{}, RenderedBy: map[string]map[string][]string{}}
}

// parseTreeOf is tree mode over one walk: walkEntries' index, then the
// content of every config file a reader reads.
func parseTreeOf(scan *config.TreeScan) (*parseTreeDoc, error) {
	doc := newParseTreeDoc("tree")
	if err := walkEntries(scan, &doc.parseTreeIndex); err != nil {
		return nil, err
	}
	for i := range doc.Entries {
		e := &doc.Entries[i]
		f, ok := scan.Files[e.Path]
		if !ok || len(e.Readers) == 0 {
			// Not a file the walk read, or one no reader reads (an
			// unselected carrier is ignored whole: not parsed, #1674).
			continue
		}
		content, bad := parsetree.Parse(f.Data)
		if bad != nil {
			e.Problem = &parseTreeProblem{Kind: bad.Kind, Message: bad.Message}
			continue
		}
		doc.Files[e.Path] = content
		// A defaults carrier with readers is its directory's selected one.
		if rb := renderedBy(e.Path, confdname.IsDefaults(path.Base(e.Path)), content); rb != nil {
			doc.RenderedBy[e.Path] = rb
		}
	}
	return doc, nil
}

// parseFileIn is file mode: input read as the text of rel, in the tree scan
// holds once rel is written. asOnDisk: the one entry is tree mode's entry of
// rel in the tree on disk (a walk record that is no config file, or a path
// that is no regular file), whose text is not read. selectedCarrier: false
// only for a defaults carrier its directory's chain would not select.
func parseFileIn(scan *config.TreeScan, rel string, input []byte) (doc *parseTreeDoc, asOnDisk, selectedCarrier bool, err error) {
	doc = newParseTreeDoc("file")
	// A path the walk recorded as no config file (a directory it entered or
	// could not list, a symlink to a directory, a path that is no regular
	// file — whatever its name: `etc/`, `etc -> /etc`) is what tree mode says
	// it is — the very entry, from the same walk and the same function
	// (walkEntries), never a second verdict here. Its text is not read.
	if d := walkedDirEntry(scan, rel); d != nil {
		doc.Entries = append(doc.Entries, *d)
		return doc, true, false, nil
	}
	e := nameEntry(rel)
	e.Role = config.RoleConfig
	reason := ""
	switch {
	case confdname.HasHiddenSegment(rel):
		reason = notReadHidden
	case !confdname.IsScannedPath(rel):
		reason = notReadNotConfigName
	default:
		reason = parentsOnDisk(scan.AbsRoot, rel)
	}
	if reason == "" {
		if issue := fileOnDisk(scan.AbsRoot, rel); issue != nil {
			// What tree mode gives the same path: the same entry, and the
			// text is not read.
			doc.Entries = append(doc.Entries, *issue)
			return doc, true, false, nil
		}
	}
	readers := []string{}
	carrier := false
	selected := true
	if reason == "" {
		if confdname.IsDefaults(path.Base(rel)) {
			// The directory's carriers once rel is written: the readable
			// ones the walk found there, rel among them.
			abs := filepath.Join(scan.AbsRoot, filepath.FromSlash(rel))
			dir := filepath.Dir(abs)
			set := map[string]bool{abs: true}
			for p := range scan.Defaults {
				if filepath.Dir(p) == dir {
					set[p] = true
				}
			}
			chosen := config.SelectDefaultsCarriers(set).ByDir[dir]
			selected = chosen == abs
			carrier = selected
			d, err := relTo(scan.AbsRoot, dir)
			if err != nil {
				return nil, false, false, err
			}
			c, err := relTo(scan.AbsRoot, chosen)
			if err != nil {
				return nil, false, false, err
			}
			doc.DefaultsCarriers[d] = c
		}
		if readers = readersFor(rel, selected); len(readers) == 0 {
			readers, reason = []string{}, notReadUnselectedCarrier
		}
	}
	e.Readers, e.NotRead = readers, reason
	content, bad := parsetree.Parse(input)
	if bad != nil {
		e.Problem = &parseTreeProblem{Kind: bad.Kind, Message: bad.Message}
	} else {
		doc.Files[rel] = content
		if rb := renderedBy(rel, carrier, content); rb != nil && reason == "" {
			doc.RenderedBy[rel] = rb
		}
	}
	doc.Entries = append(doc.Entries, e)
	return doc, false, selected, nil
}

// notFileEntry is the readers and the message of a path that is not a file
// (a config.NotFile reason), the same in every mode.
func notFileEntry(rel, reason string) ([]string, string) {
	switch reason {
	case config.NotFileDirLink:
		return []string{readerGenerator}, "a symlink to a directory the walk does not follow"
	}
	// A config-named directory: the walk descends it like any directory
	// (role dir) and its files are read — the directory itself is read by
	// no reader (readers []); the problem says only that its name looks like
	// a file's.
	return []string{},
		"a directory with a config file name (the walk descends it; its files are read)"
}

// fileOnDisk is the tree-mode entry of an existing rel that is not a regular
// file (or a symlink to one); nil when rel is missing or a regular file.
// Each case builds the walk's own record (config.UnreadableFile /
// config.NotFile) and asks it for its role, so the verdict is the walk's.
func fileOnDisk(root, rel string) *parseTreeEntry {
	abs := filepath.Join(root, filepath.FromSlash(rel))
	fi, err := os.Lstat(abs)
	switch {
	case errors.Is(err, os.ErrNotExist):
		return nil
	case err != nil:
		return unreadableEntry(config.UnreadableFile{RelKey: rel, Reason: config.UnreadableStatError,
			Missing: config.StatErrIsWrongPath(err)})
	case fi.Mode()&os.ModeSymlink != 0:
		ti, terr := os.Stat(abs)
		switch {
		case terr != nil:
			return unreadableEntry(config.UnreadableFile{RelKey: rel, Reason: config.UnreadableStatError,
				Missing: config.StatErrIsWrongPath(terr)})
		case ti.IsDir():
			e := notFileAsEntry(config.NotFile{RelKey: rel, Reason: config.NotFileDirLink})
			return &e
		case config.IsSpecialFile(ti.Mode()):
			return unreadableEntry(config.UnreadableFile{RelKey: rel, Reason: config.UnreadableSpecialFile})
		}
	case fi.IsDir():
		e := notFileAsEntry(config.NotFile{RelKey: rel, Reason: config.NotFileDirectory})
		return &e
	case config.IsSpecialFile(fi.Mode()):
		return unreadableEntry(config.UnreadableFile{RelKey: rel, Reason: config.UnreadableSpecialFile})
	}
	return nil
}

// walkedDirEntry is tree mode's entry of rel when the walk recorded rel as
// anything but a config file (role dir, unlistable_dir or not_a_file), else
// nil: the walk record itself (TreeScan.Dirs, NotFiles, Unreadable), turned
// into an entry by the functions walkEntries uses for it, so file and tree
// mode cannot disagree about such a path. Looked up directly — not a whole
// walkEntries index built and sorted for one path on every file-mode call.
// rel is never "." (cleanRelPath refuses it), so the root's own record
// (RootWalkErr) is not asked.
func walkedDirEntry(scan *config.TreeScan, rel string) *parseTreeEntry {
	for _, d := range scan.Dirs {
		if d == rel {
			e := dirAsEntry(d)
			return &e
		}
	}
	for _, nf := range scan.NotFiles {
		if nf.RelKey == rel {
			e := notFileAsEntry(nf)
			return &e
		}
	}
	for _, u := range scan.Unreadable {
		if u.RelKey == rel && u.Role() != config.RoleConfig {
			return unreadableEntry(u)
		}
	}
	return nil
}

func unreadableEntry(u config.UnreadableFile) *parseTreeEntry {
	e := unreadableAsEntry(u)
	return &e
}

// parentsOnDisk is notReadParentNotWalked when a directory on the way to rel
// exists under root but is not a real directory the walk descends (a
// symlink — the walk follows none to a directory —, a regular file, a
// dangling link), else "". A missing directory is fine: the writer creates
// it, and the walk then descends it. Tree mode never meets such a path.
func parentsOnDisk(root, rel string) string {
	parts := strings.Split(rel, "/")
	cur := root
	for _, part := range parts[:len(parts)-1] {
		cur = filepath.Join(cur, part)
		fi, err := os.Lstat(cur)
		if errors.Is(err, os.ErrNotExist) {
			return ""
		}
		if err != nil || !fi.IsDir() {
			return notReadParentNotWalked
		}
	}
	return ""
}

// readersFor is who reads the config file at rel (a root-relative slash
// path the walk keeps), given whether it is the carrier its directory's
// chain selects (true for any file that is not a defaults carrier).
//
// ⛔ The generator's half is `_grar_parse._parse_config_files`: it reads
// every file the walk lists except a carrier its directory does not select,
// which it names as ignored (an `unselected_defaults_carrier` finding) and
// does not open, whatever it holds — so it is "not_read" here. The exporter
// reads tenant files, the root's `_` files but an unselected carrier
// (isRootPlatformKey), and each directory's selected carrier; another `_`
// file below the root it only probes for syntax (isNestedPlatformFile).
func readersFor(rel string, selectedCarrier bool) []string {
	nested := strings.Contains(rel, "/")
	base := path.Base(rel)
	switch {
	case !selectedCarrier:
		return nil
	case nested && confdname.IsReserved(base) && !confdname.IsDefaults(base):
		return []string{readerGenerator}
	}
	return []string{readerExporter, readerGenerator}
}

func unreadableMessage(reason string) string {
	switch reason {
	case config.UnreadableStatError:
		return "the path cannot be statted (stat_error)"
	case config.UnreadableReadError:
		return "the file cannot be read (read_error)"
	case config.UnreadableWalkError:
		return "the directory cannot be listed; nothing under it is read (walk_error)"
	case config.UnreadableSpecialFile:
		return "not a regular file (a FIFO, a socket or a device, or a symlink to one); not read"
	}
	return reason
}

// unreadableKind is the parse-tree kind of a config.UnreadableFile reason: a
// special file is not_regular_file, every other reason read_failure.
func unreadableKind(reason string) parsetree.Kind {
	if reason == config.UnreadableSpecialFile {
		return parsetree.KindNotRegularFile
	}
	return parsetree.KindReadFailure
}

// cleanRelPath accepts a path relative to --config-dir that stays inside it
// and returns it with '/' separators.
func cleanRelPath(p string) (string, error) {
	s := filepath.ToSlash(p)
	if s == "" || path.IsAbs(s) || filepath.IsAbs(p) || filepath.VolumeName(p) != "" {
		return "", errors.New("must be a path relative to --config-dir")
	}
	clean := path.Clean(s)
	if clean == "." || clean == ".." || strings.HasPrefix(clean, "../") {
		return "", errors.New("must name a file inside --config-dir")
	}
	if !utf8.ValidString(clean) {
		return "", errors.New("is not valid UTF-8; JSON cannot carry it")
	}
	return clean, nil
}

func relTo(root, p string) (string, error) {
	r, err := filepath.Rel(root, p)
	if err != nil {
		return "", err
	}
	r = filepath.ToSlash(r)
	if !utf8.ValidString(r) {
		return "", nonUTF8Error(r)
	}
	return r, nil
}

// writeParseTree refuses an entry path that is not valid UTF-8 (JSON would
// write U+FFFD for its bad bytes, so two files could come out as one) and a
// path held by two entries, then writes doc on one line. Every path in "files" and "rendered_by" is an
// entry's; file contents are valid UTF-8 by construction (parsetree refuses
// anything else).
func writeParseTree(w io.Writer, entries []parseTreeEntry, doc any) error {
	seen := make(map[string]bool, len(entries))
	for _, e := range entries {
		if !utf8.ValidString(e.Path) {
			return nonUTF8Error(e.Path)
		}
		// One entry per path is the document's contract in every mode; the
		// builders keep it (walkEntries, parseTreeNamesOf), and this is the
		// last check before any byte is written, so a builder that breaks
		// it fails here, naming the path, never in a reader.
		if seen[e.Path] {
			return fmt.Errorf("the index holds %q twice; a document holds one entry "+
				"per path (a da-guard defect, not the tree's)", e.Path)
		}
		seen[e.Path] = true
	}
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	return enc.Encode(doc)
}
