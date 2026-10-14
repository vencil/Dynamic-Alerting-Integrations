package gitops

// #2939, ADR-036 §4: a write is judged on the tree AFTER the write, and the
// route generator (`--validate --strict`) refuses a tree with any entry it
// cannot read — so does da-guard (exit 3). A conf.d entry the walker records
// as unreadable (cfg.TreeScan.Unreadable: special_file, stat_error,
// read_error, walk_error) therefore refuses every write. What the refusal
// says depends on who can fix the entry (D3c):
//
//   - A DANGLING SYMLINK (stat_error whose link exists and whose target does
//     not) is content git holds (mode 120000), so the client fixes it in
//     git: TreeUnreadableError, ErrTenantTreeUnreadable (409).
//   - Everything else — a special file, a failed read, a directory that
//     cannot be listed, any other failed stat — is the state of tenant-api's
//     copy on disk, which git cannot hold and a client cannot repair through
//     git: TreeUnreadableError with StoreFault, ErrTenantStoreFault (500).
//     When both kinds are present the store fault wins: the server has to
//     be repaired before any answer about the tree's content means anything,
//     and a 409 would send the client to git for a problem git cannot fix.
//
// The written tenant file itself is judged apart (judgeTree's perTarget): a
// dangling symlink there is replaced by the write — it has no content to
// lose, ADR-036 §4's repair path — while a target that is unreadable in any
// other way (special_file, read_error, another stat failure) has content
// that cannot be read, so replacing it would silently erase it (#2405): the
// write is refused, TargetUnreadableError (TENANT_CONFIG_NOT_LOADABLE).
//
// ⛔ ONE JUDGEMENT. judgeTree is the only place that decides it, for every
// write and preview path: resolveTenantFilePath (Write / WriteIfUnchanged,
// WriteMerged, WritePR / WritePRChecked, DryRunValidate, Diff /
// PreviewCurrent) and applyBatch (WriteBatch, WritePRBatch). What counts as
// unreadable is the walker's own record; the one refinement made here —
// dangling or not, for a stat_error — is danglingLink, which the walker's
// reasons do not carry.

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/vencil/tenant-api/internal/confd"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// ErrTenantTreeUnreadable is what a *TreeUnreadableError without StoreFault
// unwraps to: conf.d holds dangling symlinks (other than the file(s) the
// write replaces), so the tree after the write is one the route generator
// refuses. Nothing was written; the fix is in git. Handlers map it to
// TENANT_TREE_UNREADABLE (409 for a single write, per op on a 422 batch).
var ErrTenantTreeUnreadable = errors.New("conf.d holds entries that cannot be read")

// ErrTenantStoreFault is what a *TreeUnreadableError with StoreFault unwraps
// to: tenant-api's copy of conf.d holds an entry the server cannot read that
// git cannot hold (a special file, a failed read, an unlistable directory,
// a failed stat that is not a dangling link). Nothing was written; the fix
// is on the server or its volume. Handlers map it to TENANT_STORE_FAULT
// (500; a batch fails whole).
var ErrTenantStoreFault = errors.New("tenant-api's copy of conf.d holds entries the server cannot read")

// TreeUnreadableError lists the unreadable entries that refused a write, as
// the walker recorded them (RelKey is conf.d-relative; for walk_error it is
// the directory). StoreFault is set when any of them is not a dangling
// symlink (see the file comment). ⛔ Its Error() names those entries:
// handlers log it and show the names only to a caller whose write permission
// covers every tenant.
type TreeUnreadableError struct {
	Entries    []cfg.UnreadableFile
	StoreFault bool
}

func (e *TreeUnreadableError) Error() string {
	parts := make([]string, len(e.Entries))
	for i, u := range e.Entries {
		parts[i] = u.RelKey + " (" + u.Reason + ")"
	}
	return fmt.Sprintf("%v: %s", e.Unwrap(), strings.Join(parts, ", "))
}

// Unwrap makes errors.Is(err, ErrTenantStoreFault) or
// errors.Is(err, ErrTenantTreeUnreadable) hold.
func (e *TreeUnreadableError) Unwrap() error {
	if e.StoreFault {
		return ErrTenantStoreFault
	}
	return ErrTenantTreeUnreadable
}

// ErrTenantFileUnreadable is what a *TargetUnreadableError for a file that
// is not a special file unwraps to: the tenant file the write would replace
// exists and cannot be read (read_error, or a failed stat that is not a
// dangling link).
var ErrTenantFileUnreadable = errors.New("the tenant file cannot be read")

// TargetUnreadableError refuses a write whose own tenant file is unreadable
// and is not a dangling symlink: it has content nobody can read, so the
// write — which would replace it — is refused for every caller (#2405, D3c).
// It unwraps to confd.ErrNotRegularFile for a special file and to
// ErrTenantFileUnreadable otherwise. Handlers map it to
// TENANT_CONFIG_NOT_LOADABLE (409) with ConfigError.
type TargetUnreadableError struct {
	TenantID string
	RelKey   string
	Reason   string // the walker's: UnreadableSpecialFile, UnreadableReadError or UnreadableStatError
}

func (e *TargetUnreadableError) Error() string {
	return fmt.Sprintf("tenant %s: %v: %s (%s)", e.TenantID, e.Unwrap(), e.RelKey, e.Reason)
}

// Unwrap: confd.ErrNotRegularFile for a special file, else
// ErrTenantFileUnreadable.
func (e *TargetUnreadableError) Unwrap() error {
	if e.Reason == cfg.UnreadableSpecialFile {
		return confd.ErrNotRegularFile
	}
	return ErrTenantFileUnreadable
}

// ConfigError is the tenant list's config_error value for the file:
// not_regular_file or unreadable.
func (e *TargetUnreadableError) ConfigError() confd.FileProblem {
	if e.Reason == cfg.UnreadableSpecialFile {
		return confd.ProblemNotRegularFile
	}
	return confd.ProblemUnreadable
}

// confRelKey is target's path relative to configDir in the walker's form
// (TreeScan RelKey: slash-separated). target is always configDir joined with
// a top-level name (confd.TenantFilePathForWrite), so the relation is lexical.
func confRelKey(configDir, target string) string {
	if rel, err := filepath.Rel(configDir, target); err == nil {
		return filepath.ToSlash(rel)
	}
	return filepath.ToSlash(filepath.Base(target))
}

// danglingLink reports whether the entry rel under absRoot is a symlink whose
// target does not exist — the one stat_error a client can fix in git. A
// stat_error that is anything else (a permission error on the target's
// path, a symlink loop, a link that has since been repaired or broken
// differently) is not, and counts as a store fault.
func danglingLink(absRoot, rel string) bool {
	p := filepath.Join(absRoot, filepath.FromSlash(rel))
	li, err := os.Lstat(p)
	if err != nil || li.Mode()&fs.ModeSymlink == 0 {
		return false
	}
	_, err = os.Stat(p)
	return errors.Is(err, fs.ErrNotExist)
}

// isStoreFault reports whether u, an entry of scan.Unreadable, is a store
// fault (see the file comment): anything but a dangling symlink.
func isStoreFault(absRoot string, u cfg.UnreadableFile) bool {
	return u.Reason != cfg.UnreadableStatError || !danglingLink(absRoot, u.RelKey)
}

// judgeTree is THE judgement (see the file comment). targets maps the
// conf.d-relative key (confRelKey) of every file the write replaces to its
// tenant. perTarget holds a *TargetUnreadableError for each target that is
// unreadable and not a dangling symlink; tree is the *TreeUnreadableError
// for the unreadable entries that are not targets, nil when there is none.
func judgeTree(scan *cfg.TreeScan, targets map[string]string) (perTarget map[string]error, tree error) {
	var left []cfg.UnreadableFile
	fault := false
	for _, u := range scan.Unreadable {
		if id, ok := targets[u.RelKey]; ok {
			if isStoreFault(scan.AbsRoot, u) {
				if perTarget == nil {
					perTarget = map[string]error{}
				}
				perTarget[u.RelKey] = &TargetUnreadableError{TenantID: id, RelKey: u.RelKey, Reason: u.Reason}
			}
			continue // a dangling target is replaced by the write: the repair path
		}
		left = append(left, u)
		fault = fault || isStoreFault(scan.AbsRoot, u)
	}
	if len(left) > 0 {
		tree = &TreeUnreadableError{Entries: left, StoreFault: fault}
	}
	return perTarget, tree
}

// refuseUnreadableTree is judgeTree for a single write of tenantID's file
// at relKey: the store fault first (the server must be repaired before
// anything else), then the target's own refusal, then the git-fixable tree.
func refuseUnreadableTree(scan *cfg.TreeScan, relKey, tenantID string) error {
	perTarget, tree := judgeTree(scan, map[string]string{relKey: tenantID})
	if errors.Is(tree, ErrTenantStoreFault) {
		return tree
	}
	if err := perTarget[relKey]; err != nil {
		return err
	}
	return tree
}

// StoreFaultCounts counts scan's unreadable entries that are store faults,
// by the walker's reason — the tenant_api_store_unreadable_entries gauge
// (Writer.StoreUnreadable). Every entry counts, the tenant files included:
// a FIFO tenant file is a fault of the copy whoever writes it.
func StoreFaultCounts(scan *cfg.TreeScan) map[string]int {
	counts := map[string]int{}
	for _, u := range scan.Unreadable {
		if isStoreFault(scan.AbsRoot, u) {
			counts[u.Reason]++
		}
	}
	return counts
}
