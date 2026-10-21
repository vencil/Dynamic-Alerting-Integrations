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
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"os/exec"
	pathpkg "path"
	"path/filepath"
	"regexp"
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

// tempIndexName matches exactly the names gitCommitOnto gives a temporary
// index (tempIndexPrefix<pid>-<unix nanos>) and git's lock file beside it.
var tempIndexName = regexp.MustCompile(`^` + regexp.QuoteMeta(tempIndexPrefix) + `[0-9]+-[0-9]+(\.lock)?$`)

// PruneCommitIndexes removes the temporary index files (and their .lock)
// a previous process left in the serving worktree's git dir when it was
// killed during a direct write: gitCommitOnto removes its own with a defer,
// which a kill skips. Only regular files whose whole name is one
// gitCommitOnto makes are removed; nothing else in the git dir is touched.
// Call it once at startup, before serving and outside the writer lock, as
// PruneBatchWorktrees: a live write's index would match too.
//
// A separate call from PruneBatchWorktrees rather than a step inside it:
// the two clean different directories (this one the per-worktree git dir,
// the batches the common dir) for different writers, and neither's
// failure should keep the other from running.
func (w *Writer) PruneCommitIndexes() error {
	out, err := w.gitOutput(w.gitDir, "rev-parse", "--absolute-git-dir")
	if err != nil {
		return fmt.Errorf("locate the git dir: %w", err)
	}
	dir := strings.TrimSpace(string(out))
	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("read the git dir: %w", err)
	}
	var errs []error
	for _, e := range entries {
		if !e.Type().IsRegular() || !tempIndexName.MatchString(e.Name()) {
			continue
		}
		p := filepath.Join(dir, e.Name())
		if err := os.Remove(p); err != nil && !errors.Is(err, fs.ErrNotExist) {
			errs = append(errs, err)
			continue
		}
		slog.Warn("gitops: removed a temporary commit index left by an earlier process", "path", p)
	}
	return errors.Join(errs...)
}

// gitCommitOnto commits filePath — already written to the serving tree,
// validated being the bytes the generator judged for it on base — as a
// commit whose parent is base, and advances HEAD onto it only if HEAD is
// still base. committed is false when the file holds what base holds.
// ErrConflict: HEAD moved — nothing was published; the caller puts the
// serving file back (restoreFromHEAD). A ref update that reports an error
// after HEAD reached the commit (git killed by its timeout once the ref was
// written) is a commit: committed is true; one after which neither HEAD
// nor the branch can be read is *WriteOutcomeUnknownError (#3173). The
// publish marker this writes before the ref update goes only when the
// commit is published; on every other return it is the caller's
// (commitFileChange) to remove once the serving file is put back. Any
// other error — a git timeout before or at the ref update included, when
// the branch is read back unmoved — published nothing. Caller holds w.mu.
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
	// move. Every path here is the index's own (top-level-relative,
	// indexPath / indexEntries): w.gitDir can be a subdirectory of the
	// repository (NewWriter roots git at conf.d when no git dir is given).
	idxPath, err := w.indexPath(filePath)
	if err != nil {
		return false, err
	}
	// #3173: the publish marker gets the commit (commitFileChange wrote it
	// without one, before the serving file), before anything outside the
	// temporary index is touched for this commit — as publishBatch writes
	// it before its ref update; an atomic replace, so the marker is always
	// one or the other. It records that the serving file holds the write
	// (markerServingWritten). Its lifetime follows the serving file, not the
	// outcome: it goes here only once the commit is published (the file
	// then IS the branch's); on every other return it stays, and
	// commitFileChange removes it only after the serving file is put back
	// (restoreFromHEAD) — never on an unknown outcome. Whatever is left,
	// RecoverBatchPublish settles on the next write (or startup). When the
	// replace fails, nothing is published and the commit-less marker stays
	// right: the real index is not synced yet.
	if err := w.writePublishMarker(commit, []string{idxPath}, true); err != nil {
		return false, fmt.Errorf("record the publish: %w", err)
	}
	committedEntry, err := w.indexEntries(env, filePath)
	if err != nil {
		return false, fmt.Errorf("read the committed index entry: %w", err)
	}
	priorEntries, err := w.indexEntries(nil, filePath)
	if err != nil {
		return false, fmt.Errorf("read the index entry: %w", err)
	}
	zeroOID := strings.Repeat("0", len(commit))
	if err := w.setIndexEntries(idxPath, zeroOID, committedEntry); err != nil {
		return false, err
	}

	branchRef := w.headBranchRef()
	if w.beforeCASAdvance != nil {
		w.beforeCASAdvance()
	}
	if err := w.gitExec("update-ref", "-m", "tenant-api: tenant/"+tenantID, "HEAD", commit, base); err != nil {
		// An error does not mean the ref did not move: git can be killed
		// (the per-command timeout) after it wrote the ref. Where the branch
		// is now decides (#3173), as in publishBatch: HEAD, or the branch
		// ref itself when HEAD cannot be read.
		now, herr := w.refAfterFailedUpdate(branchRef)
		switch {
		case herr == nil && now == commit:
			// Published. The real index already agrees with the commit (it
			// was synced before the ref update), so it is not undone.
			slog.Warn("gitops: HEAD update reported an error after it took effect",
				"tenant", tenantID, "commit", commit, "error", err)
			w.removePublishMarker()
			return true, nil
		case herr == nil && now == base:
			// The ref did not move: nothing was published — a timeout
			// included (it wraps ErrGitTimeout: not committed, retry).
			w.unsyncIndexEntry(tenantID, filePath, idxPath, zeroOID, committedEntry, priorEntries)
			return false, fmt.Errorf("advance HEAD: %w", err)
		case herr == nil:
			w.unsyncIndexEntry(tenantID, filePath, idxPath, zeroOID, committedEntry, priorEntries)
			slog.Warn("gitops: HEAD moved before the judged write was published",
				"tenant", tenantID, "judged_base", base, "head", now)
			return false, ErrConflict
		default:
			// Neither HEAD nor the branch can be read: whether the commit
			// was published is unknown (#3173), and the caller says so
			// (*WriteOutcomeUnknownError). unsyncIndexEntry still only
			// undoes the sync while HEAD does not hold the written blob (it
			// cannot tell either: then it undoes it), and the caller's
			// restoreFromHEAD fails for the same unreadable HEAD — so the
			// serving file keeps the write and the index entry can be wrong
			// either way. The publish marker stays (commitFileChange keeps
			// it on this outcome whatever the restore did): the next write
			// (or startup) runs RecoverBatchPublish first, which — once HEAD can
			// be read — brings the file and its index entry up to the branch
			// if the commit landed, and puts the file back as HEAD has it
			// if it did not (the index entry too, while it still names this
			// commit's blob); until then every write is refused, naming the
			// marker.
			w.unsyncIndexEntry(tenantID, filePath, idxPath, zeroOID, committedEntry, priorEntries)
			slog.Error("gitops: HEAD unreadable after a failed HEAD update; whether the write was published is unknown — the publish marker is kept",
				"tenant", tenantID, "commit", commit, "path", idxPath, "error", herr)
			return false, &WriteOutcomeUnknownError{Commit: commit,
				Cause: fmt.Errorf("advance HEAD: %w; read the branch back: %w", err, herr)}
		}
	}
	w.removePublishMarker()
	return true, nil
}

// unsyncIndexEntry undoes gitCommitOnto's sync of the real index after a
// ref update that failed: the written path's entry goes back to
// priorEntries — but only while it is still exactly the entry the sync
// wrote and HEAD does not hold that blob. Between the sync and the ref
// update something outside can have changed it: an operator's `git add`
// of the path (theirs to keep), or a `git commit` that took the real
// index, our blob with it, and moved HEAD (putting the old entry back
// would stage a revert of that commit). Then the entry is left as it is.
// Best effort: the re-read and the update-index are two git calls with no
// index.lock held between them, so an outside `git add` landing in that gap
// is overwritten. Caller holds w.mu.
func (w *Writer) unsyncIndexEntry(tenantID, filePath, idxPath, zeroOID string, committedEntry, priorEntries []byte) {
	now, err := w.indexEntries(nil, filePath)
	switch {
	case err != nil:
		slog.Error("gitops: could not read the index entry back after a write that did not commit",
			"tenant", tenantID, "error", err)
		return
	case !bytes.Equal(now, committedEntry):
		slog.Warn("gitops: the index entry changed outside tenant-api during a write that did not commit; left as it is",
			"tenant", tenantID, "path", idxPath)
		return
	case w.headHoldsEntry(idxPath, committedEntry):
		slog.Warn("gitops: HEAD already holds the written blob; the index entry is left as it is",
			"tenant", tenantID, "path", idxPath)
		return
	}
	if err := w.setIndexEntries(idxPath, zeroOID, priorEntries); err != nil {
		slog.Error("gitops: could not put the index entry back after a write that did not commit",
			"tenant", tenantID, "error", err)
	}
}

// headHoldsEntry reports whether HEAD's tree holds, at idxPath, the object
// of entry (one `ls-files -s -z` entry). false when it cannot tell.
func (w *Writer) headHoldsEntry(idxPath string, entry []byte) bool {
	meta, _, ok := bytes.Cut(entry, []byte("\t"))
	fields := strings.Fields(string(meta))
	if !ok || len(fields) < 2 {
		return false
	}
	// HEAD:<path> names a path from the top level, as the index does.
	out, err := w.gitOutput(w.gitDir, "rev-parse", "--verify", "-q", "HEAD:"+idxPath)
	return err == nil && strings.TrimSpace(string(out)) == fields[1]
}

// indexPath is filePath as the index names it: relative to the
// repository's top level (`rev-parse --show-prefix` — w.gitDir's own
// place under it — plus filePath relative to w.gitDir), slash-separated.
// `update-index --index-info` takes these paths and nothing else.
func (w *Writer) indexPath(filePath string) (string, error) {
	rel, err := filepath.Rel(w.gitDir, filePath)
	if err != nil {
		return "", fmt.Errorf("locate the index entry: %w", err)
	}
	prefix, err := w.gitOutput(w.gitDir, "rev-parse", "--show-prefix")
	if err != nil {
		return "", fmt.Errorf("locate the index entry: %w", err)
	}
	return pathpkg.Clean(strings.TrimSpace(string(prefix)) + filepath.ToSlash(rel)), nil
}

// indexEntries is every entry an index holds for filePath (env selects a
// temporary one; nil the real one), `ls-files -s -z` output with
// top-level-relative names (--full-name) — the form setIndexEntries feeds
// back to `update-index --index-info`, whatever directory git ran in.
func (w *Writer) indexEntries(env []string, filePath string) ([]byte, error) {
	return w.gitOutputEnv(w.gitDir, env, "ls-files", "-s", "-z", "--full-name", "--", filePath)
}

// setIndexEntries replaces every entry the real index holds for idxPath
// (top-level-relative, indexPath; all stages) with entries —
// indexEntries output; empty takes the path out of the index — in one
// `update-index --index-info`, under git's own index lock. zeroOID is the
// repository's all-zero object id. Caller holds w.mu.
func (w *Writer) setIndexEntries(idxPath, zeroOID string, entries []byte) error {
	// A mode-0 line removes the path, whatever stages it has.
	var in bytes.Buffer
	in.WriteString("0 " + zeroOID + "\t" + idxPath + "\x00")
	in.Write(entries)
	cmd, ctx, cancel := w.gitCmd("-C", w.gitDir, "update-index", "-z", "--index-info")
	defer cancel()
	cmd.Stdin = &in
	if out, err := cmd.CombinedOutput(); err != nil {
		return w.gitErr(ctx, "update-index", err, out)
	}
	return nil
}
