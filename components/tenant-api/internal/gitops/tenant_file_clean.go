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
//
// ⚠️ WHAT IS GUARANTEED, AND WHAT IS NOT. The check refuses a difference
// that exists when the write takes the lock — the state a person or a
// script left behind. It is not a lock on the file: another process can
// still change the file between the check and the read a moment later,
// and that window is outside the guarantee (nothing outside tenant-api
// takes its lock). A commit made from outside in that window moves HEAD
// and is refused by the compare-and-swap; an uncommitted edit in it is
// replaced by the write, as before the check existed.

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// ErrTenantFileDirty refuses a direct tenant write whose tenant file in the
// serving worktree (or the index) is not what the commit the write lands on
// holds: modified, staged, deleted, untracked, marked skip-worktree or
// assume-unchanged, or — for a tenant file reached through symlinks — a
// link on the way or the in-repository target changed. Nothing is written;
// whoever made the change outside tenant-api commits or discards it first.
// Its text names the path: for the server log only.
var ErrTenantFileDirty = errors.New("the tenant file has uncommitted changes made outside tenant-api")

// maxSymlinkHops is how many symlinks resolving a tenant file may follow —
// the limit Linux applies to a path (MAXSYMLINKS, 40). Past it the read
// fails too (ELOOP), so the check need not decide.
const maxSymlinkHops = 40

// checkTenantFileClean refuses (ErrTenantFileDirty) a write whose tenant
// file at filePath (in the serving conf.d; it need not exist) differs from
// HEAD — the judged base, sampled under the same lock just before
// (sampleJudgedBase). Caller holds w.mu. See the file comment for the
// window it does not cover.
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
// WHAT STATUS DOES NOT SEE: an index entry marked skip-worktree or
// assume-unchanged (`git update-index --skip-worktree`) is never compared
// with the worktree, so an edit under it reports nothing. `git ls-files -v`
// shows the marks ("S", or a lower-case tag); a path carrying one is
// refused too — a file git is told not to look at cannot be shown to be
// what HEAD holds.
//
// SYMLINKS: git tracks a link as the link. cleanCheckPaths follows the
// tenant file's path link by link, as the OS does, and every link on the
// way that lies in this repository is checked as a path of its own, with
// the file finally reached. What lies outside this repository is trusted
// (see cleanCheckPaths for what that means).
func (w *Writer) checkTenantFileClean(filePath string) error {
	top, paths, err := w.cleanCheckPaths(filePath)
	if err != nil {
		return fmt.Errorf("check the tenant file against HEAD: %w", err)
	}
	if len(paths) == 0 {
		return nil // nothing in this repository to compare (cleanCheckPaths)
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
	// ls-files runs with GIT_LITERAL_PATHSPECS (literalPathspecs): the
	// paths are given bare, a magic prefix would be taken literally.
	out, err = w.gitOutput(top, append([]string{"ls-files", "-v", "-z", "--"}, paths...)...)
	if err != nil {
		return fmt.Errorf("check the tenant file against HEAD: %w", err)
	}
	for _, entry := range strings.Split(string(out), "\x00") {
		if entry == "" {
			continue
		}
		if tag := entry[0]; tag == 'S' || (tag >= 'a' && tag <= 'z') {
			return fmt.Errorf("%w: %s (marked skip-worktree or assume-unchanged)", ErrTenantFileDirty, entry)
		}
	}
	return nil
}

// cleanCheckPaths is the set of paths, relative to the repository top
// level top, that checkTenantFileClean asks git about for filePath.
//
// The path is resolved component by component as the OS resolves it
// (resolveLinks): each symlink met on the way — the tenant file itself, a
// link it points to, a directory link anywhere on the path — is a path git
// tracks as a link, and the file finally reached is the content a read
// sees. Each of them is checked when it lies IN THIS REPOSITORY.
//
// "In this repository" means: below the conf repository's top level, not
// inside its .git directory, and with that same top level as its own
// (`git rev-parse --show-toplevel` run in its directory) — so not inside
// a nested repository or a submodule's worktree, whose files the conf
// repository's status does not report (it answers nothing for them, which
// would read as clean). Anything else is "outside the repository": git
// cannot compare it with HEAD, and it is trusted as it was before this
// check existed.
//
// A resolution that does not finish — more than maxSymlinkHops links (a
// loop) — checks the links met so far; the read that follows fails on the
// same path, so the write is refused there (fail-closed in the read).
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
	abs, err := filepath.Abs(filePath)
	if err != nil {
		return "", nil, err
	}
	links, final := resolveLinks(abs)
	candidates := links
	if final != "" {
		candidates = append(candidates, final)
	}
	seen := map[string]bool{}
	for _, c := range candidates {
		rel, ok, err := w.inConfRepo(topReal, c)
		if err != nil {
			return "", nil, err
		}
		if ok && !seen[rel] {
			seen[rel] = true
			paths = append(paths, filepath.ToSlash(rel))
		}
	}
	return top, paths, nil
}

// resolveLinks resolves the absolute path p as the OS does, one component
// at a time: links are every symlink met on the way, each as an absolute
// path whose directories are already resolved (so git can be asked about
// it as a link); final is the path reached, "" when resolution did not
// finish (more than maxSymlinkHops links, or a link that cannot be read).
// A component that does not exist ends the walk: the rest is appended as
// it stands (a file the write would create).
func resolveLinks(p string) (links []string, final string) {
	rest := splitPath(p)
	cur := string(filepath.Separator)
	hops := 0
	for len(rest) > 0 {
		part := rest[0]
		rest = rest[1:]
		next := filepath.Join(cur, part) // cur is resolved: ".." is lexical here, as for the OS
		fi, err := os.Lstat(next)
		if err != nil {
			return links, filepath.Join(append([]string{next}, rest...)...)
		}
		if fi.Mode()&os.ModeSymlink == 0 {
			cur = next
			continue
		}
		hops++
		if hops > maxSymlinkHops {
			return links, ""
		}
		links = append(links, next)
		target, err := os.Readlink(next)
		if err != nil {
			return links, ""
		}
		if filepath.IsAbs(target) {
			cur = string(filepath.Separator)
		}
		rest = append(splitPath(target), rest...)
	}
	return links, cur
}

// splitPath is p's components, without empty ones and ".".
func splitPath(p string) []string {
	var parts []string
	for _, s := range strings.Split(filepath.ToSlash(p), "/") {
		if s != "" && s != "." {
			parts = append(parts, s)
		}
	}
	return parts
}

// inConfRepo reports whether the absolute, directory-resolved path p is in
// the conf repository whose resolved top level is topReal (see
// cleanCheckPaths), and p relative to it.
func (w *Writer) inConfRepo(topReal, p string) (string, bool, error) {
	rel, ok := inside(topReal, p)
	if !ok || rel == "." {
		return "", false, nil
	}
	if first, _, _ := strings.Cut(filepath.ToSlash(rel), "/"); first == ".git" {
		return "", false, nil
	}
	// The nearest directory that exists, and its own repository.
	dir := filepath.Dir(p)
	for {
		if fi, err := os.Stat(dir); err == nil && fi.IsDir() {
			break
		}
		if dir == topReal || dir == filepath.Dir(dir) {
			break
		}
		dir = filepath.Dir(dir)
	}
	out, err := w.gitOutput(dir, "rev-parse", "--show-toplevel")
	if err != nil {
		return "", false, fmt.Errorf("locate the repository of %s: %w", rel, err)
	}
	own, err := filepath.EvalSymlinks(strings.TrimSpace(string(out)))
	if err != nil {
		return "", false, err
	}
	if own != topReal {
		return "", false, nil // a nested repository or a submodule's worktree
	}
	return rel, true, nil
}

// inside reports p relative to root, and whether p is root or below it.
func inside(root, p string) (string, bool) {
	rel, err := filepath.Rel(root, p)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", false
	}
	return rel, true
}
