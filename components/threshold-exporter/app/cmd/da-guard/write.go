package main

// write.go — `da-guard write` (ADR-036 §2 "寫 conf.d：Go 是唯一的寫者",
// step 10): how a Python tool changes conf.d (tenant-api, the other Go
// writer, shares pkg/confedit). A Python tool decides what to change from
// the parse result and hands that intent here; da-guard edits the node trees
// (pkg/confedit), judges the tree as it would be with every file of the call
// written, and writes the files itself, or writes nothing.
//
// Each file of a call is one of three kinds (writeKind*): edit an existing
// file with ops; create a new one from a value (comments in it are layout:
// confedit.Entry, confedit.Item, head_comment, foot_comment); move an existing file, byte for byte, to a
// new path (the source is gone after).
//
// ⛔ ONE CALL IS A BATCH (#3206 step 10c): the input names every file
// (`{"files": [...]}`, one file is a batch of one), and the whole batch is
// judged once — every proposed text read at its path in ONE walk of the tree
// (config.ScanDirTreeFilesWith: the disk with the batch's texts in place,
// in memory, nothing staged), and that tree judged by parse-tree's own tree
// mode (parseTreeOf + the validation verdicts), once. A file's reading can
// depend on another file of the batch (which `_defaults` carrier a
// directory selects); judging each file alone against the disk could not
// see that, and a subprocess plus a tree judgement per file was O(N²).
//
// ⛔ NOTHING IS WRITTEN unless all of these hold for every file:
//   - the target is a path a reader reads (parse-tree's file-mode entry has
//     readers, role config) and, for edit, the original file parse-tree
//     reads without a problem. A verdict on the original does NOT refuse by
//     itself: an edit may repair a file the exporter cannot serve, never
//     worsen one — what is judged is the text in place after the call
//     (below), so "unservable after" stays impossible. The same holds for an
//     edit that changes nothing: `unchanged` is answered only when the file
//     left in place carries no verdict. A file holding only comments is
//     edited as an empty one, its comments written back first;
//   - every op applies, on the node tree and on the parse result;
//   - the re-encode keeps every node outside the edit as it was written
//     (confedit.Verify, ADR-036 §4);
//   - in the tree with the whole batch in place (a move: its source gone,
//     its bytes at the target), the file's tree-mode entry has readers, role
//     config, no problem and no verdict, and its content equals the
//     original's parse result with the ops applied (create: the value given;
//     move: the source's own parse result);
//   - the call does not worsen the tree outside its files' own entries
//     (write_worsen.go: readers lost, a new verdict or problem anywhere, a
//     new error of da-guard's own validation) — reason worsens_tree;
//   - before anything is put in place: every edited file and every move's
//     source still holds the bytes it was read from; no create's or move's
//     target exists. This is a comparison, not a lock (write_commit.go).
//
// Each refusal is exit 1 and names its file and reason (writeReason*); with
// --json the one stdout document lists every file.

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
	writeCmd = "write"
	// writeSchema names the --json document. v3 (#3206): one command for
	// edit, create and move; each file result names its kind.
	writeSchema = "da-guard.write/v3"
)

// The closed set of writeFileResult.Kind values: what the input entry asks.
const (
	writeKindEdit   = "edit"   // {"path", "ops"}
	writeKindCreate = "create" // {"path", "create", "head_comment"?, "foot_comment"?}
	writeKindMove   = "move"   // {"path", "from"}
)

// The closed set of writeResult.Status values (the call) and
// writeFileResult.Status values (one file). not_written and maybe_written
// are a file's only.
const (
	writeStatusWritten     = "written"
	writeStatusUnchanged   = "unchanged" // edit: the text to write is the file's own; nothing written
	writeStatusDryRun      = "dry_run"
	writeStatusRefused     = "refused"      // exit 1
	writeStatusCallerError = "caller_error" // exit 2
	// writeStatusNotWritten: the file passed or was not reached, and is not
	// written because the call stopped (another file was refused, a write
	// failed), or it was written and removed again (an emit rolled back).
	writeStatusNotWritten = "not_written"
	// writeStatusMaybeWritten: whether the file is in place is not known —
	// an emitted file the roll-back could not remove again.
	writeStatusMaybeWritten = "maybe_written"
)

// The closed set of writeFileResult.Reason values for status refused (exit 1).
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
	// writeReasonEditNotApplicable: an op cannot be made on the file as it
	// is (a node on its path is not a mapping, set under a key that is not
	// there, a file that holds only comments); "difference.path" names it.
	writeReasonEditNotApplicable = "edit_not_applicable"
	// writeReasonNotRoundTrippable (edit): the re-encode would change a node
	// outside the edit (confedit.Verify); "difference.path" is where.
	writeReasonNotRoundTrippable = "not_round_trippable"
	// writeReasonReadBackMismatch: parse-tree reads the text in place after
	// the call as something else than the change means — a problem or a
	// verdict ("entry", "problem"), or a value ("difference": path, want,
	// got).
	writeReasonReadBackMismatch = "read_back_mismatch"
	// writeReasonChangedDuringWrite: edit — the file no longer holds the
	// bytes it was read from; emit — the path was created meanwhile.
	writeReasonChangedDuringWrite = "changed_during_write"
)

// The call's own writeResult.Reason values (status caller_error, exit 2);
// null when the status is the files' (a file refused or a caller error).
const (
	writeReasonBadFlags = "bad_flags"
	writeReasonBadInput = "bad_input"
	writeReasonBadPath  = "bad_path"
	// writeReasonWriteFailed: a write failed and no file was left written
	// (also a file's reason, on the file whose write failed).
	writeReasonWriteFailed = "write_failed"
	// writeReasonPartialWrite: the call stopped after some files were put
	// in place; "files" says which (written / maybe_written).
	writeReasonPartialWrite = "partial_write"
)

// writeResult is the --json document, one per call, on every path.
type writeResult struct {
	Schema         string  `json:"schema"`
	DaGuardVersion string  `json:"da_guard_version"`
	Command        string  `json:"command"`
	Status         string  `json:"status"`
	Reason         *string `json:"reason"`
	// Message: for a person, on every status but written / unchanged / dry_run.
	Message *string `json:"message"`
	// Files: one result per input file, in the input's order, once the
	// input is read; [] when the call stopped before (bad flags or input).
	Files []writeFileResult `json:"files"`
	// Worsened: what the tree would have after the call that it does not
	// have now, outside the batch's own entries (write_worsen.go; reason
	// worsens_tree); [] when nothing.
	Worsened []writeWorsened `json:"worsened"`
}

// writeFileResult is one file's result.
type writeFileResult struct {
	// Path is the file's path as given, cleaned ('/'); for a move, the
	// target.
	Path string `json:"path"`
	// Kind is the entry's kind (writeKind*); From a move's source (cleaned),
	// else null.
	Kind   string  `json:"kind"`
	From   *string `json:"from"`
	Status string  `json:"status"`
	Reason *string `json:"reason"`
	// Message: for a person, on refused / caller_error / maybe_written, and
	// on not_written when the file was written and removed again.
	Message *string `json:"message"`
	// Entry: parse-tree's entry the refusal rests on (file mode for the
	// original or the path, tree mode for the text in place after the
	// call), else null.
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
// (what parse-tree reads in the text in place after the call), each in
// parse-tree's JSON form, null where the value is absent.
type writeDifference struct {
	Path []string `json:"path"`
	Want any      `json:"want"`
	Got  any      `json:"got"`
}

type writeFlags struct {
	configDir string
	input     string
	dryRun    bool
	jsonOut   bool
}

// editOp is one element of an edit file's "ops".
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

// fileInput is one element of the input's "files": edit gives "ops", emit
// "content" (checkMemberNames refuses the other one).
type fileInput struct {
	Path        *string         `json:"path"`
	Ops         []editOp        `json:"ops"`
	Create      *confedit.Value `json:"create"`
	HeadComment *string         `json:"head_comment"`
	FootComment *string         `json:"foot_comment"`
	From        *string         `json:"from"`
}

type writeInput struct {
	Files []fileInput `json:"files"`
}

// writeFile is one file of the call, from its input to its write.
type writeFile struct {
	kind     string
	rel, abs string
	ops      []editOp        // edit
	content  *confedit.Value // create
	head     string          // create: the file's head comment (CommentText), "" for none
	foot     string          // create: the file's foot comment (CommentText), "" for none
	from     string          // move: the source, root-relative
	fromAbs  string          // move: the source's path
	res      *writeFileResult
	// pending: no refusal or caller error so far.
	pending bool
	// unchanged (edit): the text in place after the call is the file's own.
	unchanged bool
	out       []byte            // the text to write
	want      parsetree.Content // what parse-tree must read in it
	original  []byte            // edit: the bytes read; move: the source's
	perm      os.FileMode
	ofi       os.FileInfo // edit: the original's, for its owner
	tmp       string      // the temporary file, once made (edit, create)
	// placed (create, move): the file this call put at abs (its inode): a
	// roll-back undoes abs only while it is still this file.
	placed os.FileInfo
	// sourceGone (move): the source name was removed after the link.
	sourceGone bool
}

func parseWriteFlags(cmd string, args []string, errOut io.Writer) (*writeFlags, error) {
	fs := flag.NewFlagSet(programName+" "+cmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	f := &writeFlags{}
	fs.StringVar(&f.configDir, "config-dir", "", "Path to the conf.d/ root. Required.")
	fs.StringVar(&f.input, "input", "", "The JSON input naming every file ('-' = stdin). Required.")
	fs.BoolVar(&f.dryRun, "dry-run", false, "Judge everything, write nothing: print the texts that would be written.")
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
	fmt.Fprintf(errOut, "Usage: %s %s --config-dir <dir> --input <file|-> [--dry-run] [--json]\n", programName, cmd)
	fmt.Fprintf(errOut, "Change conf.d files in one batch, judged once, all or nothing (ADR-036 §2, §4).\n\n"+
		"Input: {\"files\": [<entry>, ...]}, each path once (a move's source counts), each entry one of:\n"+
		"  {\"path\": <rel>, \"ops\": [<op>, ...]}   edit an existing file: its ops applied in order\n"+
		"  {\"path\": <rel>, \"create\": <value>, \"head_comment\": <text>, \"foot_comment\": <text>}\n"+
		"      a new file whose top level is the \"map\" value, keys in the order given;\n"+
		"      head_comment (optional) above it, foot_comment (optional) after its last node\n"+
		"  {\"path\": <rel>, \"from\": <rel>}   move an existing file here, byte for byte; the source\n"+
		"      is gone after. The source is judged as an edit's original: a file the walk reads, with\n"+
		"      readers, no symlink on the way, that parse-tree reads (else not_read / symlink /\n"+
		"      original_unreadable; missing: source_missing)\n"+
		"<rel> is relative to --config-dir. A create's or a move's target must not exist: an existing\n"+
		"regular file is a caller error (path_exists, exit 2); an existing path that is no regular\n"+
		"file (a FIFO, a device, a directory, a symlink to one) is refused not_read (exit 1). Missing\n"+
		"directories are created (and removed again when the call does not complete).\n\n"+
		"Ops:\n"+
		"  {\"op\": \"set\", \"path\": [<key>, ...], \"value\": <value>}  the key at path := value; the\n"+
		"      mapping holding it must be there (ensure-map first); an existing key keeps its place.\n"+
		"      An empty path sets the whole top level (value: a \"map\"); nothing of it is kept\n"+
		"  {\"op\": \"unset\", \"path\": [<key>, ...]}                  remove the key (absent: no-op)\n"+
		"  {\"op\": \"ensure-map\", \"path\": [<key>, ...]}             path is a mapping; each missing\n"+
		"      one is created\n"+
		"An edit writes every node and comment outside it back as it was. A comment ON an edited node\n"+
		"may be dropped (a replaced scalar keeps its own; a scalar replaced by a mapping or sequence\n"+
		"does not). The re-encode also normalises layout, never content: indentation becomes 2\n"+
		"spaces, a trailing comment is set one space after its value, an explicit key (`? a`) is\n"+
		"written `a:`, blank lines may go. The line ending (CRLF or LF, from the first line) and a\n"+
		"leading UTF-8 BOM are kept. An edit whose result is the file's own node tree is unchanged:\n"+
		"nothing is written. A written file keeps the original's mode, and its owner and group when\n"+
		"they can be set (else a warning); a moved file is the same file (its mode and owner too).\n\n")
	fmt.Fprintf(errOut, "<value> names its type, so a mapping's order and a scalar's type are the caller's:\n"+
		"  {\"str\": \"80\"}  written double-quoted (under _custom_alerts, a multi-line string is a literal\n"+
		"                 block when it reads back the same)\n"+
		"  {\"int\": \"80\"}  {\"float\": \"0.5\"}  canonical decimal text only (other text: send \"str\")\n"+
		"  {\"bool\": true}  {\"null\": null}\n"+
		"  {\"map\": [[\"key\", <value>], [\"key\", <value>, {\"head\": <text>, \"line\": <text>}], ...]}\n"+
		"                 keys unique, never `<<`; a new key is written plain only when it is\n"+
		"                 [A-Za-z_][A-Za-z0-9_-]* and not a YAML 1.1 bool/null word (y n yes no on off\n"+
		"                 true false null, any case), else double-quoted. The optional third element\n"+
		"                 is the key's comments: head above it, line after `key:`\n"+
		"  {\"seq\": [<value>, [<value>, {\"head\": <text>, \"line\": <text>}], ...]}\n"+
		"                 an item given as the pair [value, {comments}] carries its comments: head\n"+
		"                 above its `- `, line after its value (refused after a non-empty mapping or\n"+
		"                 sequence item, which yaml.v3 would write on another line)\n"+
		"A comment (head, line, head_comment, foot_comment) is layout, not content: never read back, compared or\n"+
		"judged. Each of its lines is written `# <line>`, so it cannot become YAML; a line comment is\n"+
		"one line; a carriage return, another control character but a tab, DEL, U+0085, U+2028,\n"+
		"U+2029 and U+FEFF are refused (bad_input), and so is a line comment on a key whose value is\n"+
		"an empty mapping or sequence (written {} / []: give it a head comment).\n"+
		"The input is UTF-8 JSON: invalid UTF-8, an unpaired \\uD800-\\uDFFF escape, a member given\n"+
		"twice or a member name not spelled exactly as above is a caller error.\n\n"+
		"The whole call is judged once, before anything is written: the tree is read with every\n"+
		"file's text in place at its path and every move's source gone (one walk, in memory) and\n"+
		"judged by parse-tree's tree mode. Each file must have readers, no problem and no verdict\n"+
		"there, and read as the original's parse result with the ops applied (create: the value\n"+
		"given; move: the source's own). One refusal and no file is written; every refused file is\n"+
		"named. The call may repair the tree, never worsen it: it is refused whole (worsens_tree)\n"+
		"when the tree after it has what the tree before it has not — a path outside the batch\n"+
		"losing readers, a verdict or problem on any entry, an error of da-guard's own validation\n"+
		"(a tenant declared twice, ...). An edited file is written in its own directory and renamed\n"+
		"into place; a created one is linked into place; a move links the source at its target and\n"+
		"removes the source name.\n\n")
	fs.PrintDefaults()
	fmt.Fprintf(errOut, "\n--json document: {\"schema\": %q, \"da_guard_version\", \"command\",\n"+
		"  \"status\": written|unchanged|dry_run|refused|caller_error, \"reason\", \"message\",\n"+
		"  \"files\": [{\"path\", \"kind\": edit|create|move, \"from\", \"status\": written|unchanged|\n"+
		"  dry_run|refused|caller_error|not_written|maybe_written, \"reason\", \"message\", \"entry\" (a\n"+
		"  parse-tree entry), \"problem\": {\"kind\", \"message\"}, \"difference\": {\"path\", \"want\", \"got\"},\n"+
		"  \"content\" (dry_run), \"warnings\": [<text>]}],\n"+
		"  \"worsened\": [{\"kind\": readers_lost|verdict|problem|duplicate_tenant|finding, \"path\",\n"+
		"  \"tenant\", \"finding\", \"message\"}]}; null where it has no value (warnings, worsened: []). The call's\n"+
		"reason: bad_flags, bad_input, bad_path, write_failed, partial_write, worsens_tree, or null\n"+
		"when its status is the files'. A path that exists but\n"+
		"is no regular file (a FIFO, a device, a directory) is judged by parse-tree and refused\n"+
		"not_read, never read. An original that parse-tree reads but validation names under a\n"+
		"verdict may be edited: the write is judged by the text in place after the call, which must\n"+
		"carry no verdict.\n"+
		"A file's refusal reasons: not_read, symlink, original_unreadable, edit_not_applicable,\n"+
		"not_round_trippable, read_back_mismatch, changed_during_write.\n\n"+
		"A write that fails after files were put in place: the files it created are removed again and\n"+
		"its moves undone (each not_written, or maybe_written when it cannot); an edit's rename cannot\n"+
		"be undone, so the files renamed by then are written (reason partial_write).\n\n"+
		"Exit codes:\n"+
		"  0  every file written or unchanged (edit: the text is the file's own), or a dry run\n"+
		"  1  refused: nothing written; every refused file is named\n"+
		"  2  caller error: bad flags, input that is not the JSON above, a path given twice, absolute\n"+
		"     or outside --config-dir, --config-dir not a directory, edit of a missing file or a move\n"+
		"     of a missing source, a create or move onto an existing regular file, or a write that\n"+
		"     failed (partial_write: some files are written)\n", writeSchema)
}

// runWriteCmd is the subcommand's entry point.
func runWriteCmd(args []string, stdin io.Reader, stdout, errOut io.Writer) int {
	return runWrite(writeCmd, args, stdin, stdout, errOut)
}

// writer carries one call's output and the seams its tests swap.
type writer struct {
	cmd     string
	jsonOut bool
	stdout  io.Writer
	errOut  io.Writer
	res     writeResult
	// link puts an emitted file in place (os.Link when nil); a test swaps it
	// to fail part-way through a batch.
	link func(oldname, newname string) error
	// rename puts an edited file in place, and an emitted one on a file
	// system without hard links (os.Rename when nil).
	rename func(oldname, newname string) error
	// remove takes an emitted file out again when the call does not
	// complete (os.Remove when nil); a test swaps it to fail.
	remove func(name string) error
	// mkdir makes one directory for emit (os.Mkdir when nil); a test swaps
	// it to have another process make one first.
	mkdir func(name string, perm os.FileMode) error
	// chown sets a temporary file's owner and group (its File.Chown when
	// nil); a test swaps it to have the kernel refuse.
	chown func(uid, gid int) error
	// read reads edit's original (readRegular when nil); a test swaps it to
	// see the path change between the open and the judge.
	read func(abs string) ([]byte, os.FileInfo, bool, error)
}

// ownerWarning gives the written file the original's owner and group with
// chown, best effort, and returns what it could not carry over ("" when
// nothing): a process that is not uid 0 cannot give a file away, but may
// still set the group when it is a member — so the group is tried alone
// (uid -1) after the pair fails, and only what still fails is reported.
//
// cur and curGID are the temporary file's own owner and group: what already
// matches is not set, and a refusal names only what differs — an
// original this process owns, refused its group, says "group", never
// "owner and group".
func ownerWarning(chown func(uid, gid int) error, uid, gid, cur, curGID int) string {
	switch {
	case uid == cur && gid == curGID:
		return ""
	case uid == cur:
		if err := chown(-1, gid); err != nil {
			return fmt.Sprintf("the written file could not be given the original's group (gid %d): %v", gid, err)
		}
		return ""
	}
	err := chown(uid, gid)
	if err == nil {
		return ""
	}
	if gerr := chown(-1, gid); gerr == nil {
		return fmt.Sprintf("the written file could not be given the original's owner (uid %d): %v; its group (gid %d) is kept", uid, err, gid)
	}
	return fmt.Sprintf("the written file could not be given the original's owner and group (uid %d, gid %d): %v", uid, gid, err)
}

func strp(s string) *string { return &s }

// warn records something a file's write did not carry over, on stderr and
// in the --json document — never silently.
func (w *writer) warn(f *writeFile, format string, a ...any) {
	msg := fmt.Sprintf(format, a...)
	fmt.Fprintf(w.errOut, "%s %s: %s: warning: %s\n", programName, w.cmd, f.rel, msg)
	f.res.Warnings = append(f.res.Warnings, msg)
}

// finish writes the --json document. A file no step gave a status to was
// not written.
func (w *writer) finish(code int) int {
	for i := range w.res.Files {
		if w.res.Files[i].Status == "" {
			w.res.Files[i].Status = writeStatusNotWritten
		}
	}
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

// callerError ends the call with the call's own caller error.
func (w *writer) callerError(reason, format string, a ...any) int {
	msg := fmt.Sprintf(format, a...)
	fmt.Fprintf(w.errOut, "%s %s: %s\n", programName, w.cmd, msg)
	w.res.Status, w.res.Reason, w.res.Message = writeStatusCallerError, strp(reason), strp(msg)
	return w.finish(exitCallerErr)
}

// refuse records f's refusal; the call goes on judging the other files.
func (w *writer) refuse(f *writeFile, reason, msg string) {
	fmt.Fprintf(w.errOut, "%s %s: %s: refused (%s): %s\n", programName, w.cmd, f.rel, reason, msg)
	f.res.Status, f.res.Reason, f.res.Message = writeStatusRefused, strp(reason), strp(msg)
	f.pending = false
}

// fileCallerError records a caller error of f's (its path, or its write).
func (w *writer) fileCallerError(f *writeFile, reason, format string, a ...any) {
	msg := fmt.Sprintf(format, a...)
	fmt.Fprintf(w.errOut, "%s %s: %s: %s\n", programName, w.cmd, f.rel, msg)
	f.res.Status, f.res.Reason, f.res.Message = writeStatusCallerError, strp(reason), strp(msg)
	f.pending = false
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
	return (&writer{cmd: cmd, stdout: stdout, errOut: errOut}).run(args, stdin)
}

func (w *writer) run(args []string, stdin io.Reader) int {
	w.res = writeResult{Schema: writeSchema, DaGuardVersion: Version, Command: w.cmd, Files: []writeFileResult{},
		Worsened: []writeWorsened{}}
	f, err := parseWriteFlags(w.cmd, args, w.errOut)
	if errors.Is(err, flag.ErrHelp) {
		return exitOK
	}
	if err != nil {
		w.jsonOut = hasJSONFlag(args)
		return w.callerError(writeReasonBadFlags, "%v", err)
	}
	w.jsonOut = f.jsonOut
	switch {
	case f.configDir == "":
		return w.callerError(writeReasonBadFlags, "--config-dir is required")
	case f.input == "":
		return w.callerError(writeReasonBadFlags, "--input is required")
	}
	var raw []byte
	if f.input == "-" {
		raw, err = io.ReadAll(stdin)
	} else {
		raw, err = os.ReadFile(f.input) // #nosec G304 -- the caller names the file to read
	}
	if err != nil {
		return w.callerError(writeReasonBadInput, "--input: %v", err)
	}
	files, code, ok := w.takeInput(raw)
	if !ok {
		return code
	}
	// From here on every input file has its result: one no step reaches is
	// not_written (finish).
	w.res.Files = make([]writeFileResult, len(files))
	for i, wf := range files {
		w.res.Files[i] = writeFileResult{Path: wf.rel, Kind: wf.kind, Warnings: []string{}}
		if wf.kind == writeKindMove {
			w.res.Files[i].From = strp(wf.from)
		}
		wf.res = &w.res.Files[i]
	}

	scan, err := config.ScanDirTreeFiles(f.configDir, log.New(io.Discard, "", 0))
	if err != nil {
		return w.callerError(writeReasonBadPath, "%v", err)
	}
	if p := nonUTF8Path(scan); p != "" {
		return w.callerError(writeReasonBadPath, "%v", nonUTF8Error(p))
	}
	for _, wf := range files {
		wf.abs = filepath.Join(scan.AbsRoot, filepath.FromSlash(wf.rel))
		wf.pending = true
		switch wf.kind {
		case writeKindEdit:
			w.prepareEdit(scan, wf)
		case writeKindCreate:
			w.prepareCreate(scan, wf)
		case writeKindMove:
			wf.fromAbs = filepath.Join(scan.AbsRoot, filepath.FromSlash(wf.from))
			w.prepareMove(scan, wf)
		}
	}
	after, afterDoc, overlays, code, ok := w.judgeTree(scan, files)
	if !ok {
		return code
	}
	if code, stop := w.concludeJudgement(files); stop {
		return code
	}
	if len(overlays) > 0 {
		batch := make(map[string]bool, len(files))
		for _, wf := range files {
			batch[wf.rel] = true
			if wf.from != "" {
				batch[wf.from] = true
			}
		}
		worse, err := worsening(scan, after, afterDoc, overlays, true, batch)
		if err != nil {
			return w.callerError(writeReasonBadPath, "%v", err)
		}
		if len(worse) > 0 {
			return w.refuseWorsening(worse)
		}
	}
	if f.dryRun {
		return w.dryRun(files)
	}
	return w.commit(files)
}

// takeInput reads the input into the call's files, or ends the call with a
// caller error (ok false).
func (w *writer) takeInput(raw []byte) ([]*writeFile, int, bool) {
	bad := func(format string, a ...any) ([]*writeFile, int, bool) {
		return nil, w.callerError(writeReasonBadInput, "--input: "+format, a...), false
	}
	if err := checkInputText(raw); err != nil {
		return bad("%v", err)
	}
	if err := checkMemberNames(raw); err != nil {
		return bad("%v", err)
	}
	var in writeInput
	if err := decodeStrict(raw, &in); err != nil {
		return bad("%v", err)
	}
	if len(in.Files) == 0 {
		return bad("\"files\" is required: a list of at least one file")
	}
	files := make([]*writeFile, len(in.Files))
	seen := make(map[string]int, len(in.Files))
	for i, fi := range in.Files {
		if fi.Path == nil {
			return bad("files[%d]: \"path\" is required", i)
		}
		rel, err := cleanRelPath(*fi.Path)
		if err != nil {
			return nil, w.callerError(writeReasonBadPath, "--input: files[%d].path %q: %v", i, *fi.Path, err), false
		}
		if j, twice := seen[rel]; twice {
			return bad("files[%d].path %q is files[%d]'s as well: each file is given once", i, rel, j)
		}
		seen[rel] = i
		wf := &writeFile{rel: rel}
		n := 0
		for _, given := range []bool{fi.Ops != nil, fi.Create != nil, fi.From != nil} {
			if given {
				n++
			}
		}
		if n != 1 {
			return bad("files[%d]: exactly one of \"ops\" (edit), \"create\" (a new file) and \"from\" (a move)", i)
		}
		if fi.HeadComment != nil && fi.Create == nil {
			return bad("files[%d]: \"head_comment\" goes with \"create\" only", i)
		}
		if fi.FootComment != nil && fi.Create == nil {
			return bad("files[%d]: \"foot_comment\" goes with \"create\" only", i)
		}
		switch {
		case fi.Ops != nil:
			if err := checkOps(fi.Ops); err != nil {
				return bad("files[%d]: %v", i, err)
			}
			wf.kind, wf.ops = writeKindEdit, fi.Ops
		case fi.Create != nil:
			if fi.Create.Type != confedit.TypeMap {
				return bad("files[%d]: \"create\" is a \"map\" value (the file's top level)", i)
			}
			wf.kind, wf.content = writeKindCreate, fi.Create
			if fi.HeadComment != nil {
				head, err := confedit.CommentText(*fi.HeadComment, true)
				if err != nil {
					return bad("files[%d]: \"head_comment\": %v", i, err)
				}
				wf.head = head
			}
			if fi.FootComment != nil {
				foot, err := confedit.CommentText(*fi.FootComment, true)
				if err != nil {
					return bad("files[%d]: \"foot_comment\": %v", i, err)
				}
				wf.foot = foot
			}
		default:
			from, err := cleanRelPath(*fi.From)
			if err != nil {
				return nil, w.callerError(writeReasonBadPath, "--input: files[%d].from %q: %v", i, *fi.From, err), false
			}
			wf.kind, wf.from = writeKindMove, from
		}
		files[i] = wf
	}
	// A move's source is a path of the call too: no other entry names it.
	for i, wf := range files {
		if wf.from == "" {
			continue
		}
		if j, twice := seen[wf.from]; twice {
			return bad("files[%d].from %q is files[%d]'s path or source as well: each file is given once", i, wf.from, j)
		}
		seen[wf.from] = i
	}
	return files, 0, true
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
			if len(op.Path) == 0 && op.Value.Type != confedit.TypeMap {
				return fmt.Errorf("op %d (set): an empty \"path\" sets the whole top level: \"value\" is a \"map\"", i)
			}
		case opUnset, opEnsureMap:
			if op.Value != nil {
				return fmt.Errorf("op %d (%s): takes no \"value\"", i, op.Op)
			}
			if len(op.Path) == 0 {
				return fmt.Errorf("op %d (%s): \"path\" names at least one key", i, op.Op)
			}
		default:
			return fmt.Errorf("op %d: unknown op %q (set, unset, ensure-map)", i, op.Op)
		}
		if op.Path == nil {
			return fmt.Errorf("op %d (%s): \"path\" is required (a list; [] for set's whole top level)", i, op.Op)
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

// refuseNotAFile judges a path (rel) that is no regular file with parse-tree
// (the walk's own entry for it) and refuses f not_read.
func (w *writer) refuseNotAFile(scan *config.TreeScan, f *writeFile, rel, what string) {
	e, _, _, err := judgeAt(scan, rel, nil)
	if err != nil {
		w.fileCallerError(f, writeReasonBadPath, "%v", err)
		return
	}
	f.res.Entry, f.res.Problem = &e, e.Problem
	w.refuse(f, writeReasonNotRead, fmt.Sprintf("%s is no regular file (role %s); nothing is read from it", what, e.Role))
}

// existing is a file a write reads and replaces or removes: an edit's
// original, or a move's source — judged by one rule (readOriginal).
type existing struct {
	data []byte
	fi   os.FileInfo // its Lstat
	ofi  os.FileInfo // the opened handle's, for its owner
	base parsetree.Content
}

// readOriginal reads and judges the existing file at rel (an edit's path, a
// move's source) for f: it must exist (else a caller error, missing), be no
// symlink and a regular file, be read by a reader in the walk as it is on
// disk (parse-tree's file-mode entry: readers, role config — a hidden or
// non-config name, or a directory on the way the walk does not descend, is
// not_read), and be read by parse-tree without a problem. A verdict on it is
// no refusal (repair is allowed). ok false: f's refusal or caller error is
// recorded. what names rel in messages ("the path", "the source …").
func (w *writer) readOriginal(scan *config.TreeScan, f *writeFile, rel, abs, missing, what string) (existing, bool) {
	fi, err := os.Lstat(abs)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		if f.kind == writeKindMove {
			w.fileCallerError(f, missing, "%s does not exist", what)
		} else {
			w.fileCallerError(f, missing, "%q does not exist; an edit rewrites an existing file (a new one is a create's)", rel)
		}
		return existing{}, false
	case err == nil && fi.Mode()&os.ModeSymlink != 0:
		// What a reader makes of the link is the walk's: a link to no
		// config file is not_read below; a link to one would be replaced.
		if e, asOnDisk, _, jerr := judgeAt(scan, rel, nil); jerr == nil && (asOnDisk || e.Role != config.RoleConfig) {
			f.res.Entry, f.res.Problem = &e, e.Problem
			w.refuse(f, writeReasonNotRead, what+": "+notReadMessage(e))
			return existing{}, false
		}
		w.refuse(f, writeReasonSymlink, what+" is a symlink; a write would replace or remove the link, not the file")
		return existing{}, false
	case err == nil && !fi.Mode().IsRegular():
		w.refuseNotAFile(scan, f, rel, what)
		return existing{}, false
	}
	read := w.read
	if read == nil {
		read = readRegular
	}
	original, ofi, regular, rerr := read(abs)
	if rerr == nil && !regular {
		// Swapped for a FIFO, a device, … since the Lstat: nothing was read.
		// Refused here, whatever the path is by the time it is judged — a
		// second swap back to a regular file must not carry a nil original
		// on to the edit and the write.
		w.refuseNotAFile(scan, f, rel, what)
		return existing{}, false
	}
	e, asOnDisk, base, err := judgeAt(scan, rel, original)
	if err != nil {
		w.fileCallerError(f, writeReasonBadPath, "%v", err)
		return existing{}, false
	}
	if asOnDisk || e.Role != config.RoleConfig || len(e.Readers) == 0 {
		f.res.Entry, f.res.Problem = &e, e.Problem
		w.refuse(f, writeReasonNotRead, what+": "+notReadMessage(e))
		return existing{}, false
	}
	if rerr != nil {
		f.res.Problem = &parseTreeProblem{Kind: parsetree.KindReadFailure, Message: rerr.Error()}
		w.refuse(f, writeReasonOriginalUnreadable, what+" cannot be read: "+rerr.Error())
		return existing{}, false
	}
	if e.Problem != nil {
		f.res.Entry, f.res.Problem = &e, e.Problem
		w.refuse(f, writeReasonOriginalUnreadable,
			fmt.Sprintf("parse-tree cannot read %s as it is (%s: %s)", what, e.Problem.Kind, e.Problem.Message))
		return existing{}, false
	}
	if base == nil {
		base = parsetree.Content{}
	}
	return existing{data: original, fi: fi, ofi: ofi, base: base}, true
}

// prepareEdit judges one edit file alone — its path, its original, its ops
// and the §4 invariant — and leaves the text to write and what it must read
// as (f.out, f.want) for the batch's judgement; or records why not.
func (w *writer) prepareEdit(scan *config.TreeScan, f *writeFile) {
	orig, ok := w.readOriginal(scan, f, f.rel, f.abs, "path_missing", "the path")
	if !ok {
		return
	}
	original, ofi, fi, base := orig.data, orig.ofi, orig.fi, orig.base
	// A verdict on the original is not a refusal (repair is allowed): the
	// text in place after the call must have none (judgeTree).

	// A file holding only comments is edited as an empty one (as a blank
	// file is): the ops apply to an empty mapping and the comments are
	// written back, verbatim, ahead of it — they become the head comment of
	// its first key.
	src := confedit.WithoutCRLF(original)
	var lead []byte
	if docs, perr := confedit.ParseAll(src); perr == nil && len(docs) == 0 && len(bytes.TrimSpace(src)) > 0 {
		lead = bytes.TrimPrefix(src, []byte("\uFEFF"))
		if !bytes.HasSuffix(lead, []byte("\n")) {
			lead = append(append([]byte{}, lead...), '\n')
		}
		src = nil
	}
	doc, err := confedit.ParseDoc(src)
	if err != nil {
		w.refuse(f, writeReasonEditNotApplicable, err.Error())
		return
	}
	if base == nil {
		base = parsetree.Content{}
	}
	// want is the original's parse result with the ops applied; base stays
	// the original's (ContentSet and friends edit in place).
	want := confedit.CloneContent(base)
	for i, op := range f.ops {
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
				f.res.Difference = &writeDifference{Path: nonNilPath(pe.At)}
			}
			if err != nil {
				w.refuse(f, writeReasonEditNotApplicable, fmt.Sprintf("op %d (%s): %v", i, op.Op, err))
				return
			}
		}
	}
	encoded, err := doc.Bytes()
	if err != nil {
		w.refuse(f, writeReasonEditNotApplicable, "re-encode: "+err.Error())
		return
	}
	if lead != nil {
		encoded = append(lead, encoded...)
	}
	// The original's line ending and BOM; every check below reads these
	// very bytes.
	out := confedit.KeepFraming(original, encoded)
	f.original, f.ofi, f.perm, f.want = original, ofi, fi.Mode().Perm(), want
	if _, _, _, same := confedit.ContentDifference(base, want); same && (lead != nil || confedit.SameTree(original, out)) {
		// Nothing changes, in the file's nodes nor in what the ops mean: a
		// layout-only re-encode is not worth a write (and a commit). Both
		// must hold — an unset of a key a `<<` merge supplies leaves the
		// nodes as they were while the change means the key is gone, which
		// the read-back refuses.
		//
		// ⛔ The text in place after the call must read back clean whether
		// or not it changed: judgeTree judges this file as it stays, and an
		// unchanged file validation names is refused, never answered
		// `unchanged`.
		f.unchanged, f.out = true, original
		return
	}
	// A comments-only original has nothing to keep but its comments, written
	// above verbatim (Verify would count its zero documents); the read-back
	// judges the rest.
	if lead == nil {
		if d := confedit.Verify(original, out, doc.Edits()); d != nil {
			f.res.Difference = &writeDifference{Path: nonNilPath(d.Path)}
			w.refuse(f, writeReasonNotRoundTrippable,
				"the re-encode would also change what the edit does not: "+d.String())
			return
		}
	}
	f.out = out
}

// prepareTarget judges a create's or a move's target alone — it must not
// exist, and a reader must read the path (file mode, no text; what it reads
// there is judgeTree's) — and reports whether it may go on.
func (w *writer) prepareTarget(scan *config.TreeScan, f *writeFile) bool {
	if _, err := os.Lstat(f.abs); err == nil || !errors.Is(err, fs.ErrNotExist) {
		if err != nil {
			w.fileCallerError(f, writeReasonBadPath, "%q: %v", f.rel, err)
			return false
		}
		if st, serr := os.Stat(f.abs); serr == nil && st.Mode().IsRegular() {
			w.fileCallerError(f, "path_exists", "%q exists; a %s writes a new file (an existing one is an edit's)", f.rel, f.kind)
			return false
		}
		// A FIFO, a device, a directory, a link to none of them: no file a
		// reader reads, and nothing a create or a move may write over.
		w.refuseNotAFile(scan, f, f.rel, "the path")
		return false
	}
	e, asOnDisk, _, err := judgeAt(scan, f.rel, nil)
	if err != nil {
		w.fileCallerError(f, writeReasonBadPath, "%v", err)
		return false
	}
	if asOnDisk || e.Role != config.RoleConfig || len(e.Readers) == 0 {
		f.res.Entry, f.res.Problem = &e, e.Problem
		w.refuse(f, writeReasonNotRead, notReadMessage(e))
		return false
	}
	return true
}

// prepareCreate judges one new file alone — its path — and leaves its text
// (the value, its comments, the head and foot comments) and its content for the
// batch's judgement; or records why not.
func (w *writer) prepareCreate(scan *config.TreeScan, f *writeFile) {
	if !w.prepareTarget(scan, f) {
		return
	}
	doc := confedit.NewDoc(f.content.Node(false)).Node()
	// On the document node: yaml.v3 writes its head comment first, then a
	// blank line, and its foot comment after a blank line below the last
	// node, at column 0, whatever that node is (an empty `{}` too) —
	// TestWrite_AFootCommentEndsTheFile pins it.
	doc.HeadComment, doc.FootComment = f.head, f.foot
	out, err := confedit.Encode(doc)
	if err != nil {
		w.refuse(f, writeReasonEditNotApplicable, "encode: "+err.Error())
		return
	}
	want, _ := f.content.Content().(map[string]any)
	f.out, f.want, f.perm = out, parsetree.Content(want), 0o644
}

// prepareMove judges one move alone — its source exactly as an edit's
// original (readOriginal: a file of the walk, no symlink on the way, read by
// a reader, read by parse-tree without a problem; its bytes are what the
// target will hold) and its target (prepareTarget) — and leaves the bytes
// and the source's parse result for the batch's judgement; or records why
// not. A source the walk does not read (a hidden or non-config name, under a
// symlinked or hidden directory, outside the tree through a link) is
// refused, so the batch's tree always takes it out (judgeTree).
func (w *writer) prepareMove(scan *config.TreeScan, f *writeFile) {
	src, ok := w.readOriginal(scan, f, f.from, f.fromAbs, "source_missing", fmt.Sprintf("the source %q", f.from))
	if !ok {
		return
	}
	if !w.prepareTarget(scan, f) {
		return
	}
	f.out, f.original, f.want, f.perm = src.data, src.data, src.base, src.fi.Mode().Perm()
}

// judgeTree is the batch's one judgement: the tree as it is once every
// pending file's text is in place, walked once with the texts read at their
// paths (config.ScanDirTreeFilesWith; a created file's missing directories
// taken as made) and judged by parse-tree's tree mode (parseTreeOf and
// validation's verdicts, setVerdicts over treeDropped) — the very document
// `parse-tree --config-dir` would print for that tree. Each pending file is
// then held to its entry there: readers, role config, no problem, no
// verdict, and its content equal to f.want. ok false: the call ended (a
// caller error).
func (w *writer) judgeTree(disk *config.TreeScan, files []*writeFile) (*config.TreeScan, *parseTreeDoc, []config.FileOverlay, int, bool) {
	var overlays []config.FileOverlay
	for _, f := range files {
		if f.pending && !f.unchanged {
			overlays = append(overlays, config.FileOverlay{Rel: f.rel, Data: f.out})
			// A move's source is gone after the call. It is a file of the
			// walk (prepareMove: judged as an edit's original), so the walk
			// takes it out; one it does not list is an OverlayError below,
			// refused.
			if f.kind == writeKindMove {
				overlays = append(overlays, config.FileOverlay{Rel: f.from, Remove: true})
			}
		}
	}
	if len(overlays) == 0 && !anyPending(files) {
		return nil, nil, nil, 0, true
	}
	byRel := make(map[string]*writeFile, len(files))
	for _, f := range files {
		byRel[f.rel] = f
		if f.from != "" {
			byRel[f.from] = f
		}
	}
	var after *config.TreeScan
	for {
		var err error
		after, err = config.ScanDirTreeFilesWith(disk.AbsRoot, log.New(io.Discard, "", 0), overlays, true)
		var oe *config.OverlayError
		if errors.As(err, &oe) && byRel[oe.Rel] != nil && byRel[oe.Rel].pending {
			// A path the walk would not read the text at (a parent it
			// could not list, …): that file is refused, the rest judged
			// without it.
			f := byRel[oe.Rel]
			w.refuse(f, writeReasonNotRead, oe.Reason)
			overlays = withoutOverlay(withoutOverlay(overlays, f.rel), f.from)
			continue
		}
		if err != nil {
			return nil, nil, nil, w.callerError(writeReasonBadPath, "%v", err), false
		}
		break
	}
	doc, err := parseTreeOf(after)
	if err == nil {
		err = setVerdicts(doc.Entries, treeDropped(after))
	}
	if err != nil {
		return nil, nil, nil, w.callerError(writeReasonBadPath, "%v", err), false
	}
	entries := make(map[string]*parseTreeEntry, len(doc.Entries))
	for i := range doc.Entries {
		entries[doc.Entries[i].Path] = &doc.Entries[i]
	}
	for _, f := range files {
		if !f.pending {
			continue
		}
		e := entries[f.rel]
		switch {
		case e == nil:
			w.refuse(f, writeReasonNotRead, "the walk does not keep the path once the call is written (no parse-tree entry)")
		case e.Role != config.RoleConfig || len(e.Readers) == 0:
			f.res.Entry, f.res.Problem = e, e.Problem
			w.refuse(f, writeReasonNotRead, notReadMessage(*e))
		case e.Problem != nil:
			f.res.Entry, f.res.Problem = e, e.Problem
			w.refuse(f, writeReasonReadBackMismatch,
				fmt.Sprintf("parse-tree would not read the text to be written (%s: %s)", e.Problem.Kind, e.Problem.Message))
		case e.Verdict != "" && f.unchanged:
			f.res.Entry = e
			w.refuse(f, writeReasonReadBackMismatch,
				fmt.Sprintf("the edit leaves the file as it is, and validation names it under verdict %s", e.Verdict))
		case e.Verdict != "":
			f.res.Entry = e
			w.refuse(f, writeReasonReadBackMismatch,
				fmt.Sprintf("validation would name the written file under verdict %s", e.Verdict))
		default:
			if p, wv, gv, same := confedit.ContentDifference(f.want, doc.Files[f.rel]); !same {
				f.res.Difference = &writeDifference{Path: p, Want: wv, Got: gv}
				w.refuse(f, writeReasonReadBackMismatch,
					fmt.Sprintf("parse-tree reads %s differently in the text to be written than the change means",
						confedit.JoinPath(p)))
			}
		}
	}
	return after, doc, overlays, 0, true
}

// refuseWorsening ends the call, nothing written: every file passes on its
// own entry, but the batch would worsen the tree (write_worsen.go).
func (w *writer) refuseWorsening(worse []writeWorsened) int {
	for _, x := range worse {
		fmt.Fprintf(w.errOut, "%s %s: the call would worsen the tree: %s %s: %s\n", programName, w.cmd, x.Kind, x.Path, x.Message)
	}
	msg := fmt.Sprintf("the call would worsen the tree in %d place(s) outside its own files' entries; nothing was written", len(worse))
	fmt.Fprintf(w.errOut, "%s %s: %s\n", programName, w.cmd, msg)
	w.res.Status, w.res.Reason, w.res.Message, w.res.Worsened = writeStatusRefused, strp(writeReasonWorsensTree), strp(msg), worse
	return w.finish(exitFindings)
}

func anyPending(files []*writeFile) bool {
	for _, f := range files {
		if f.pending {
			return true
		}
	}
	return false
}

func withoutOverlay(overlays []config.FileOverlay, rel string) []config.FileOverlay {
	out := overlays[:0:0]
	for _, o := range overlays {
		if o.Rel != rel {
			out = append(out, o)
		}
	}
	return out
}

// concludeJudgement ends the call when any file was refused or is a caller
// error (stop true): nothing is written, and every other file is
// not_written.
func (w *writer) concludeJudgement(files []*writeFile) (int, bool) {
	refused, callerErrs := 0, 0
	for _, f := range files {
		switch f.res.Status {
		case writeStatusRefused:
			refused++
		case writeStatusCallerError:
			callerErrs++
		}
	}
	if refused+callerErrs == 0 {
		return 0, false
	}
	msg := fmt.Sprintf("%d of %d file(s) refused, %d a caller error; nothing was written", refused, len(files), callerErrs)
	fmt.Fprintf(w.errOut, "%s %s: %s\n", programName, w.cmd, msg)
	w.res.Message = strp(msg)
	if callerErrs > 0 {
		w.res.Status = writeStatusCallerError
		return w.finish(exitCallerErr), true
	}
	w.res.Status = writeStatusRefused
	return w.finish(exitFindings), true
}

func nonNilPath(p []string) []string {
	if p == nil {
		return []string{}
	}
	return p
}

// dryRun answers every file without writing: its text (dry_run), or
// unchanged. Without --json the texts go to stdout, each after a
// `==> <path> <==` line.
func (w *writer) dryRun(files []*writeFile) int {
	w.res.Status = writeStatusUnchanged
	for _, f := range files {
		if f.unchanged {
			f.res.Status = writeStatusUnchanged
			fmt.Fprintf(w.errOut, "%s %s: %s: unchanged\n", programName, w.cmd, f.rel)
			continue
		}
		f.res.Status, f.res.Content = writeStatusDryRun, strp(string(f.out))
		w.res.Status = writeStatusDryRun
		if !w.jsonOut {
			if _, err := fmt.Fprintf(w.stdout, "==> %s <==\n%s", f.rel, f.out); err != nil {
				return w.callerError(writeReasonWriteFailed, "stdout: %v", err)
			}
		}
	}
	return w.finish(exitOK)
}
