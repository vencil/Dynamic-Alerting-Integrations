package gitops

// The direct single-file commit bound to the generator's verdict (#2903,
// ADR-036 §4 "驗的和寫的必須是同一份"). The generator judged the tree at
// judgedBase; w.mu keeps every in-process write out, but a commit made from
// outside the process can land at any time. So the commit is made as an
// object whose parent is judgedBase, and HEAD is advanced onto it with a
// compare-and-swap (`update-ref HEAD <new> <judgedBase>`, as publishBatch
// does): if HEAD is no longer judgedBase, nothing is published.
//
// The commit's tree is judgedBase's plus the written path and nothing else
// (#1535): it is built in a temporary index, never from the serving
// repository's own — see gitCommitOnto.

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// tenantCommitMessage is the message of a tenant write's commit. trailer
// (optional) is appended to the body so an audit annotation (e.g. an
// admission-validator --force bypass) is permanently bound to the commit,
// not just an ephemeral log line.
func tenantCommitMessage(tenantID string, trailer ...string) string {
	msg := fmt.Sprintf("tenant/%s: update via portal\n\nTimestamp: %s\nSource: da-portal/tenant-manager",
		tenantID, time.Now().UTC().Format(time.RFC3339))
	if len(trailer) > 0 && trailer[0] != "" {
		msg += "\n\n" + trailer[0]
	}
	return msg
}

// commitIdentity is the author name and the committer of a write by
// authorEmail. The author name defaults to the email's local part; the
// committer is the service account from GIT_COMMITTER_NAME /
// GIT_COMMITTER_EMAIL (set in the K8s Deployment), falling back to the
// author (dev/local mode).
func (w *Writer) commitIdentity(authorEmail string) (authorName, committerName, committerEmail string) {
	authorName = authorEmail
	if at := strings.Index(authorEmail, "@"); at > 0 {
		authorName = authorEmail[:at]
	}
	committerName, committerEmail = w.committerName, w.committerEmail
	if committerName == "" {
		committerName = authorName
	}
	if committerEmail == "" {
		committerEmail = authorEmail
	}
	return authorName, committerName, committerEmail
}

// gitIndexCmd is gitCmd with env added to the command's environment (nil:
// none) — a temporary index's GIT_INDEX_FILE.
func (w *Writer) gitIndexCmd(env []string, args ...string) (*exec.Cmd, context.Context, context.CancelFunc) {
	cmd, ctx, cancel := w.gitCmd(args...)
	cmd.Env = append(cmd.Env, env...)
	return cmd, ctx, cancel
}

// stagePaths stages every path in paths in dir in one `git add` (so the
// index never holds part of them for longer than this command), holds the
// staged blobs to validated (checkStagedAsValidated; on a refusal the paths
// are unstaged), and reports whether anything differs from against (HEAD
// when ""). env is added to every git command it runs: a batch worktree
// passes nil (it stages in its own index), a direct write the temporary
// index's GIT_INDEX_FILE (gitCommitOnto).
func (w *Writer) stagePaths(dir string, env, paths []string, validated map[string][]byte, against string) (changed bool, err error) {
	addCmd, addCtx, addCancel := w.gitIndexCmd(env, append([]string{"-C", dir, "add", "--"}, paths...)...)
	defer addCancel()
	if out, err := addCmd.CombinedOutput(); err != nil {
		return false, w.gitErr(addCtx, "add", err, out)
	}
	if err := w.checkStagedAsValidated(dir, env, validated); err != nil {
		resetCmd, resetCtx, resetCancel := w.gitIndexCmd(env, append([]string{"-C", dir, "reset", "-q", "--"}, paths...)...)
		defer resetCancel()
		if out, uerr := resetCmd.CombinedOutput(); uerr != nil {
			slog.Warn("gitops: could not unstage a refused write", "error", w.gitErr(resetCtx, "reset", uerr, out))
		}
		return false, err
	}
	// Exit 0 means nothing is staged — nothing to commit.
	diffArgs := []string{"-C", dir, "diff", "--cached", "--quiet"}
	if against != "" {
		diffArgs = append(diffArgs, against)
	}
	statusCmd, _, statusCancel := w.gitIndexCmd(env, diffArgs...)
	defer statusCancel()
	return statusCmd.Run() != nil, nil
}

// tempIndexPrefix names a direct write's temporary index file, in the
// serving worktree's git dir (gitCommitOnto).
const tempIndexPrefix = "tenant-api-commit-index-"

// gitCommitOnto commits filePath — already written to the serving tree,
// validated being the bytes the generator judged for it on base — as a
// commit whose parent is base, and advances HEAD onto it only if HEAD is
// still base. committed is false when the file holds what base holds.
// ErrConflict: HEAD moved — nothing was published; the caller puts the
// serving file back (restoreFromHEAD). Caller holds w.mu.
//
// ⛔ THE COMMIT HOLDS THIS PATH AND NOTHING ELSE (#1535). Its tree is
// written from a TEMPORARY index (GIT_INDEX_FILE): base's tree read into
// it, then filePath added from the serving tree — what
// `git commit --only <path>` does. `write-tree` on the serving repository's
// own index writes the WHOLE index, so a file an operator `git add`-ed
// there by hand rode along in a tenant's commit, under the tenant's author,
// never judged by the generator.
//
// THE REAL INDEX (the serving repository's own) is touched for filePath
// only, and only so that it agrees with the published commit — as
// `git commit --only` leaves it: its entry is set to the committed blob
// BEFORE HEAD moves, and put back to exactly what it held when HEAD then
// does not move. Before, so that a held index.lock refuses the write while
// nothing is published (lock contention, as before #1535), instead of
// publishing a commit whose path the real index still holds at the old
// blob — `git status` would show a staged revert of the tenant's write, and
// an operator's next `git commit` would make it. Every other entry of the
// real index — another file staged out of band — is neither read nor
// written: it is still staged after the write, committed or not.
func (w *Writer) gitCommitOnto(filePath, tenantID, authorEmail string, validated []byte, base string, trailer ...string) (committed bool, err error) {
	gitDirOut, err := w.gitOutput(w.gitDir, "rev-parse", "--absolute-git-dir")
	if err != nil {
		return false, fmt.Errorf("locate the git dir: %w", err)
	}
	index := filepath.Join(strings.TrimSpace(string(gitDirOut)),
		fmt.Sprintf("%s%d-%d", tempIndexPrefix, os.Getpid(), time.Now().UnixNano()))
	defer func() {
		_ = os.Remove(index)
		_ = os.Remove(index + ".lock")
	}()
	env := []string{"GIT_INDEX_FILE=" + index}

	readCmd, readCtx, readCancel := w.gitIndexCmd(env, "-C", w.gitDir, "read-tree", base)
	defer readCancel()
	if out, err := readCmd.CombinedOutput(); err != nil {
		return false, w.gitErr(readCtx, "read-tree", err, out)
	}
	changed, err := w.stagePaths(w.gitDir, env, []string{filePath}, map[string][]byte{filePath: validated}, base)
	if err != nil || !changed {
		return false, err
	}
	treeCmd, treeCtx, treeCancel := w.gitIndexCmd(env, "-C", w.gitDir, "write-tree")
	defer treeCancel()
	tree, err := treeCmd.Output()
	if err != nil {
		return false, w.gitErr(treeCtx, "write-tree", err, tree)
	}

	authorName, committerName, committerEmail := w.commitIdentity(authorEmail)
	ctCmd, ctCtx, ctCancel := w.gitCmd("-C", w.gitDir, "commit-tree", strings.TrimSpace(string(tree)),
		"-p", base, "-m", tenantCommitMessage(tenantID, trailer...))
	defer ctCancel()
	ctCmd.Env = append(ctCmd.Env, // keeps gitEnv: no inherited pathspec setting
		"GIT_AUTHOR_NAME="+authorName, "GIT_AUTHOR_EMAIL="+authorEmail,
		"GIT_COMMITTER_NAME="+committerName, "GIT_COMMITTER_EMAIL="+committerEmail)
	out, err := ctCmd.Output()
	if err != nil {
		return false, w.gitErr(ctCtx, "commit-tree", err, out)
	}
	commit := strings.TrimSpace(string(out))

	// The real index: filePath's entry as the commit has it (read from the
	// temporary index), and what it holds now, to put back if HEAD does not
	// move.
	committedEntry, err := w.gitOutputEnv(w.gitDir, env, "ls-files", "-s", "-z", "--", filePath)
	if err != nil {
		return false, fmt.Errorf("read the committed index entry: %w", err)
	}
	priorEntries, err := w.gitOutput(w.gitDir, "ls-files", "-s", "-z", "--", filePath)
	if err != nil {
		return false, fmt.Errorf("read the index entry: %w", err)
	}
	zeroOID := strings.Repeat("0", len(commit))
	if err := w.setIndexEntries(filePath, zeroOID, committedEntry); err != nil {
		return false, err
	}

	if w.beforeCASAdvance != nil {
		w.beforeCASAdvance()
	}
	if err := w.gitExec("update-ref", "-m", "tenant-api: tenant/"+tenantID, "HEAD", commit, base); err != nil {
		if rerr := w.setIndexEntries(filePath, zeroOID, priorEntries); rerr != nil {
			slog.Error("gitops: could not put the index entry back after a write that did not commit",
				"tenant", tenantID, "error", rerr)
		}
		now, herr := w.currentHEAD()
		if herr == nil && now != base {
			slog.Warn("gitops: HEAD moved before the judged write was published",
				"tenant", tenantID, "judged_base", base, "head", now)
			return false, ErrConflict
		}
		return false, fmt.Errorf("advance HEAD: %w", err)
	}
	return true, nil
}

// setIndexEntries replaces every entry the real index holds for filePath
// (all stages) with entries — `ls-files -s -z` output; empty takes
// filePath out of the index — in one `update-index --index-info`, under
// git's own index lock. zeroOID is the repository's all-zero object id.
// Caller holds w.mu.
func (w *Writer) setIndexEntries(filePath, zeroOID string, entries []byte) error {
	rel, err := filepath.Rel(w.gitDir, filePath)
	if err != nil {
		return fmt.Errorf("locate the index entry: %w", err)
	}
	// A mode-0 line removes the path, whatever stages it has.
	var in bytes.Buffer
	in.WriteString("0 " + zeroOID + "\t" + filepath.ToSlash(rel) + "\x00")
	in.Write(entries)
	cmd, ctx, cancel := w.gitCmd("-C", w.gitDir, "update-index", "-z", "--index-info")
	defer cancel()
	cmd.Stdin = &in
	if out, err := cmd.CombinedOutput(); err != nil {
		return w.gitErr(ctx, "update-index", err, out)
	}
	return nil
}
