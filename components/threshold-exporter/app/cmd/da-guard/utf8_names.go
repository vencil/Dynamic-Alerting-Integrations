package main

// A conf.d path that is not valid UTF-8 (#3206): every da-guard subcommand
// that walks conf.d refuses such a tree with exit 2, through the one check
// below. parse-tree always had to (JSON cannot carry the name: encoding/json
// writes U+FFFD for each invalid byte, so two files could come out as one,
// and every Python reader of conf.d reads parse-tree); validation,
// served-values, effective and key-refs read the tree and exited 0 on it, so
// "da-guard passes" and "every conf.d tool refuses" described the same tree.

import (
	"fmt"
	"io"
	"log"
	"unicode/utf8"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// nonUTF8Path is the first path the walk recorded (kept files, unreadable
// entries, paths that are no file, directories) that is not valid UTF-8, in
// that order; "" when every one is.
func nonUTF8Path(scan *config.TreeScan) string {
	for _, k := range scan.Keys {
		if !utf8.ValidString(k) {
			return k
		}
	}
	for _, u := range scan.Unreadable {
		if !utf8.ValidString(u.RelKey) {
			return u.RelKey
		}
	}
	for _, nf := range scan.NotFiles {
		if !utf8.ValidString(nf.RelKey) {
			return nf.RelKey
		}
	}
	for _, d := range scan.Dirs {
		if !utf8.ValidString(d) {
			return d
		}
	}
	return ""
}

// nonUTF8Error is the refusal of one such path; %q writes each invalid byte
// escaped (`\xff`), so the message names the bytes to rename.
func nonUTF8Error(rel string) error {
	return fmt.Errorf("the path %q is not valid UTF-8 (invalid bytes shown escaped); "+
		"no da-guard output can carry it — JSON cannot — so the tree is refused: rename it", rel)
}

// refuseNonUTF8Tree is the check for a subcommand whose load does not hand
// it the walk: one names-only walk (config.ScanDirTreeNames — the exporter's
// walker, no byte read) of configDir. It prints the refusal as who and
// returns true when the walk recorded such a path; a walk that fails says
// nothing here (the subcommand's own load reports that tree).
func refuseNonUTF8Tree(configDir, who string, errOut io.Writer) bool {
	scan, err := config.ScanDirTreeNames(configDir, log.New(io.Discard, "", 0))
	if err != nil {
		return false
	}
	if p := nonUTF8Path(scan); p != "" {
		fmt.Fprintf(errOut, "%s: %v\n", who, nonUTF8Error(p))
		return true
	}
	return false
}
