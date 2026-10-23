package main

// write.go — `da-guard edit` and `da-guard emit` (ADR-036 §2 "寫 conf.d：Go 是
// 唯一的寫者", step 10): the one writer of conf.d. A Python tool decides what
// to change from the parse result and hands that intent here; da-guard edits
// the node tree (pkg/confedit), reads the text it would write back with
// parse-tree's own file mode, and writes the file itself, atomically, or
// writes nothing.
//
// ⛔ NOTHING IS WRITTEN unless all of these hold, in this order:
//   - the target is a path a reader reads (parse-tree's file-mode entry has
//     readers, role config) and, for edit, the original file parse-tree
//     reads without a problem and validation names it under no verdict;
//   - every op applies, on the node tree and on the parse result;
//   - the re-encode keeps every node outside the edit as it was written
//     (confedit.Verify, ADR-036 §4);
//   - parse-tree file mode on the text to be written, at its target path,
//     has no problem and no verdict, and its content equals the original's
//     parse result with the ops applied (emit: the content given);
//   - for edit, the file still holds the bytes it was read from.
//
// Each refusal is exit 1 and names its reason (writeReason*); with --json the
// one stdout document says so as well.

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"log"
	"os"
	"path/filepath"

	"github.com/vencil/threshold-exporter/pkg/confedit"
	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

const (
	editCmd = "edit"
	emitCmd = "emit"
	// writeSchema names the --json document of both commands.
	writeSchema = "da-guard.write/v1"
)

// The closed set of writeResult.Status values.
const (
	writeStatusWritten     = "written"
	writeStatusUnchanged   = "unchanged" // edit: the text to write is the file's own; nothing written
	writeStatusDryRun      = "dry_run"
	writeStatusRefused     = "refused"      // exit 1
	writeStatusCallerError = "caller_error" // exit 2
)

// The closed set of writeResult.Reason values for status refused (exit 1).
const (
	// writeReasonNotRead: no reader reads the target path (parse-tree's
	// entry has no readers, or the path is no config file: a directory, a
	// FIFO, a symlink to a directory) — "entry" is parse-tree's.
	writeReasonNotRead = "not_read"
	// writeReasonSymlink (edit): the path is a symlink; writing would
	// replace the link with a file.
	writeReasonSymlink = "symlink"
	// writeReasonOriginalUnreadable (edit): parse-tree cannot read the file
	// as it is ("problem" says why), or it cannot be read at all.
	writeReasonOriginalUnreadable = "original_unreadable"
	// writeReasonOriginalUnservable (edit): parse-tree reads the file, but
	// validation names it under a verdict (the exporter cannot serve it).
	writeReasonOriginalUnservable = "original_unservable"
	// writeReasonEditNotApplicable: an op cannot be made on the file as it
	// is (a node on its path is not a mapping, set under a key that is not
	// there, a file that holds only comments); "difference.path" names it.
	writeReasonEditNotApplicable = "edit_not_applicable"
	// writeReasonNotRoundTrippable (edit): the re-encode would change a node
	// outside the edit (confedit.Verify); "difference.path" is where.
	writeReasonNotRoundTrippable = "not_round_trippable"
	// writeReasonReadBackMismatch: parse-tree reads the text to be written
	// as something else than the change means — a problem or a verdict
	// ("entry", "problem"), or a value ("difference": path, want, got).
	writeReasonReadBackMismatch = "read_back_mismatch"
	// writeReasonChangedDuringWrite: edit — the file no longer holds the
	// bytes it was read from; emit — the path was created meanwhile.
	writeReasonChangedDuringWrite = "changed_during_write"
)

// writeResult is the --json document, one per run, on every path.
type writeResult struct {
	Schema         string `json:"schema"`
	DaGuardVersion string `json:"da_guard_version"`
	Command        string `json:"command"`
	// Path is --path as given (cleaned, '/'), "" when it was not usable.
	Path   string  `json:"path"`
	Status string  `json:"status"`
	Reason *string `json:"reason"`
	// Message: for a person, on every status but written / unchanged / dry_run.
	Message *string `json:"message"`
	// Entry: parse-tree's file-mode entry the refusal rests on (the original
	// for not_read / original_*, the text to be written for
	// read_back_mismatch), else null.
	Entry *parseTreeEntry `json:"entry"`
	// Problem: the entry's problem, or the read failure of the original.
	Problem    *parseTreeProblem `json:"problem"`
	Difference *writeDifference  `json:"difference"`
	// Content: dry_run — the text that would be written.
	Content *string `json:"content"`
	// Warnings: what the write did not carry over but did not refuse for
	// (e.g. the original's owner could not be set); [] when none.
	Warnings []string `json:"warnings"`
}

// writeDifference is where a refused write differs: Path (mapping keys,
// "[i]" for a sequence item); for read_back_mismatch Want (the original's
// parse result with the ops applied, or the content emit was given) and Got
// (what parse-tree reads in the text to be written), each in parse-tree's
// JSON form, null where the value is absent.
type writeDifference struct {
	Path []string `json:"path"`
	Want any      `json:"want"`
	Got  any      `json:"got"`
}

type writeFlags struct {
	configDir string
	path      string
	input     string
	dryRun    bool
	jsonOut   bool
}

// editOp is one element of edit's "ops".
type editOp struct {
	Op    string          `json:"op"`
	Path  []string        `json:"path"`
	Value *confedit.Value `json:"value"`
}

// Op names, as edit's input spells them.
const (
	opSet       = "set"
	opUnset     = "unset"
	opEnsureMap = "ensure-map"
)

type editInput struct {
	Ops []editOp `json:"ops"`
}

type emitInput struct {
	Content *confedit.Value `json:"content"`
}

func parseWriteFlags(cmd string, args []string, errOut io.Writer) (*writeFlags, error) {
	fs := flag.NewFlagSet(programName+" "+cmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	f := &writeFlags{}
	fs.StringVar(&f.configDir, "config-dir", "", "Path to the conf.d/ root. Required.")
	fs.StringVar(&f.path, "path", "", "The file to write, relative to --config-dir. Required.")
	fs.StringVar(&f.input, "input", "", "The JSON input ('-' = stdin). Required.")
	fs.BoolVar(&f.dryRun, "dry-run", false, "Judge everything, write nothing: print the text that would be written.")
	fs.BoolVar(&f.jsonOut, "json", false, "Print one JSON document ("+writeSchema+") on stdout, on every path.")
	fs.Usage = func() { writeUsage(cmd, fs, errOut) }
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	if fs.NArg() > 0 {
		return nil, fmt.Errorf("unexpected argument %q", fs.Arg(0))
	}
	return f, nil
}

func writeUsage(cmd string, fs *flag.FlagSet, errOut io.Writer) {
	fmt.Fprintf(errOut, "Usage: %s %s --config-dir <dir> --path <rel> --input <file|-> [--dry-run] [--json]\n", programName, cmd)
	if cmd == editCmd {
		fmt.Fprintf(errOut, "Rewrite an existing conf.d file: apply ops to its yaml.v3 node tree; every node and\n"+
			"comment outside the edit is written back as it was (ADR-036 §2, §4). A comment ON an\n"+
			"edited node may be dropped (a replaced scalar keeps its own; a scalar replaced by a\n"+
			"mapping or sequence does not). The re-encode also normalises layout, never content:\n"+
			"indentation becomes 2 spaces, a trailing comment is set one space after its value,\n"+
			"an explicit key (`? a`) is written `a:`, blank lines may go. The line ending (CRLF\n"+
			"or LF, from the first line) and a leading UTF-8 BOM are kept. An edit whose result\n"+
			"is the file's own node tree is unchanged: nothing is written. The written file keeps\n"+
			"the original's mode, and its owner and group when they can be set (else a warning).\n\n"+
			"Input: {\"ops\": [<op>, ...]}, applied in order:\n"+
			"  {\"op\": \"set\", \"path\": [<key>, ...], \"value\": <value>}  the key at path := value; the\n"+
			"      mapping holding it must be there (ensure-map first); an existing key keeps its place\n"+
			"  {\"op\": \"unset\", \"path\": [<key>, ...]}                  remove the key (absent: no-op)\n"+
			"  {\"op\": \"ensure-map\", \"path\": [<key>, ...]}             path is a mapping; each missing\n"+
			"      one is created\n")
	} else {
		fmt.Fprintf(errOut, "Write a new conf.d file from the content given, keys in the order given (ADR-036 §2).\n"+
			"The path must not exist; missing directories are created (and removed again when\n"+
			"the write does not complete).\n\n"+
			"Input: {\"content\": <value>}, a \"map\" value: the file's top level.\n")
	}
	fmt.Fprintf(errOut, "<value> names its type, so a mapping's order and a scalar's type are the caller's:\n"+
		"  {\"str\": \"80\"}  written double-quoted (under _custom_alerts, a multi-line string is a literal\n"+
		"                 block when it reads back the same)\n"+
		"  {\"int\": \"80\"}  {\"float\": \"0.5\"}  canonical decimal text only (other text: send \"str\")\n"+
		"  {\"bool\": true}  {\"null\": null}\n"+
		"  {\"map\": [[\"key\", <value>], ...]}  keys unique, never `<<`; a new key is written plain\n"+
		"                 only when it is [A-Za-z_][A-Za-z0-9_-]* and not a YAML 1.1 bool/null word\n"+
		"                 (y n yes no on off true false null, any case), else double-quoted\n"+
		"  {\"seq\": [<value>, ...]}\n"+
		"The input is UTF-8 JSON: invalid UTF-8, an unpaired \\uD800-\\uDFFF escape, a member given\n"+
		"twice or a member name not spelled exactly as above is a caller error.\n\n"+
		"Before anything is written, the text is read back with parse-tree's file mode at --path: it must\n"+
		"have no problem and no verdict, and read as the original file's parse result with the ops applied\n"+
		"(emit: the content given). The file is written in its own directory and renamed into place.\n\n")
	fs.PrintDefaults()
	fmt.Fprintf(errOut, "\n--json document: {\"schema\": %q, \"da_guard_version\", \"command\", \"path\",\n"+
		"  \"status\": written|unchanged|dry_run|refused|caller_error, \"reason\", \"message\",\n"+
		"  \"entry\" (a parse-tree entry), \"problem\": {\"kind\", \"message\"},\n"+
		"  \"difference\": {\"path\", \"want\", \"got\"}, \"content\" (dry_run), \"warnings\": [<text>]};\n"+
		"null where it has no value (warnings: []). A path that exists but is no regular file\n"+
		"(a FIFO, a device, a directory) is judged by parse-tree and refused not_read, never read.\n"+
		"Refusal reasons: not_read, symlink, original_unreadable, original_unservable,\n"+
		"edit_not_applicable, not_round_trippable, read_back_mismatch, changed_during_write.\n\n"+
		"Exit codes:\n"+
		"  0  written, unchanged (edit: the text is the file's own) or dry run\n"+
		"  1  refused: nothing written; the reason is named\n"+
		"  2  caller error: bad flags, input that is not the JSON above, --path absolute or outside\n"+
		"     --config-dir, --config-dir not a directory, edit of a missing file, emit onto an\n"+
		"     existing path, or a write that failed\n", writeSchema)
}

// runEdit / runEmit are the subcommands' entry points.
func runEdit(args []string, stdin io.Reader, stdout, errOut io.Writer) int {
	return runWrite(editCmd, args, stdin, stdout, errOut)
}

func runEmit(args []string, stdin io.Reader, stdout, errOut io.Writer) int {
	return runWrite(emitCmd, args, stdin, stdout, errOut)
}

// writer carries one run's output.
type writer struct {
	cmd     string
	jsonOut bool
	stdout  io.Writer
	errOut  io.Writer
	res     writeResult
	// link puts emit's file in place (os.Link when nil); a test swaps it to
	// fail after the directories are created.
	link func(oldname, newname string) error
	// mkdir makes one directory for emit (os.Mkdir when nil); a test swaps
	// it to have another process make one first.
	mkdir func(name string, perm os.FileMode) error
}

func strp(s string) *string { return &s }

// warn records something the write did not carry over, on stderr and in the
// --json document — never silently.
func (w *writer) warn(format string, a ...any) {
	msg := fmt.Sprintf(format, a...)
	fmt.Fprintf(w.errOut, "%s %s: warning: %s\n", programName, w.cmd, msg)
	w.res.Warnings = append(w.res.Warnings, msg)
}

func (w *writer) finish(code int) int {
	if w.jsonOut {
		enc := json.NewEncoder(w.stdout)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(w.res); err != nil {
			fmt.Fprintf(w.errOut, "%s %s: %v\n", programName, w.cmd, err)
			return exitCallerErr
		}
	}
	return code
}

func (w *writer) callerError(reason, format string, a ...any) int {
	msg := fmt.Sprintf(format, a...)
	fmt.Fprintf(w.errOut, "%s %s: %s\n", programName, w.cmd, msg)
	w.res.Status, w.res.Reason, w.res.Message = writeStatusCallerError, strp(reason), strp(msg)
	return w.finish(exitCallerErr)
}

func (w *writer) refuse(reason, msg string) int {
	fmt.Fprintf(w.errOut, "%s %s: %s: refused (%s): %s; nothing was written\n", programName, w.cmd, w.res.Path, reason, msg)
	w.res.Status, w.res.Reason, w.res.Message = writeStatusRefused, strp(reason), strp(msg)
	return w.finish(exitFindings)
}

func hasJSONFlag(args []string) bool {
	for _, a := range args {
		if a == "--" {
			return false
		}
		if a == "--json" || a == "-json" || a == "--json=true" || a == "-json=true" {
			return true
		}
	}
	return false
}

func runWrite(cmd string, args []string, stdin io.Reader, stdout, errOut io.Writer) int {
	w := &writer{cmd: cmd, stdout: stdout, errOut: errOut,
		res: writeResult{Schema: writeSchema, DaGuardVersion: Version, Command: cmd, Warnings: []string{}}}
	f, err := parseWriteFlags(cmd, args, errOut)
	if errors.Is(err, flag.ErrHelp) {
		return exitOK
	}
	if err != nil {
		w.jsonOut = hasJSONFlag(args)
		return w.callerError("bad_flags", "%v", err)
	}
	w.jsonOut = f.jsonOut
	switch {
	case f.configDir == "":
		return w.callerError("bad_flags", "--config-dir is required")
	case f.path == "":
		return w.callerError("bad_flags", "--path is required")
	case f.input == "":
		return w.callerError("bad_flags", "--input is required")
	}
	rel, err := cleanRelPath(f.path)
	if err != nil {
		return w.callerError("bad_path", "--path %q: %v", f.path, err)
	}
	w.res.Path = rel
	var raw []byte
	if f.input == "-" {
		raw, err = io.ReadAll(stdin)
	} else {
		raw, err = os.ReadFile(f.input) // #nosec G304 -- the caller names the file to read
	}
	if err != nil {
		return w.callerError("bad_input", "--input: %v", err)
	}
	if err := checkInputText(raw); err != nil {
		return w.callerError("bad_input", "--input: %v", err)
	}
	if err := checkMemberNames(cmd, raw); err != nil {
		return w.callerError("bad_input", "--input: %v", err)
	}
	var ops []editOp
	var content *confedit.Value
	if cmd == editCmd {
		var in editInput
		if err := decodeStrict(raw, &in); err != nil {
			return w.callerError("bad_input", "--input: %v", err)
		}
		if in.Ops == nil {
			return w.callerError("bad_input", "--input: \"ops\" is required (a list)")
		}
		if err := checkOps(in.Ops); err != nil {
			return w.callerError("bad_input", "--input: %v", err)
		}
		ops = in.Ops
	} else {
		var in emitInput
		if err := decodeStrict(raw, &in); err != nil {
			return w.callerError("bad_input", "--input: %v", err)
		}
		if in.Content == nil || in.Content.Type != confedit.TypeMap {
			return w.callerError("bad_input", "--input: \"content\" is required, a \"map\" value (the file's top level)")
		}
		content = in.Content
	}

	scan, err := config.ScanDirTreeFiles(f.configDir, log.New(io.Discard, "", 0))
	if err != nil {
		return w.callerError("bad_path", "%v", err)
	}
	if p := nonUTF8Path(scan); p != "" {
		return w.callerError("bad_path", "%v", nonUTF8Error(p))
	}
	abs := filepath.Join(scan.AbsRoot, filepath.FromSlash(rel))
	if cmd == editCmd {
		return w.edit(scan, rel, abs, ops, f.dryRun)
	}
	return w.emit(scan, rel, abs, *content, f.dryRun)
}

// decodeStrict reads exactly one JSON object with no unknown member.
func decodeStrict(raw []byte, v any) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	if dec.More() {
		return errors.New("more than one JSON document")
	}
	var extra json.RawMessage
	if err := dec.Decode(&extra); !errors.Is(err, io.EOF) {
		return errors.New("trailing data after the JSON document")
	}
	return nil
}

func checkOps(ops []editOp) error {
	for i, op := range ops {
		switch op.Op {
		case opSet:
			if op.Value == nil {
				return fmt.Errorf("op %d (set): \"value\" is required", i)
			}
		case opUnset, opEnsureMap:
			if op.Value != nil {
				return fmt.Errorf("op %d (%s): takes no \"value\"", i, op.Op)
			}
		default:
			return fmt.Errorf("op %d: unknown op %q (set, unset, ensure-map)", i, op.Op)
		}
		if len(op.Path) == 0 {
			return fmt.Errorf("op %d (%s): \"path\" names at least one key", i, op.Op)
		}
		for _, k := range op.Path {
			if err := confedit.CheckKey(k); err != nil {
				return fmt.Errorf("op %d (%s): %v", i, op.Op, err)
			}
		}
	}
	return nil
}

// judgeAt is parse-tree's file-mode entry of rel holding text, with its
// full verdict, and the content it reads (nil when it reads none).
func judgeAt(scan *config.TreeScan, rel string, text []byte) (parseTreeEntry, bool, parsetree.Content, error) {
	doc, asOnDisk, selected, err := parseFileIn(scan, rel, text)
	if err != nil {
		return parseTreeEntry{}, false, nil, err
	}
	e := doc.Entries[0]
	setFileVerdict(scan, &e, asOnDisk, selected, rel, text)
	return e, asOnDisk, doc.Files[rel], nil
}

func notReadMessage(e parseTreeEntry) string {
	if e.Role != config.RoleConfig {
		return fmt.Sprintf("the path is no config file a reader reads (role %s)", e.Role)
	}
	return fmt.Sprintf("no reader reads the path (not_read %s)", e.NotRead)
}

// readRegular reads abs only when the opened handle is a regular file
// (fstat, not a name check): a FIFO is opened without blocking and a device
// or a directory is never read. regular false: nothing was read.
func readRegular(abs string) (data []byte, fi os.FileInfo, regular bool, err error) {
	f, err := openNoBlock(abs)
	if err != nil {
		return nil, nil, false, err
	}
	defer func() { _ = f.Close() }()
	if fi, err = f.Stat(); err != nil {
		return nil, nil, false, err
	}
	if !fi.Mode().IsRegular() {
		return nil, fi, false, nil
	}
	data, err = io.ReadAll(f)
	return data, fi, true, err
}

// refuseNotAFile judges a path that is no regular file with parse-tree (the
// walk's own entry for it) and refuses it not_read.
func (w *writer) refuseNotAFile(scan *config.TreeScan, rel string) int {
	e, _, _, err := judgeAt(scan, rel, nil)
	if err != nil {
		return w.callerError("bad_path", "%v", err)
	}
	w.res.Entry, w.res.Problem = &e, e.Problem
	return w.refuse(writeReasonNotRead, fmt.Sprintf("the path is no regular file (role %s); nothing is read from it", e.Role))
}

func (w *writer) edit(scan *config.TreeScan, rel, abs string, ops []editOp, dryRun bool) int {
	fi, err := os.Lstat(abs)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return w.callerError("path_missing", "--path %q does not exist; edit rewrites an existing file (a new one is emit's)", rel)
	case err == nil && fi.Mode()&os.ModeSymlink != 0:
		// What a reader makes of the link is the walk's: a link to no
		// config file is not_read below; a link to one would be replaced.
		if e, asOnDisk, _, jerr := judgeAt(scan, rel, nil); jerr == nil && (asOnDisk || e.Role != config.RoleConfig) {
			w.res.Entry, w.res.Problem = &e, e.Problem
			return w.refuse(writeReasonNotRead, notReadMessage(e))
		}
		return w.refuse(writeReasonSymlink, "the path is a symlink; writing would replace the link with a file")
	case err == nil && !fi.Mode().IsRegular():
		return w.refuseNotAFile(scan, rel)
	}
	// A path swapped for a FIFO, a device, … since the Lstat is not read
	// (readRegular: regular false, original nil); the judge below then gives
	// it the walk's own not_a_file entry, refused not_read.
	original, ofi, _, rerr := readRegular(abs)
	e, asOnDisk, base, err := judgeAt(scan, rel, original)
	if err != nil {
		return w.callerError("bad_path", "%v", err)
	}
	if asOnDisk || e.Role != config.RoleConfig || len(e.Readers) == 0 {
		w.res.Entry, w.res.Problem = &e, e.Problem
		return w.refuse(writeReasonNotRead, notReadMessage(e))
	}
	if rerr != nil {
		w.res.Problem = &parseTreeProblem{Kind: parsetree.KindReadFailure, Message: rerr.Error()}
		return w.refuse(writeReasonOriginalUnreadable, "the file cannot be read: "+rerr.Error())
	}
	w.res.Entry = &e
	if e.Problem != nil {
		w.res.Problem = e.Problem
		return w.refuse(writeReasonOriginalUnreadable,
			fmt.Sprintf("parse-tree cannot read the file as it is (%s: %s)", e.Problem.Kind, e.Problem.Message))
	}
	if e.Verdict != "" {
		return w.refuse(writeReasonOriginalUnservable,
			fmt.Sprintf("validation names the file as it is under verdict %s; repair it first", e.Verdict))
	}
	w.res.Entry = nil

	doc, err := confedit.ParseDoc(confedit.WithoutCRLF(original))
	if err != nil {
		return w.refuse(writeReasonEditNotApplicable, err.Error())
	}
	if base == nil {
		base = parsetree.Content{}
	}
	// want is the original's parse result with the ops applied; base stays
	// the original's (ContentSet and friends edit in place).
	want := confedit.CloneContent(base)
	for i, op := range ops {
		var nerr, cerr error
		switch op.Op {
		case opSet:
			blocks := false
			for _, k := range op.Path {
				blocks = blocks || k == confedit.CustomAlertsKey
			}
			nerr = doc.Set(op.Path, op.Value.Node(blocks))
			if nerr == nil {
				cerr = confedit.ContentSet(want, op.Path, op.Value.Content())
			}
		case opUnset:
			_, nerr = doc.Unset(op.Path)
			if nerr == nil {
				cerr = confedit.ContentUnset(want, op.Path)
			}
		case opEnsureMap:
			nerr = doc.EnsureMap(op.Path)
			if nerr == nil {
				cerr = confedit.ContentEnsureMap(want, op.Path)
			}
		}
		for _, err := range []error{nerr, cerr} {
			var pe *confedit.PathError
			if errors.As(err, &pe) {
				w.res.Difference = &writeDifference{Path: nonNilPath(pe.At)}
				return w.refuse(writeReasonEditNotApplicable, fmt.Sprintf("op %d (%s): %v", i, op.Op, err))
			}
			if err != nil {
				return w.refuse(writeReasonEditNotApplicable, fmt.Sprintf("op %d (%s): %v", i, op.Op, err))
			}
		}
	}
	encoded, err := doc.Bytes()
	if err != nil {
		return w.refuse(writeReasonEditNotApplicable, "re-encode: "+err.Error())
	}
	// The original's line ending and BOM; every check below reads these
	// very bytes.
	out := confedit.KeepFraming(original, encoded)
	if _, _, _, same := confedit.ContentDifference(base, want); same && confedit.SameTree(original, out) {
		// Nothing changes, in the file's nodes nor in what the ops mean: a
		// layout-only re-encode is not worth a write (and a commit). Both
		// must hold — an unset of a key a `<<` merge supplies leaves the
		// nodes as they were while the change means the key is gone, which
		// the read-back below refuses.
		fmt.Fprintf(w.errOut, "%s %s: %s: unchanged; nothing written\n", programName, w.cmd, rel)
		w.res.Status = writeStatusUnchanged
		return w.finish(exitOK)
	}
	if d := confedit.Verify(original, out, doc.Edits()); d != nil {
		w.res.Difference = &writeDifference{Path: nonNilPath(d.Path)}
		return w.refuse(writeReasonNotRoundTrippable,
			"the re-encode would also change what the edit does not: "+d.String())
	}
	if code, ok := w.readBack(scan, rel, out, want); !ok {
		return code
	}
	if dryRun {
		return w.dryRun(out)
	}
	return w.commit(abs, out, original, fi.Mode().Perm(), ofi)
}

func (w *writer) emit(scan *config.TreeScan, rel, abs string, content confedit.Value, dryRun bool) int {
	if _, err := os.Lstat(abs); err == nil || !errors.Is(err, fs.ErrNotExist) {
		if err != nil {
			return w.callerError("bad_path", "--path %q: %v", rel, err)
		}
		if st, serr := os.Stat(abs); serr == nil && st.Mode().IsRegular() {
			return w.callerError("path_exists", "--path %q exists; emit writes a new file (an existing one is edit's)", rel)
		}
		// A FIFO, a device, a directory, a link to none of them: no file a
		// reader reads, and nothing emit may write over.
		return w.refuseNotAFile(scan, rel)
	}
	out, err := confedit.Encode(confedit.NewDoc(content.Node(false)).Node())
	if err != nil {
		return w.refuse(writeReasonEditNotApplicable, "encode: "+err.Error())
	}
	want, _ := content.Content().(map[string]any)
	if code, ok := w.readBack(scan, rel, out, parsetree.Content(want)); !ok {
		return code
	}
	if dryRun {
		return w.dryRun(out)
	}
	return w.commit(abs, out, nil, 0o644, nil)
}

// readBack holds the text to be written to parse-tree's reading at rel:
// read by a reader, no problem, no verdict, content equal to want.
func (w *writer) readBack(scan *config.TreeScan, rel string, out []byte, want parsetree.Content) (int, bool) {
	e, asOnDisk, got, err := judgeAt(scan, rel, out)
	if err != nil {
		return w.callerError("bad_path", "%v", err), false
	}
	if asOnDisk || e.Role != config.RoleConfig || len(e.Readers) == 0 {
		w.res.Entry, w.res.Problem = &e, e.Problem
		return w.refuse(writeReasonNotRead, notReadMessage(e)), false
	}
	if e.Problem != nil {
		w.res.Entry, w.res.Problem = &e, e.Problem
		return w.refuse(writeReasonReadBackMismatch,
			fmt.Sprintf("parse-tree would not read the text to be written (%s: %s)", e.Problem.Kind, e.Problem.Message)), false
	}
	if e.Verdict != "" {
		w.res.Entry = &e
		return w.refuse(writeReasonReadBackMismatch,
			fmt.Sprintf("validation would name the written file under verdict %s", e.Verdict)), false
	}
	if p, wv, gv, ok := confedit.ContentDifference(want, got); !ok {
		w.res.Difference = &writeDifference{Path: p, Want: wv, Got: gv}
		return w.refuse(writeReasonReadBackMismatch,
			fmt.Sprintf("parse-tree reads %s differently in the text to be written than the change means",
				confedit.JoinPath(p))), false
	}
	return 0, true
}

func nonNilPath(p []string) []string {
	if p == nil {
		return []string{}
	}
	return p
}

func (w *writer) dryRun(out []byte) int {
	w.res.Status, w.res.Content = writeStatusDryRun, strp(string(out))
	if !w.jsonOut {
		if _, err := w.stdout.Write(out); err != nil {
			return w.callerError("write_failed", "stdout: %v", err)
		}
		return exitOK
	}
	return w.finish(exitOK)
}

// commit writes out to abs atomically: a hidden temporary file in abs's own
// directory (no walk reads a hidden name), synced, then moved into place —
// for edit (original non-nil) only while abs still holds original, with the
// original's owner and group when they can be set (orig); for emit only
// while abs does not exist, and the directories it created are removed again
// when the write does not complete.
func (w *writer) commit(abs string, out, original []byte, perm os.FileMode, orig os.FileInfo) int {
	dir := filepath.Dir(abs)
	var created []string // the directories this run made, outermost first
	done := false
	defer func() {
		if !done {
			for i := len(created) - 1; i >= 0; i-- {
				_ = os.Remove(created[i]) // only ever empty: the temporary file is gone first
			}
		}
	}()
	if original == nil {
		var missing []string // innermost first
		for d := dir; ; d = filepath.Dir(d) {
			if _, err := os.Lstat(d); err == nil || filepath.Dir(d) == d {
				break
			}
			missing = append(missing, d)
		}
		mkdir := w.mkdir
		if mkdir == nil {
			mkdir = os.Mkdir
		}
		// One level at a time: a level counts as this run's only when its
		// Mkdir succeeds — one another process made meanwhile (EEXIST) is not
		// ours, and the cleanup above never removes it.
		for i := len(missing) - 1; i >= 0; i-- {
			switch err := mkdir(missing[i], 0o755); { // #nosec G301 -- conf.d directories are world-readable by convention
			case err == nil:
				created = append(created, missing[i])
			case errors.Is(err, fs.ErrExist):
				if fi, serr := os.Stat(missing[i]); serr != nil || !fi.IsDir() {
					return w.callerError("write_failed", "%s exists and is no directory", missing[i])
				}
			default:
				return w.callerError("write_failed", "%v", err)
			}
		}
	}
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(abs)+".da-guard-*")
	if err != nil {
		return w.callerError("write_failed", "%v", err)
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }()
	_, werr := tmp.Write(out)
	if werr == nil {
		werr = tmp.Chmod(perm)
	}
	if werr == nil && orig != nil {
		if msg := chownLike(tmp, orig); msg != "" {
			w.warn("%s", msg)
		}
	}
	if werr == nil {
		werr = tmp.Sync()
	}
	if cerr := tmp.Close(); werr == nil {
		werr = cerr
	}
	if werr != nil {
		return w.callerError("write_failed", "%v", werr)
	}
	if original != nil {
		now, _, regular, err := readRegular(abs)
		if err != nil || !regular || !bytes.Equal(now, original) {
			return w.refuse(writeReasonChangedDuringWrite, "the file changed after it was read; re-run on its new content")
		}
		if err := os.Rename(tmpName, abs); err != nil {
			return w.callerError("write_failed", "%v", err)
		}
	} else {
		link := w.link
		if link == nil {
			link = os.Link
		}
		// A hard link never replaces what is there: a path created meanwhile
		// is not overwritten.
		switch err := link(tmpName, abs); {
		case errors.Is(err, fs.ErrExist):
			return w.refuse(writeReasonChangedDuringWrite, "the path was created after it was judged; nothing replaced it")
		case err != nil:
			// A file system without hard links: check, then rename.
			if _, lerr := os.Lstat(abs); !errors.Is(lerr, fs.ErrNotExist) {
				return w.refuse(writeReasonChangedDuringWrite, "the path was created after it was judged; nothing replaced it")
			}
			if err := os.Rename(tmpName, abs); err != nil {
				return w.callerError("write_failed", "%v", err)
			}
		}
	}
	done = true
	if d, err := os.Open(dir); err == nil { // #nosec G304 -- the directory just written
		_ = d.Sync()
		_ = d.Close()
	}
	fmt.Fprintf(w.errOut, "%s %s: wrote %s\n", programName, w.cmd, w.res.Path)
	w.res.Status = writeStatusWritten
	return w.finish(exitOK)
}
