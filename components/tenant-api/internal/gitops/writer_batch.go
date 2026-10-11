package gitops

// Single-commit batches (#2900, ADR-036 §4 "批次"): one request, one commit,
// all or nothing.
//
// Every op is judged exactly as a single write judges it — tenantFilePath's
// #2078 placement walk and readMergeValidate, under the writer lock, on the
// tree as the ops before it left it — and its result is written to the
// worktree so the next op sees it. Only after the LAST op passed is anything
// committed, in one commit. One refused op, or a failed write or commit, and
// every file the batch touched is put back as it was: nothing of the batch
// reaches the repository.
//
// ⛔ WHY THE OPS ARE WRITTEN BEFORE THE VERDICT AND NOT STAGED IN MEMORY. The
// verdicts read the tree, not just the op's own file: the placement walk
// reads every conf.d file, and an op may change what another op's tenant
// resolves to (a merge that drops a second tenant's section from a shared
// file frees that id for its own `<id>.yaml`; a write through a symlink or a
// hard link changes another name's content). An in-memory overlay would have
// to reproduce all of that for the walker; writing the files and judging the
// real tree keeps "judged" and "written" the same bytes by construction.
//
// ⚠️ The worktree therefore holds the batch's intermediate states while the
// batch runs (under the writer lock, until the commit or the rollback), as it
// held each op's committed state before #2900. A process killed mid-batch
// leaves them uncommitted in the worktree; see restore.

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// BatchOp is one operation of a single-commit batch (WriteBatch).
//
// Merge computes the tenant file's new content from its current content —
// which, when an earlier op of the same batch changed the file, is that op's
// result. It returns ErrMergeNoOp when the op changes nothing; any other error
// refuses the op.
type BatchOp struct {
	TenantID string
	Merge    MergeFunc
}

// BatchOpOutcome is one op's verdict in a single-commit batch.
//
// Err is nil when the op was accepted. NoOp marks an accepted op that changed
// nothing (ErrMergeNoOp, or a merge equal to the file). Notices is
// validate()'s advisory channel for an accepted op (#1231 1b).
type BatchOpOutcome struct {
	Err     error
	NoOp    bool
	Notices []string
}

// ErrBatchRejected is what *BatchRejectedError unwraps to: at least one op of
// a single-commit batch was refused, so nothing of the batch was written.
var ErrBatchRejected = errors.New("batch rejected: an operation was refused, nothing was written")

// BatchRejectedError reports a batch refused because of its ops. Outcomes has
// one entry per op, in request order; every op whose Err is non-nil was
// refused, and the others were judged fine but not written either.
//
// It unwraps to ErrBatchRejected only — an op's error is not the batch's, and
// a caller that wants it reads Outcomes.
type BatchRejectedError struct {
	Outcomes []BatchOpOutcome
}

func (e *BatchRejectedError) Error() string {
	var parts []string
	for i, o := range e.Outcomes {
		if o.Err != nil {
			parts = append(parts, fmt.Sprintf("op %d: %v", i, o.Err))
		}
	}
	return fmt.Sprintf("%v (%s)", ErrBatchRejected, strings.Join(parts, "; "))
}

// Unwrap makes errors.Is(err, ErrBatchRejected) hold.
func (e *BatchRejectedError) Unwrap() error { return ErrBatchRejected }

// touchedFile is a file a batch wrote, with what is needed to put it back.
type touchedFile struct {
	path    string
	existed bool
	// backup is a hard link to the file's ORIGINAL directory entry, taken
	// before the batch first wrote it. The direct path replaces files by
	// rename (writeFileAtomic), so the original inode is never modified and
	// renaming the link back restores it exactly — content, mode, a symlink
	// as a symlink, other hard links to it. "" when no link was taken.
	backup string
	// The fallback when the link could not be made: the original bytes and
	// permissions, or the symlink's target.
	raw           []byte
	perm          os.FileMode
	symlinkTarget string
	tenants       []string
}

// batchTx tracks the files one batch wrote, in first-write order.
type batchTx struct {
	files  []*touchedFile
	byPath map[string]*touchedFile
	seq    int
}

func newBatchTx() *batchTx { return &batchTx{byPath: map[string]*touchedFile{}} }

// touch records path before the batch first writes it.
func (tx *batchTx) touch(path, tenantID string, linkBackup bool) (*touchedFile, error) {
	f, ok := tx.byPath[path]
	if !ok {
		f = &touchedFile{path: path}
		fi, err := os.Lstat(path)
		switch {
		case errors.Is(err, fs.ErrNotExist):
		case err != nil:
			return nil, fmt.Errorf("stat %s before batch write: %w", filepath.Base(path), err)
		default:
			f.existed = true
			if err := tx.backupOf(f, fi, linkBackup); err != nil {
				return nil, err
			}
		}
		tx.byPath[path] = f
		tx.files = append(tx.files, f)
	}
	if !containsString(f.tenants, tenantID) {
		f.tenants = append(f.tenants, tenantID)
	}
	return f, nil
}

// backupOf records how to restore f's existing entry. The backup name starts
// with a dot and does not end in .yaml/.yml, so no conf.d reader takes it for
// a tenant file.
func (tx *batchTx) backupOf(f *touchedFile, fi os.FileInfo, linkBackup bool) error {
	if linkBackup {
		tx.seq++
		name := filepath.Join(filepath.Dir(f.path), "."+filepath.Base(f.path)+".batch-"+
			strconv.FormatInt(time.Now().UnixNano(), 36)+"-"+strconv.Itoa(tx.seq)+".bak")
		if err := os.Link(f.path, name); err == nil {
			f.backup = name
			return nil
		}
	}
	if fi.Mode()&os.ModeSymlink != 0 {
		target, err := os.Readlink(f.path)
		if err != nil {
			return fmt.Errorf("read link %s before batch write: %w", filepath.Base(f.path), err)
		}
		f.symlinkTarget = target
		return nil
	}
	raw, err := os.ReadFile(f.path)
	if err != nil {
		return fmt.Errorf("read %s before batch write: %w", filepath.Base(f.path), err)
	}
	f.raw, f.perm = raw, fi.Mode().Perm()
	return nil
}

// restore puts every touched file back as it was before the batch, last
// written first. Best effort: a failure is logged at ERROR (the worktree then
// differs from HEAD) and the caller returns its primary error regardless.
//
// ⚠️ It runs only on a failure the process lives through. A process killed
// mid-batch leaves the batch's files uncommitted in the worktree (and a
// `.<file>.batch-*.bak` link beside each); nothing of it is in the
// repository, and the next write of such a tenant commits whatever its file
// then holds — as a single write killed between its file write and its commit
// always has.
func (tx *batchTx) restore() {
	for i := len(tx.files) - 1; i >= 0; i-- {
		f := tx.files[i]
		var err error
		switch {
		case !f.existed:
			err = os.Remove(f.path)
			if errors.Is(err, fs.ErrNotExist) {
				err = nil
			}
		case f.backup != "":
			err = os.Rename(f.backup, f.path)
		case f.symlinkTarget != "":
			if err = os.Remove(f.path); err == nil || errors.Is(err, fs.ErrNotExist) {
				err = os.Symlink(f.symlinkTarget, f.path)
			}
		default:
			err = writeFileAtomic(f.path, f.raw, f.perm)
		}
		if err != nil {
			slog.Error("gitops: could not restore a tenant file after a refused or failed batch — the worktree differs from HEAD",
				"path", f.path, "error", err)
		}
	}
}

// dropBackups removes the backup links once the batch is committed.
func (tx *batchTx) dropBackups() {
	for _, f := range tx.files {
		if f.backup == "" {
			continue
		}
		if err := os.Remove(f.backup); err != nil && !errors.Is(err, fs.ErrNotExist) {
			slog.Warn("gitops: could not remove a batch backup link", "path", f.backup, "error", err)
		}
	}
}

func (tx *batchTx) paths() []string {
	out := make([]string, len(tx.files))
	for i, f := range tx.files {
		out[i] = f.path
	}
	return out
}

func containsString(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

// batchWriteFunc writes one op's content: writeFileAtomic on the direct path,
// os.WriteFile on a PR branch (what each path always used).
type batchWriteFunc func(path string, content []byte) error

// applyBatchOp judges one op as a single write does and, when it passes and
// changes the file, writes it so the next op sees it. Caller holds w.mu.
func (w *Writer) applyBatchOp(tx *batchTx, op BatchOp, linkBackup bool, write batchWriteFunc) BatchOpOutcome {
	if err := guardTenantID(op.TenantID); err != nil {
		return BatchOpOutcome{Err: err}
	}
	// #1673: one resolution per op — the file read by readMergeValidate and
	// the file written below must be the same one. Resolved per op, not per
	// batch: the walk must see the earlier ops' writes (see the file comment).
	filePath, err := w.tenantFilePath(op.TenantID)
	if err != nil {
		return BatchOpOutcome{Err: err}
	}
	content, existing, notices, err := w.readMergeValidate(op.TenantID, filePath, op.Merge)
	if errors.Is(err, ErrMergeNoOp) {
		// B2: the merge changes nothing — accepted, nothing written, its
		// notices kept (#1231 F5).
		return BatchOpOutcome{NoOp: true, Notices: notices}
	}
	if err != nil {
		return BatchOpOutcome{Err: err}
	}
	if existing != nil && string(existing) == content {
		return BatchOpOutcome{NoOp: true, Notices: notices}
	}
	if _, err := tx.touch(filePath, op.TenantID, linkBackup); err != nil {
		return BatchOpOutcome{Err: err}
	}
	if err := write(filePath, []byte(content)); err != nil {
		return BatchOpOutcome{Err: fmt.Errorf("write file for %s: %w", op.TenantID, err)}
	}
	return BatchOpOutcome{Notices: notices}
}

// WriteBatch is the direct-mode single-commit batch (#2900). Every op is
// merged, validated and written as WriteMerged would — under the writer lock,
// on the tree as the ops before it left it — and the files are committed in
// ONE commit only when every op passed. Every op is judged even after one is
// refused, so the caller can report every refusal.
//
// Returns:
//   - outcomes, nil: every op accepted. The files that changed are in one
//     commit; a batch in which nothing changes makes no commit.
//   - nil, *BatchRejectedError: at least one op refused (its Outcomes say
//     which and why). Every file the batch wrote is restored; nothing is
//     committed.
//   - nil, err: the batch could not run or could not be committed (admission,
//     the branch guard, git). The files are restored when the commit did not
//     land. ErrConflict is the exception, exactly as for a single write: it
//     is reported after the commit landed on top of a commit made outside
//     this writer, so the batch IS in the repository, whole.
func (w *Writer) WriteBatch(ctx context.Context, ops []BatchOp, authorEmail string) ([]BatchOpOutcome, error) {
	if len(ops) == 0 {
		return nil, fmt.Errorf("empty batch operations")
	}
	// Load-shedding admission (TRK-320) before w.mu, as every write.
	if err := w.acquireWrite(ctx); err != nil {
		return nil, err
	}
	defer w.releaseWrite()

	// #1723: before the first read. It also covers the commit below — the
	// lock is held throughout, so the tree cannot leave base in between; that
	// is why commitFileChange's backstop is not repeated here.
	if err := w.lockTreeOnBase(); err != nil {
		return nil, err
	}
	defer w.unlockTree()

	headBefore, err := w.currentHEAD()
	if err != nil {
		// Proceed without conflict detection in non-git environments.
		slog.Warn("gitops: could not read HEAD before batch write", "error", err)
	}

	tx := newBatchTx()
	outcomes := make([]BatchOpOutcome, len(ops))
	refused := false
	atomicWrite := func(path string, content []byte) error { return writeFileAtomic(path, content, 0644) }
	for i, op := range ops {
		outcomes[i] = w.applyBatchOp(tx, op, true, atomicWrite)
		refused = refused || outcomes[i].Err != nil
	}
	if refused {
		tx.restore()
		return nil, &BatchRejectedError{Outcomes: outcomes}
	}
	if len(tx.files) == 0 {
		return outcomes, nil
	}

	paths := tx.paths()
	committed, err := w.gitCommitPaths(paths, batchCommitMessage(tx.files), authorEmail)
	if err != nil {
		slog.Warn("gitops: batch commit failed", "files", len(paths), "error", err)
		if headAfter, herr := w.currentHEAD(); herr == nil && headBefore != "" && headAfter != headBefore {
			// The commit landed although git reported an error (a deadline
			// kill after the ref moved): the tree matches it, keep it.
			tx.dropBackups()
			return nil, fmt.Errorf("git commit: %w", err)
		}
		tx.restore()
		resetArgs := append([]string{"reset", "-q", "--"}, paths...)
		if rerr := w.gitExec(resetArgs...); rerr != nil {
			slog.Warn("gitops: could not reset the index after a failed batch commit", "error", rerr)
		}
		return nil, fmt.Errorf("git commit: %w", err)
	}
	tx.dropBackups()
	if !committed {
		// Every changed file already matched HEAD (a dirty tree the batch
		// wrote back): the desired state is the repository's, nothing to do.
		slog.Info("gitops: no changes to commit", "commit_tag", "batch")
		return outcomes, nil
	}
	if headBefore != "" {
		parent, err := w.commitParent()
		if err == nil && parent != headBefore {
			slog.Warn("gitops: external commit detected",
				"commit_tag", "batch",
				"expected_parent", headBefore[:8],
				"actual_parent", parent[:8])
			return nil, ErrConflict
		}
	}
	slog.Info("gitops: committed batch", "files", len(paths), "author", authorEmail)
	if w.onWrite != nil {
		for _, f := range tx.files {
			for _, t := range f.tenants {
				w.onWrite(t)
			}
		}
	}
	return outcomes, nil
}

// batchCommitMessage is the message of a batch's single commit: the
// single-write subject for one tenant, a batch subject naming the count
// otherwise, and every tenant in the body.
func batchCommitMessage(files []*touchedFile) string {
	var tenants []string
	for _, f := range files {
		for _, t := range f.tenants {
			if !containsString(tenants, t) {
				tenants = append(tenants, t)
			}
		}
	}
	sort.Strings(tenants)
	subject := "tenant/" + tenants[0] + ": update via portal"
	if len(tenants) > 1 {
		subject = fmt.Sprintf("tenant/batch: update %d tenants via portal", len(tenants))
	}
	return fmt.Sprintf("%s\n\nTenants: %s\nTimestamp: %s\nSource: da-portal/tenant-manager",
		subject, strings.Join(tenants, ", "), time.Now().UTC().Format(time.RFC3339))
}
