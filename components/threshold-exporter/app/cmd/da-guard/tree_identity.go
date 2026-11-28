package main

import (
	"fmt"
	"io"
)

// configHashField is a document's `config_hash` (#3965): the identity of the
// walk the document was computed from (pkg/config treeIdentity — the value
// `da-guard parse-tree` reports for the same tree and the exporter serves on
// /api/v1/config/identity), so a reader can tell whether two documents read
// the same tree. null (`identity` "") when the document claims none: no walk
// ran, or an overlay made it read a tree that is not the one on disk.
func configHashField(identity string) *string {
	if identity == "" {
		return nil
	}
	return &identity
}

// betweenWalks, when set, runs between two walks of --config-dir in one run
// (after the default check's ScopeEffective and before redundant_override's
// key verdict walks the tree again; after key-verdict's facts walk and after
// its first scan): a test changes the tree there to have the
// run's later walks read another one. nil outside tests; set only by
// non-parallel tests (#3965).
var betweenWalks func(configDir string)

// sameWalks reports whether two walks of one run read the same tree: first
// and later are their identities. A first walk that claims none ("")
// compares with anything; once it named one, a later walk that names none
// failed on a tree the first one walked (the root gone since) and is
// another tree too. When they differ it says so on errOut: the run's
// answer would mix two trees, so the caller ends it with exit 2 and no
// document.
func sameWalks(first, later string, errOut io.Writer) bool {
	if first == "" || first == later {
		return true
	}
	fmt.Fprintf(errOut, "%s: --config-dir changed during this run (its walks read %s, then %s); nothing was judged — run it again\n",
		programName, first, walkName(later))
	return false
}

// walkName is a walk's identity as the change message names it: a walk that
// names none failed.
func walkName(identity string) string {
	if identity == "" {
		return "none (the walk failed)"
	}
	return identity
}
