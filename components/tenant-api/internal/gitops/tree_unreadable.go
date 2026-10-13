package gitops

// #2939 / ADR-036 §4: a write is judged on the tree AFTER the write, and the
// route generator (`--validate --strict`) refuses a tree with any entry it
// cannot read — so does da-guard (exit 3). A conf.d entry the walker records
// as unreadable (cfg.TreeScan.Unreadable: special_file, stat_error,
// read_error, walk_error) therefore refuses every write, unless the write
// itself replaces that entry: a tenant file that is a dangling symlink or
// cannot be read is replaced by the write, which is ADR-036 §4's repair path.
// (A tenant file that is a special file is not replaced — tenant-api never
// writes over a FIFO; the handlers refuse it first, TENANT_CONFIG_NOT_LOADABLE,
// and readMerge refuses it without blocking.)
//
// ⛔ ONE JUDGEMENT. refuseUnreadableTree is the only place that decides it,
// for every write and preview path: resolveTenantFilePath (Write /
// WriteIfUnchanged, WriteMerged, WritePR / WritePRChecked, DryRunValidate,
// Diff / PreviewCurrent) and applyBatch (WriteBatch, WritePRBatch). What
// counts as unreadable is the walker's own record — no rule of its own here,
// and none in the batch mirror (mirrorServingConf).

import (
	"errors"
	"fmt"
	"path/filepath"
	"strings"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// ErrTenantTreeUnreadable is what *TreeUnreadableError unwraps to: conf.d
// holds an entry the walker cannot read, other than the file(s) the write
// replaces, so the tree after the write is one the route generator refuses.
// Nothing was written. The handlers map it to TENANT_TREE_UNREADABLE (409 for
// a single write, per op on a 422 batch).
var ErrTenantTreeUnreadable = errors.New("conf.d holds entries that cannot be read")

// TreeUnreadableError lists the unreadable entries that refused a write, as
// the walker recorded them (RelKey is conf.d-relative; for walk_error it is
// the directory). ⛔ Its Error() names those entries: handlers log it and
// show the names only to a caller whose write permission covers every tenant.
type TreeUnreadableError struct {
	Entries []cfg.UnreadableFile
}

func (e *TreeUnreadableError) Error() string {
	parts := make([]string, len(e.Entries))
	for i, u := range e.Entries {
		parts[i] = u.RelKey + " (" + u.Reason + ")"
	}
	return fmt.Sprintf("%v: %s", ErrTenantTreeUnreadable, strings.Join(parts, ", "))
}

// Unwrap makes errors.Is(err, ErrTenantTreeUnreadable) hold.
func (e *TreeUnreadableError) Unwrap() error { return ErrTenantTreeUnreadable }

// confRelKey is target's path relative to configDir in the walker's form
// (TreeScan RelKey: slash-separated). target is always configDir joined with
// a top-level name (confd.TenantFilePathForWrite), so the relation is lexical.
func confRelKey(configDir, target string) string {
	if rel, err := filepath.Rel(configDir, target); err == nil {
		return filepath.ToSlash(rel)
	}
	return filepath.ToSlash(filepath.Base(target))
}

// refuseUnreadableTree is THE judgement (see the file comment): scan's
// unreadable entries other than targets — the conf.d-relative keys (confRelKey)
// of the files the write replaces — refuse the write with
// *TreeUnreadableError. nil when there is none.
func refuseUnreadableTree(scan *cfg.TreeScan, targets map[string]bool) error {
	var left []cfg.UnreadableFile
	for _, u := range scan.Unreadable {
		if !targets[u.RelKey] {
			left = append(left, u)
		}
	}
	if len(left) == 0 {
		return nil
	}
	return &TreeUnreadableError{Entries: left}
}
