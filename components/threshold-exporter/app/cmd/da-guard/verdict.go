package main

// verdict: what each conf.d path makes da-guard's validation do — the
// per-entry "verdict" of parse-tree (#3206, ADR-036 §7 step 9). A reader of
// conf.d that must fail closed on every path validation names (exit 3's
// parse_failed / unreadable, exit 4's unreadable_outside_run) reads this one
// field instead of composing that answer from role and problem.
//
// ⛔ ONE RULE, TWO CONSUMERS. pathVerdict is the rule; validation's
// unreadable_outside_run evidence (undeterminedFromParseTree) and
// parse-tree's verdict are both it. What it is given is validation's own
// exit-3 lists — config.ScopeDropped, the steps of config.ScopeEffective that
// fill ScopedTenants.ParseFailed and .Unreadable, passed through
// reportedPaths as validation passes them — never a list of parse-tree's own.
// TestFixtureCatalog_RecipesGiveTheirEntries holds the two outputs equal on
// every catalogue tree (fixture_catalog_unix_test.go,
// assertVerdictIsValidationsLists).

import (
	"errors"
	"fmt"
	"io"
	"log"
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

// treeDropped is the set of paths a whole-tree validation run (no --scope)
// names under exit 3, on the tree opts makes the walk read (the zero opts:
// the tree on disk).
func treeDropped(configDir string, opts config.ScanOptions) (map[string]bool, error) {
	s, err := config.ScopeDropped(configDir, "", opts)
	if err != nil {
		return nil, err
	}
	return reportedPaths(s.ParseFailed, unreadableEntries(s.Unreadable)), nil
}

// setVerdicts sets every entry's Verdict from dropped. A dropped path no
// entry has is an error: the verdicts would not name it, and a reader would
// read the tree as servable (the two walks saw different trees — it changed
// between them — or a da-guard defect).
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
		return fmt.Errorf("the exporter's load drops %q, and the walk recorded no entry for it: "+
			"the tree changed during the run, or a da-guard defect; run again", missing)
	}
	return nil
}

// treeVerdicts is tree mode's verdict of every path of scan (a
// ScanDirTreeFiles walk of configDir as opts makes it read), by path.
func treeVerdicts(configDir string, scan *config.TreeScan, opts config.ScanOptions) (map[string]string, error) {
	doc, err := parseTreeOf(scan)
	if err != nil {
		return nil, err
	}
	dropped, err := treeDropped(configDir, opts)
	if err != nil {
		return nil, err
	}
	if err := setVerdicts(doc.Entries, dropped); err != nil {
		return nil, err
	}
	out := make(map[string]string, len(doc.Entries))
	for _, e := range doc.Entries {
		out[e.Path] = e.Verdict
	}
	return out, nil
}

// setFileVerdict is file mode's verdict: the one tree mode gives --path (rel)
// in the tree as it is once input is written there. asOnDisk: the entry is
// already tree mode's entry of the tree on disk (a directory, or a path that
// is no regular file), whose text is not read — its verdict is that tree's.
// A path the walk would not record (hidden, not a config name, under a
// directory the walk does not descend) has none: no validation list can name
// it. When the walk refuses to take input at rel (ErrOverlay: rel exists but
// cannot be read, or its directory cannot be listed), the verdict is the tree
// on disk's — that directory's own entry carries its verdict there, rel none.
func setFileVerdict(configDir string, disk *config.TreeScan, e *parseTreeEntry, asOnDisk bool, rel string, input []byte) error {
	switch e.NotRead {
	case notReadHidden, notReadNotConfigName, notReadParentNotWalked:
		return nil
	}
	if !asOnDisk {
		opts := config.ScanOptions{Overlay: &config.FileOverlay{Rel: rel, Data: input}}
		scan, err := config.ScanDirTreeFilesWith(configDir, log.New(io.Discard, "", 0), opts)
		if err == nil {
			v, err := treeVerdicts(configDir, scan, opts)
			if err != nil {
				return err
			}
			e.Verdict = v[rel]
			return nil
		}
		if !errors.Is(err, config.ErrOverlay) {
			return err
		}
	}
	v, err := treeVerdicts(configDir, disk, config.ScanOptions{})
	if err != nil {
		return err
	}
	e.Verdict = v[rel]
	return nil
}
