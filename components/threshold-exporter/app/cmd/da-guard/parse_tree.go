package main

// parse-tree: the conf.d "parse result" of ADR-036 §2, as JSON (#2897) —
// which files each reader reads, every readable file's content as yaml.v3
// decodes it, and the files that cannot be read, each with a fixed kind.
// No reader consumes it yet; ADR-036's switch moves the Python side onto it.
//
// ⛔ NO RULES OF ITS OWN. Which files exist is the exporter's walker
// (config.ScanDirTree: hidden names, extensions in any case, directories
// below the root, the one defaults carrier per directory); which of them
// the route generator reads besides is stated once, in readersFor, from
// `_grar_parse._parse_config_files`; what a file holds is internal/parsetree.

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/vencil/threshold-exporter/internal/confdname"
	"github.com/vencil/threshold-exporter/internal/parsetree"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// parseTreeCmd is the subcommand name: `da-guard parse-tree ...`.
const parseTreeCmd = "parse-tree"

// parseTreeSchema names the document's shape. A reader refuses any other
// value (and a document without one), as for served-values.
const parseTreeSchema = "da-guard.parse-tree/v1"

// The readers a file can have, in the order a "selected" entry lists them.
const (
	readerExporter  = "exporter"
	readerGenerator = "generator"
)

// Closed set of parseTreeNotRead.Reason values.
const (
	// notReadUnselectedCarrier: a defaults carrier (`_defaults.yaml` /
	// `.yml`, any case) that its directory's chain does not select — at the
	// root or below it (#1674: every plane ignores it whole).
	notReadUnselectedCarrier = "unselected_defaults_carrier"
	// notReadHidden: the path, or a directory on it, starts with '.'.
	notReadHidden = "hidden"
	// notReadNotConfigName: the name does not end in .yaml / .yml (any case).
	notReadNotConfigName = "not_config_name"
)

// parseTreeDoc is the JSON document on stdout.
type parseTreeDoc struct {
	Schema         string `json:"schema"`
	DaGuardVersion string `json:"da_guard_version"`
	// Mode is "tree" (--config-dir alone) or "file" (--path / --input).
	Mode string `json:"mode"`
	// Selected: every path some reader reads (root-relative, slash), with
	// its readers. Its keys are exactly the keys of Files plus the files of
	// Unreadable. In file mode it holds the one --path, whose readers may
	// be empty (then NotRead says why).
	Selected map[string][]string `json:"selected"`
	// DefaultsCarriers: directory ("." for the root) → the one defaults
	// carrier its chain reads. In file mode, only --path's directory, as it
	// is once --path is written.
	DefaultsCarriers map[string]string `json:"defaults_carriers"`
	// NotRead: files the walk found that no reader reads (tree mode), or
	// --path when no reader would read it (file mode).
	NotRead []parseTreeNotRead `json:"not_read"`
	// Files: each readable file's content (parsetree.Content).
	Files map[string]parsetree.Content `json:"files"`
	// Unreadable: each file no reader can read, with a parsetree.Kind.
	Unreadable []parseTreeUnreadable `json:"unreadable"`
}

type parseTreeNotRead struct {
	File   string `json:"file"`
	Reason string `json:"reason"`
}

type parseTreeUnreadable struct {
	File    string         `json:"file"`
	Kind    parsetree.Kind `json:"kind"`
	Message string         `json:"message"`
}

type parseTreeFlags struct {
	configDir string
	path      string
	input     string
}

func parseParseTreeFlags(args []string, errOut io.Writer) (*parseTreeFlags, error) {
	fs := flag.NewFlagSet(programName+" "+parseTreeCmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	f := &parseTreeFlags{}
	fs.StringVar(&f.configDir, "config-dir", "",
		"Path to the conf.d/ root. Required.")
	fs.StringVar(&f.path, "path", "",
		"File mode: the path, relative to --config-dir, the text of --input is meant for.\n"+
			"Its readers are judged in the tree as it is once that file is written.")
	fs.StringVar(&f.input, "input", "",
		"File mode: the file whose text to read ('-' = stdin). Required with --path.")
	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s %s --config-dir <dir>\n", programName, parseTreeCmd)
		fmt.Fprintf(errOut, "       %s %s --config-dir <dir> --path <rel> --input <file|->\n", programName, parseTreeCmd)
		fmt.Fprintf(errOut, "Print, as JSON, which conf.d files each reader reads and what each one holds, as\n"+
			"yaml.v3 decodes it (ADR-036 §2). The document's \"schema\" is "+parseTreeSchema+";\n"+
			"a reader refuses any other value.\n\n"+
			"Document (one line on stdout):\n"+
			"  {\"schema\", \"da_guard_version\", \"mode\": \"tree\"|\"file\",\n"+
			"   \"selected\": {<path>: [\"exporter\", \"generator\"]},  every file a reader reads, and who\n"+
			"   \"defaults_carriers\": {<dir>: <path>},  the one _defaults file each directory's chain reads\n"+
			"   \"not_read\": [{\"file\", \"reason\"}],  files no reader reads (unselected_defaults_carrier,\n"+
			"                                       at the root or below it;\n"+
			"                                       in file mode also hidden, not_config_name)\n"+
			"   \"files\": {<path>: <content>},  every readable selected file\n"+
			"   \"unreadable\": [{\"file\", \"kind\", \"message\"}]}  every selected file that cannot be read\n"+
			"Paths are relative to --config-dir, with '/'; <dir> is \".\" for the root. The keys of\n"+
			"\"selected\" are the keys of \"files\" plus the files of \"unreadable\".\n"+
			"Readers: the exporter reads every tenant file, the root's platform (`_`) files and each\n"+
			"directory's selected defaults carrier; the route generator reads those and every other\n"+
			"`_` file below the root (e.g. team/_domain_policy.yaml, team/_routing_profiles.yaml).\n\n"+
			"<content> is the top-level mapping, aliases and unquoted `<<` merges expanded. A mapping\n"+
			"is a JSON object keyed by each key's source text (`010:` is \"010\"); a sequence is an\n"+
			"array; a scalar is {\"text\", \"tag\", \"value\"}: its source text, its yaml.v3 tag\n"+
			"(!!str !!int !!float !!bool !!null !!timestamp), and its value — true/false/null for\n"+
			"!!bool/!!null, the decimal text for !!int/!!float (\"0777\" is \"511\"), the string for\n"+
			"!!str, absent for !!timestamp and for .inf/.nan. A scalar is the only object whose\n"+
			"\"text\" is a JSON string. An empty file, or one holding only null, is {}.\n\n"+
			"Unreadable kinds, a closed list (anything else is read as yaml.v3 reads it, even where\n"+
			"today's route generator refuses the file): %s.\n\n"+
			"File mode reads --input as the text of --path: \"selected\" holds --path with its readers\n"+
			"(an empty list, and an entry in \"not_read\", when no reader would read it), and \"files\"\n"+
			"or \"unreadable\" what the text holds.\n\n", kindList())
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  ok — an unreadable file is data in the document, not a verdict\n"+
			"  2  caller error: bad flags, --config-dir missing or not a directory, a --path that is\n"+
			"     absolute or leaves --config-dir, --input not readable, a file name that is not\n"+
			"     valid UTF-8 (JSON cannot carry it), or stdout not writable\n")
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
	names := make([]string, len(parsetree.Kinds))
	for i, k := range parsetree.Kinds {
		names[i] = string(k)
	}
	return strings.Join(names, ", ")
}

// runParseTree is the subcommand's entry point. stdin is read only for
// `--input -`.
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
	// unreadable, e.g. a collection key beside a merge) and without reading
	// a FIFO, which would block forever.
	scan, err := config.ScanDirTreeFiles(f.configDir, log.New(io.Discard, "", 0))
	if err != nil {
		return fail("%v", err)
	}
	var doc *parseTreeDoc
	if f.path == "" {
		doc, err = parseTreeOf(scan)
	} else {
		doc, err = parseFileIn(scan, rel, input)
	}
	if err != nil {
		return fail("%v", err)
	}
	if err := writeParseTree(stdout, doc); err != nil {
		return fail("%v", err)
	}
	return exitOK
}

func newParseTreeDoc(mode string) *parseTreeDoc {
	return &parseTreeDoc{
		Schema: parseTreeSchema, DaGuardVersion: Version, Mode: mode,
		Selected: map[string][]string{}, DefaultsCarriers: map[string]string{},
		NotRead: []parseTreeNotRead{}, Files: map[string]parsetree.Content{},
		Unreadable: []parseTreeUnreadable{},
	}
}

// parseTreeOf is tree mode over one walk.
func parseTreeOf(scan *config.TreeScan) (*parseTreeDoc, error) {
	doc := newParseTreeDoc("tree")
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
	unreadable := func(rel string, readers []string, kind parsetree.Kind, msg string) {
		doc.Selected[rel] = readers
		doc.Unreadable = append(doc.Unreadable, parseTreeUnreadable{File: rel, Kind: kind, Message: msg})
	}
	for _, rel := range scan.Keys {
		f := scan.Files[rel]
		readers := readersFor(rel, !confdname.IsDefaults(path.Base(rel)) || selected[f.AbsPath])
		if len(readers) == 0 {
			doc.NotRead = append(doc.NotRead, parseTreeNotRead{File: rel, Reason: notReadUnselectedCarrier})
			continue
		}
		content, bad := parsetree.Parse(f.Data)
		if bad != nil {
			unreadable(rel, readers, bad.Kind, bad.Message)
			continue
		}
		doc.Selected[rel] = readers
		doc.Files[rel] = content
	}
	for _, u := range scan.Unreadable {
		readers := []string{readerExporter, readerGenerator}
		if u.Reason != config.UnreadableWalkError {
			// Never an unselected carrier: selection runs over the files
			// the walk could read (the generator's readable_carriers too).
			readers = readersFor(u.RelKey, true)
		}
		unreadable(u.RelKey, readers, parsetree.KindReadFailure, unreadableMessage(u.Reason))
	}
	if scan.RootWalkErr != nil {
		unreadable(".", []string{readerExporter, readerGenerator}, parsetree.KindReadFailure, unreadableMessage(config.UnreadableWalkError))
	}
	for _, nf := range scan.NotFiles {
		readers, msg := []string{readerGenerator}, "a directory with a config file name"
		switch nf.Reason {
		case config.NotFileDirLink:
			msg = "a symlink to a directory the walk does not follow"
		case config.NotFileSpecial:
			// The exporter's walk reads it (and blocks on a FIFO); the
			// generator books it as unusable (`Path.is_file()`).
			readers = readersFor(nf.RelKey, true)
			msg = "not a regular file (a FIFO, a socket or a device, or a symlink to one); not read"
		}
		unreadable(nf.RelKey, readers, parsetree.KindNotRegularFile, msg)
	}
	sort.Slice(doc.Unreadable, func(i, j int) bool { return doc.Unreadable[i].File < doc.Unreadable[j].File })
	sort.Slice(doc.NotRead, func(i, j int) bool { return doc.NotRead[i].File < doc.NotRead[j].File })
	return doc, nil
}

// parseFileIn is file mode: input read as the text of rel, in the tree scan
// holds once rel is written.
func parseFileIn(scan *config.TreeScan, rel string, input []byte) (*parseTreeDoc, error) {
	doc := newParseTreeDoc("file")
	readers, reason := []string{}, ""
	switch {
	case !config.IsScannedPath(rel) && hiddenPath(rel):
		reason = notReadHidden
	case !config.IsScannedPath(rel):
		reason = notReadNotConfigName
	default:
		selected := true
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
			d, err := relTo(scan.AbsRoot, dir)
			if err != nil {
				return nil, err
			}
			c, err := relTo(scan.AbsRoot, chosen)
			if err != nil {
				return nil, err
			}
			doc.DefaultsCarriers[d] = c
		}
		if readers = readersFor(rel, selected); len(readers) == 0 {
			readers, reason = []string{}, notReadUnselectedCarrier
		}
	}
	doc.Selected[rel] = readers
	if reason != "" {
		doc.NotRead = append(doc.NotRead, parseTreeNotRead{File: rel, Reason: reason})
	}
	content, bad := parsetree.Parse(input)
	if bad != nil {
		doc.Unreadable = append(doc.Unreadable, parseTreeUnreadable{File: rel, Kind: bad.Kind, Message: bad.Message})
	} else {
		doc.Files[rel] = content
	}
	return doc, nil
}

// readersFor is who reads the config file at rel (a root-relative slash
// path the walk keeps), given whether it is the carrier its directory's
// chain selects (true for any file that is not a defaults carrier).
//
// ⛔ The generator's half is `_grar_parse._parse_config_files`: it reads
// every file the walk lists except a carrier its directory does not select,
// which it names as ignored (warn_multi_carrier). Below the root it still
// opens such a file for one question — does it write `_routing_enforced`
// (_enforced_in_unread_file) — and ignores the file when it does not parse;
// it is not a file the generator reads, so it is "not_read" here, and that
// probe has no counterpart in this document (an open question for the
// switch, #2897). The exporter reads tenant files, the root's `_` files but
// an unselected carrier (isRootPlatformKey), and each directory's selected
// carrier; another `_` file below the root it only probes for syntax
// (isNestedPlatformFile).
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
	}
	return reason
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

func hiddenPath(rel string) bool {
	for _, seg := range strings.Split(rel, "/") {
		if confdname.IsHidden(seg) {
			return true
		}
	}
	return false
}

func relTo(root, p string) (string, error) {
	r, err := filepath.Rel(root, p)
	if err != nil {
		return "", err
	}
	r = filepath.ToSlash(r)
	if !utf8.ValidString(r) {
		return "", fmt.Errorf("the path %q is not valid UTF-8; JSON cannot carry it", r)
	}
	return r, nil
}

// writeParseTree refuses a path that is not valid UTF-8 (JSON would write
// U+FFFD for its bad bytes, so two files could come out as one), then
// writes the document on one line. File contents are valid UTF-8 by
// construction (parsetree refuses anything else).
func writeParseTree(w io.Writer, doc *parseTreeDoc) error {
	check := func(p string) error {
		if !utf8.ValidString(p) {
			return fmt.Errorf("the path %q is not valid UTF-8; JSON cannot carry it", p)
		}
		return nil
	}
	for p := range doc.Selected {
		if err := check(p); err != nil {
			return err
		}
	}
	for _, n := range doc.NotRead {
		if err := check(n.File); err != nil {
			return err
		}
	}
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	return enc.Encode(doc)
}
