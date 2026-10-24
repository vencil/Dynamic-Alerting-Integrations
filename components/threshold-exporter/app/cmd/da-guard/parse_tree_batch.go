package main

// Batch file mode (`parse-tree --config-dir <dir> --inputs <file|->`): file
// mode for many paths over ONE walk of the tree (#3206, ADR-036 §3).
//
// Why it exists: the generator container fills its per-file parse cache with
// file mode, and file mode walks the whole tree (config.ScanDirTreeFiles) to
// judge one path — so filling the cache for N files one call at a time read
// the tree N times (O(N²): ~43 s for 1000 tenants). The walk is the same for
// every item of one fill, so it is made once here.
//
// ⛔ NOT A NEW VERDICT. Each line is fileModeDoc over the same kind of walk
// file mode makes, written by the same writer: byte for byte what
// `--path <path> --input <file>` prints for that item
// (TestParseTreeBatch_IsFileModeLineForLine holds the two equal on every
// fixture-catalogue tree). Nothing is shared between items but the walk.

import (
	"bufio"
	"errors"
	"io"
	"log"
	"os"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// parseTreeBatchItem is one item of --inputs: Input is the text of Path, in
// base64 (encoding/json's []byte), so any bytes — not UTF-8 too — can be
// given, as on file mode's stdin.
type parseTreeBatchItem struct {
	Path  string `json:"path"`
	Input []byte `json:"input"`
}

// runParseTreeBatch is batch file mode. Every item's path is checked as file
// mode checks --path before the walk; then one walk, then one line per item,
// written as it is made (bufio, never the whole output in memory).
func runParseTreeBatch(f *parseTreeFlags, stdin io.Reader, stdout io.Writer,
	fail func(string, ...any) int) int {
	var raw []byte
	var err error
	if f.inputs == "-" {
		raw, err = io.ReadAll(stdin)
	} else {
		raw, err = os.ReadFile(f.inputs) // #nosec G304 -- the caller names the file to read
	}
	if err != nil {
		return fail("--inputs: %v", err)
	}
	var items []parseTreeBatchItem
	if err := decodeStrict(raw, &items); err != nil || items == nil {
		if err == nil {
			err = errors.New("one array expected")
		}
		return fail("--inputs: not a JSON array of {\"path\", \"input\"}: %v", err)
	}
	rels := make([]string, len(items))
	for i, it := range items {
		if rels[i], err = cleanRelPath(it.Path); err != nil {
			return fail("--inputs item %d: path %q: %v", i, it.Path, err)
		}
	}
	scan, err := config.ScanDirTreeFiles(f.configDir, log.New(io.Discard, "", 0))
	if err != nil {
		return fail("%v", err)
	}
	if p := nonUTF8Path(scan); p != "" {
		return fail("%v", nonUTF8Error(p))
	}
	w := bufio.NewWriter(stdout)
	for i := range items {
		doc, err := fileModeDoc(scan, rels[i], items[i].Input)
		if err != nil {
			return fail("%v", err)
		}
		items[i].Input = nil // done with: the text lives on only in the line
		if err := writeParseTree(w, doc.Entries, doc); err != nil {
			return fail("%v", err)
		}
	}
	if err := w.Flush(); err != nil {
		return fail("%v", err)
	}
	return exitOK
}
