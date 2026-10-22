package main

// verdict: what each conf.d path makes da-guard's validation do — the
// per-entry "verdict" of parse-tree (#3206, ADR-036 §7 step 9). A reader of
// conf.d that must fail closed on every path validation names (exit 3's
// parse_failed / unreadable, exit 4's unreadable_outside_run) reads this one
// field instead of composing that answer from role and problem.
//
// ⛔ ONE RULE, TWO CONSUMERS. pathVerdict is the rule; validation's
// unreadable_outside_run evidence (undeterminedFromParseTree) and
// parse-tree's verdict are both it. What it is given is the set of paths
// validation names under exit 3, built from the walk parse-tree already made:
// the walk's half (config.TreeUnreadable, the function ScopeEffective fills
// unreadable with) and the build's half (TreeScan.DroppedFiles — the
// exporter's build's own per-file decode, config.DropsConfFile, over the bytes
// the walk read), never a list of parse-tree's own. One walk, no second load.
// fixture_catalog_unix_test.go (assertVerdictIsValidationsLists) holds the
// verdicts equal to validation's report on every catalogue tree.

import (
	"fmt"
	"sort"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// Closed set of parseTreeEntry.Verdict values ("" is written as null:
// nothing to report).
const (
	// verdictUnservable: validation names the path under exit 3 — in
	// parse_failed (the exporter's load drops the file) or in unreadable (the
	// walk cannot stat, read or list it). The exporter cannot serve it.
	verdictUnservable = "unservable"
	// verdictTreeRefused: validation names the path under exit 4 as
	// unreadable_outside_run — parse-tree names a problem the exporter's load
	// does not drop (a config-named directory, a symlink to a directory, a
	// root listing that failed part-way) — and the route generator refuses
	// the whole tree on it.
	verdictTreeRefused = "tree_refused"
)

// pathVerdict is the verdict on one entry, given the paths a validation run
// names under exit 3 (dropped, reportedPaths' set).
func pathVerdict(e parseTreeEntry, dropped map[string]bool) string {
	switch {
	case dropped[e.Path]:
		return verdictUnservable
	case e.Problem != nil:
		return verdictTreeRefused
	}
	return ""
}

// walkDropped is the walk's half of the paths a whole-tree validation run
// (no --scope) names under exit 3: unreadable. List mode reads no byte and
// has only this half; so do the entries file mode does not judge.
func walkDropped(scan *config.TreeScan) map[string]bool {
	return reportedPaths(nil, unreadableEntries(config.TreeUnreadable(scan)))
}

// treeDropped is the whole set: walkDropped plus parse_failed, the files the
// exporter's build drops (TreeScan.DroppedFiles over the bytes this walk read).
func treeDropped(scan *config.TreeScan) map[string]bool {
	return reportedPaths(scan.DroppedFiles(), unreadableEntries(config.TreeUnreadable(scan)))
}

// setVerdicts sets every entry's Verdict from dropped. A dropped path no
// entry has is an error: the verdicts would not name it, and a reader would
// read the tree as servable (a da-guard defect).
func setVerdicts(entries []parseTreeEntry, dropped map[string]bool) error {
	seen := make(map[string]bool, len(entries))
	for i := range entries {
		entries[i].Verdict = pathVerdict(entries[i], dropped)
		seen[entries[i].Path] = true
	}
	var missing []string
	for p := range dropped {
		if !seen[p] {
			missing = append(missing, p)
		}
	}
	if len(missing) > 0 {
		sort.Strings(missing)
		return fmt.Errorf("validation would drop %q, and the walk recorded no entry for it "+
			"(a da-guard defect)", missing)
	}
	return nil
}

// setFileVerdict is file mode's verdict on its one entry, --path (rel), with
// no load of the tree: the walk half of the tree on disk (walkDropped), plus,
// for a file the walk would keep at rel, whether the exporter's build drops
// input there (config.DropsConfFile, the build's own per-file decode;
// selectedCarrier as parseFileIn decided it). asOnDisk: the entry is the
// walk's own record of rel (a directory, a symlink to one, a path that is no
// regular file), judged as tree mode judges it. A path the walk would not
// record (hidden, not a config name, under a directory the walk does not
// descend) has none. pathVerdict is the rule, as in tree mode; it is given
// the entry as tree mode would hold it: content no reader reads is not
// parsed there (parseTreeOf), so its problem does not count.
func setFileVerdict(disk *config.TreeScan, e *parseTreeEntry, asOnDisk, selectedCarrier bool, rel string, input []byte) {
	dropped := walkDropped(disk)
	if !asOnDisk {
		switch e.NotRead {
		case notReadHidden, notReadNotConfigName, notReadParentNotWalked:
			return
		}
		if config.DropsConfFile(rel, input, selectedCarrier) {
			dropped[rel] = true
		}
	}
	judged := *e
	if !asOnDisk && len(judged.Readers) == 0 {
		judged.Problem = nil
	}
	e.Verdict = pathVerdict(judged, dropped)
}
