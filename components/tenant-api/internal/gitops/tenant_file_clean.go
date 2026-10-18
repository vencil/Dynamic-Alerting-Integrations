package gitops

// #1535 item 6: a direct tenant write reads the tenant file from the serving
// worktree (the base_hash precondition, the merge base, the no-op rule) but
// commits onto the judged base. The two must be the same file, or every
// verdict the write takes is a verdict on bytes no commit holds: an
// uncommitted edit made outside tenant-api was merged over, committed under
// the API caller's name, or answered "nothing to change" while HEAD still
// held something else. Git compares the worktree with HEAD itself — it
// knows what a path stores as (.gitattributes) and what a symlink is —
// rather than tenant-api reimplementing either; a difference refuses the
// write.

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// ErrTenantFileDirty refuses a direct tenant write whose tenant file in the
// serving worktree (or the index) is not what the commit the write lands on
// holds: modified, staged, deleted, untracked, or — for a tenant file that
// is a symlink — the link or its in-repository target changed. Nothing is
// written; whoever made the change outside tenant-api commits or discards
// it first. Its text names the path: for the server log only.
var ErrTenantFileDirty = errors.New("the tenant file has uncommitted changes made outside tenant-api")

// checkTenantFileClean refuses (ErrTenantFileDirty) a write whose tenant
// file at filePath (in the serving conf.d; it need not exist) differs from
// HEAD — the judged base, sampled under the same lock just before
// (sampleJudgedBase). Caller holds w.mu.
//
// THE COMMAND: `git status --porcelain=v1 -z --untracked-files=all
// --ignored=no -- <paths>`. One call answers every case the write cares
// about: HEAD against the index (a staged change, "M "/"A "/"D "), the index
// against the worktree (a modification, a deletion, a symlink retargeted or
// replaced by a file, " M"/" D"/" T"), and an untracked file where the
// write would create one ("??"). Content is compared after the clean
// filter, so a CRLF checkout of an LF blob under `eol=crlf` is clean. (A
// `git diff <base>` plus a separate untracked listing would be two calls
// for the same answer.) Ignored files are not reported: an ignored path
// cannot be staged by the commit anyway.
//
// SYMLINKS: git tracks a link as the link. When the tenant file, or a
// directory on its way below conf.d, is a symlink, that link is checked as
// a path of its own, and the file a read actually reaches — resolved as the
// OS resolves it (filepath.EvalSymlinks, as the read does) — is checked too
// when it lies inside the repository. A target outside the repository is
// nothing git can compare: it is trusted as before this check existed.
func (w *Writer) checkTenantFileClean(filePath string) error {
	top, paths, err := w.cleanCheckPaths(filePath)
	if err != nil {
		return fmt.Errorf("check the tenant file against HEAD: %w", err)
	}
	args := []string{"status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no", "--"}
	for _, p := range paths {
		args = append(args, literalPathspec(p))
	}
	out, err := w.gitOutput(top, args...)
	if err != nil {
		return fmt.Errorf("check the tenant file against HEAD: %w", err)
	}
	if entry, _, _ := strings.Cut(string(out), "\x00"); entry != "" {
		return fmt.Errorf("%w: %s", ErrTenantFileDirty, entry)
	}
	return nil
}

// cleanCheckPaths is the set of paths, relative to the repository top
// level top, that checkTenantFileClean asks git about for filePath (see
// there). conf.d itself may be reached through a symlink (a deployment
// mount); it is resolved as the root, and only links below it count.
func (w *Writer) cleanCheckPaths(filePath string) (top string, paths []string, err error) {
	out, err := w.gitOutput(w.gitDir, "rev-parse", "--show-toplevel")
	if err != nil {
		return "", nil, err
	}
	top = strings.TrimSpace(string(out))
	topReal, err := filepath.EvalSymlinks(top)
	if err != nil {
		return "", nil, err
	}
	confAbs, err := filepath.Abs(w.configDir)
	if err != nil {
		return "", nil, err
	}
	confReal, err := filepath.EvalSymlinks(confAbs)
	if err != nil {
		return "", nil, err
	}
	confRel, ok := inside(topReal, confReal)
	if !ok {
		return "", nil, fmt.Errorf("config dir %s is not inside the repository", confReal)
	}
	abs, err := filepath.Abs(filePath)
	if err != nil {
		return "", nil, err
	}
	below, ok := inside(confAbs, abs)
	if !ok {
		return "", nil, fmt.Errorf("tenant file %s is not inside the config dir", abs)
	}

	// The first symlink on the way from conf.d to the file, if any.
	link := ""
	cur := confReal
	for _, part := range strings.Split(below, string(filepath.Separator)) {
		cur = filepath.Join(cur, part)
		fi, lerr := os.Lstat(cur)
		if lerr != nil {
			break // nothing further exists; git compares the missing path
		}
		if fi.Mode()&os.ModeSymlink != 0 {
			link = cur
			break
		}
	}
	if link == "" {
		return top, []string{filepath.ToSlash(filepath.Join(confRel, below))}, nil
	}
	linkRel, _ := inside(topReal, link)
	paths = []string{filepath.ToSlash(linkRel)}
	// What a read of the file reaches. A file that does not exist (yet) is
	// resolved through its directory.
	real, rerr := filepath.EvalSymlinks(filepath.Join(confReal, below))
	if rerr != nil {
		dir, derr := filepath.EvalSymlinks(filepath.Dir(filepath.Join(confReal, below)))
		if derr != nil {
			return top, paths, nil // nothing reachable to compare beyond the link
		}
		real = filepath.Join(dir, filepath.Base(below))
	}
	if realRel, ok := inside(topReal, real); ok {
		paths = append(paths, filepath.ToSlash(realRel))
	}
	// Outside the repository: git cannot compare it — trusted, as before.
	return top, paths, nil
}

// inside reports p relative to root, and whether p is root or below it.
func inside(root, p string) (string, bool) {
	rel, err := filepath.Rel(root, p)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", false
	}
	return rel, true
}
