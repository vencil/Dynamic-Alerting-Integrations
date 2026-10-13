package gitops

// Single-commit batches (#2900, ADR-036 §4 "批次"): one request, one commit,
// all or nothing — in a STAGING worktree.
//
// Every batch gets its own git worktree under <git-common-dir>/batch-worktrees,
// checked out at the commit the batch lands on (the serving HEAD in direct
// mode, the fresh origin base in PR mode). Each op is judged there exactly as
// a single write judges it — the #2078 placement walk and readMergeValidate,
// against the staging tree's conf.d as the ops before it left it — and
// written there. Only when every op passed is the staging tree committed; in
// direct mode the serving branch is then advanced to that commit with a
// compare-and-swap, and the paths the batch changed are brought up to it in
// the serving worktree. A refusal, an error, a cancelled context or a panic
// throws the staging worktree away: the serving worktree and branch are never
// touched, so no reader — locked or not — sees content that was not
// committed. (While a committed batch's files are brought up one by one, a
// reader may see some new and some old — all committed; see publishBatch.)
//
// ⛔ WHY NOT JUDGE IN MEMORY. The verdicts read the tree, not just the op's own
// file: the placement walk reads every conf.d file, and an op may change what
// another op's tenant resolves to (a merge that drops a second tenant's
// section from a shared file frees that id for its own `<id>.yaml`). A real
// tree keeps "judged" and "written" the same bytes by construction.

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/vencil/tenant-api/internal/confd"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
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
// It unwraps to ErrBatchRejected and to every refused op's error, so
// errors.Is / errors.As find an op's sentinel or type (the first matching op
// in request order); a caller that renders per-op results reads Outcomes.
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

// Unwrap makes errors.Is(err, ErrBatchRejected) hold, and every refused op's
// error reachable.
func (e *BatchRejectedError) Unwrap() []error {
	errs := []error{ErrBatchRejected}
	for _, o := range e.Outcomes {
		if o.Err != nil {
			errs = append(errs, o.Err)
		}
	}
	return errs
}

// BatchCommittedError reports a direct-mode batch whose commit IS on the
// serving branch although the write did not finish cleanly: the ref update
// reported an error after it took effect, or the serving worktree could not
// be brought up to the new commit. The batch is in the repository, whole;
// a caller must never report it as not written. It unwraps to ErrConflict:
// like a conflict, the caller should refresh before doing anything else.
type BatchCommittedError struct {
	Commit string
	Cause  error
}

func (e *BatchCommittedError) Error() string {
	return fmt.Sprintf("batch committed as %s, but: %v", e.Commit, e.Cause)
}

// Unwrap makes errors.Is(err, ErrConflict) hold.
func (e *BatchCommittedError) Unwrap() error { return ErrConflict }

// batchWorktreesSubdir is where staging worktrees live, under the repository's
// common git dir: git manages them there, and nothing that reads the serving
// worktree walks into .git.
const batchWorktreesSubdir = "batch-worktrees"

// batchWorktreesDir returns <git-common-dir>/batch-worktrees, absolute.
func (w *Writer) batchWorktreesDir() (string, error) {
	out, err := w.gitOutput(w.gitDir, "rev-parse", "--path-format=absolute", "--git-common-dir")
	if err != nil {
		return "", fmt.Errorf("locate git dir: %w", err)
	}
	return filepath.Join(strings.TrimSpace(string(out)), batchWorktreesSubdir), nil
}

// confRel returns configDir relative to the repository's top level, as git
// paths in a staging worktree name it ("." when conf.d is the top level).
func (w *Writer) confRel() (string, error) {
	out, err := w.gitOutput(w.gitDir, "rev-parse", "--show-toplevel")
	if err != nil {
		return "", fmt.Errorf("locate repository top level: %w", err)
	}
	top, err := filepath.EvalSymlinks(strings.TrimSpace(string(out)))
	if err != nil {
		return "", fmt.Errorf("repository top level: %w", err)
	}
	conf, err := filepath.Abs(w.configDir)
	if err == nil {
		conf, err = filepath.EvalSymlinks(conf)
	}
	if err != nil {
		return "", fmt.Errorf("config dir: %w", err)
	}
	rel, err := filepath.Rel(top, conf)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("config dir is not inside the git repository")
	}
	return rel, nil
}

// stagingTree is one batch's worktree.
type stagingTree struct {
	root      string // the worktree
	configDir string // root/<conf.d relative to the top level>
	rel       string
	prior     *cfg.TreeScan // the staging tree's own walk prior
	changed   map[string][]string
	order     []string // changed paths relative to root, first-written order
	// targets: every op's resolved tenant file, conf.d-relative (confRelKey)
	// — the files the batch replaces, which the #2939 whole-tree verdict
	// (refuseUnreadableTree, in applyBatch) does not count against it.
	targets map[string]bool
	// branchUnneeded (PR mode): the staging branch may be deleted with the
	// worktree — it carries nothing worth keeping.
	branchUnneeded bool
}

// stagingBatchMtime is the modification time a fresh staging checkout gets:
// old enough for the walker's mtime fast path (cfg.TreeScanMtimeGuard), so a
// file no op touched is not re-read on every op's placement walk. A file an
// op writes gets a current mtime and is read again.
var stagingBatchMtime = time.Unix(946684800, 0) // 2000-01-01

// openStaging adds a staging worktree at rev, with only conf.d in its working
// tree (the index holds the whole tree, so the commit keeps every other path).
// branch, when not "", is created at rev and checked out there (PR mode).
// The caller MUST closeStaging it, whatever happens.
//
// mirror (direct mode) fills the staging conf.d from the serving one
// (mirrorServingConf) instead of checking it out: a direct write is judged on
// the tree the exporter reads. PR mode checks out the fresh base, which is
// what its branch carries.
func (w *Writer) openStaging(rev, branch string, mirror bool) (*stagingTree, error) {
	rel, err := w.confRel()
	if err != nil {
		return nil, err
	}
	parent, err := w.batchWorktreesDir()
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return nil, fmt.Errorf("create batch worktree dir: %w", err)
	}
	root := filepath.Join(parent, strconv.FormatInt(time.Now().UnixNano(), 36)+"-"+strconv.Itoa(os.Getpid()))
	args := []string{"worktree", "add", "--no-checkout"}
	if branch != "" {
		args = append(args, "-b", branch)
	} else {
		args = append(args, "--detach")
	}
	if err := w.gitExec(append(args, root, rev)...); err != nil {
		_ = os.RemoveAll(root)
		return nil, fmt.Errorf("add batch worktree: %w", err)
	}
	st := &stagingTree{root: root, configDir: filepath.Join(root, rel), rel: rel,
		changed: map[string][]string{}, targets: map[string]bool{}}
	if err := w.gitIn(root, "reset", "-q"); err != nil {
		w.closeStaging(st)
		return nil, fmt.Errorf("populate batch worktree index: %w", err)
	}
	if err := os.MkdirAll(st.configDir, 0o755); err != nil {
		w.closeStaging(st)
		return nil, fmt.Errorf("create conf.d in the batch worktree: %w", err)
	}
	if mirror {
		if err := w.mirrorServingConf(st); err != nil {
			w.closeStaging(st)
			return nil, fmt.Errorf("copy the serving conf.d into the batch worktree: %w", err)
		}
	} else {
		tracked, err := w.gitOutput(root, "ls-files", "-z", "--", filepath.ToSlash(rel))
		if err != nil {
			w.closeStaging(st)
			return nil, fmt.Errorf("list conf.d in the batch worktree: %w", err)
		}
		if len(tracked) > 0 { // an empty conf.d has nothing to check out
			if err := w.gitIn(root, "checkout", "--", filepath.ToSlash(rel)); err != nil {
				w.closeStaging(st)
				return nil, fmt.Errorf("check out conf.d in the batch worktree: %w", err)
			}
		}
	}
	_ = filepath.WalkDir(st.configDir, func(p string, d fs.DirEntry, err error) error {
		if err == nil && d.Type().IsRegular() {
			_ = os.Chtimes(p, stagingBatchMtime, stagingBatchMtime)
		}
		return nil
	})
	if w.onBatchStaging != nil {
		w.onBatchStaging(st)
	}
	return st, nil
}

// mirrorServingConf copies the SERVING conf.d into the staging conf.d as it
// is on disk — tracked or not, committed or not: the exporter reads the
// serving tree, and a direct-mode write has always been judged on it (a
// gitignored `team/x.yaml` declaring a tenant is a declaration). Nothing of
// it is committed except the batch's own paths, and the copy is thrown away.
// Directories and symlinks are recreated as such; `.git` is skipped.
//
// ⛔ IT JUDGES NOTHING (#2939). An entry it cannot copy — a special file (a
// FIFO named *.yaml: copying would read it), a file whose read fails, a link
// it cannot read — becomes a symlink to the serving entry, so every read of
// it in the staging tree (the placement walk, an op's merge) meets the
// serving entry and fails exactly as it does there, without blocking (the
// walker never opens a special file, cfg.OpenConfFile refuses one). A
// directory below the root that cannot be listed is left out: there is
// nothing to copy, and the batch is refused on the serving walk's
// walk_error (applyBatch judges the serving tree in direct mode). Whether
// the batch may be written with such entries in the tree is decided once,
// by refuseUnreadableTree — not here. Only the root itself failing to list,
// or a failure to create the copy, fails the batch.
func (w *Writer) mirrorServingConf(st *stagingTree) error {
	src, err := filepath.Abs(w.configDir)
	if err != nil {
		return err
	}
	entries, err := os.ReadDir(src)
	if err != nil {
		return err
	}
	return mirrorDir(src, src, st.configDir, entries)
}

// mirrorDir copies the listed entries of the serving directory dir (under
// the serving root src) into the matching staging directory under dstRoot,
// recursively. See mirrorServingConf.
func mirrorDir(src, dir, dstRoot string, entries []fs.DirEntry) error {
	for _, d := range entries {
		p := filepath.Join(dir, d.Name())
		rel, err := filepath.Rel(src, p)
		if err != nil {
			return err
		}
		dst := filepath.Join(dstRoot, rel)
		switch t := d.Type(); {
		case t.IsDir():
			if d.Name() == ".git" {
				continue
			}
			sub, lerr := os.ReadDir(p)
			if lerr != nil {
				continue // not listable: left out (see mirrorServingConf)
			}
			if err := os.MkdirAll(dst, 0o755); err != nil {
				return err
			}
			if err := mirrorDir(src, p, dstRoot, sub); err != nil {
				return err
			}
		case t&os.ModeSymlink != 0:
			target, err := os.Readlink(p)
			if err != nil {
				if err := linkServing(p, dst); err != nil {
					return err
				}
				continue
			}
			// A relative target is resolved from the SERVING link: one that
			// stays inside conf.d is copied as is (it resolves to the staging
			// copy), one that leaves conf.d is made absolute, so the staging
			// link reads the same file the serving one does.
			if !filepath.IsAbs(target) {
				resolved := filepath.Join(filepath.Dir(p), target)
				if r, rerr := filepath.Rel(src, resolved); rerr != nil || r == ".." || strings.HasPrefix(r, ".."+string(filepath.Separator)) {
					target = resolved
				}
			}
			_ = os.Remove(dst)
			if err := os.Symlink(target, dst); err != nil {
				return err
			}
		case t.IsRegular():
			// cfg.ReadConfFile, not os.ReadFile: an entry swapped for a FIFO
			// since the listing is refused instead of blocking (#2939).
			raw, rerr := cfg.ReadConfFile(p)
			fi, ierr := d.Info()
			if rerr != nil || ierr != nil {
				if err := linkServing(p, dst); err != nil {
					return err
				}
				continue
			}
			_ = os.Remove(dst)
			if err := os.WriteFile(dst, raw, fi.Mode().Perm()); err != nil {
				return err
			}
		default: // a special file: never opened (cfg.IsSpecialFile)
			if err := linkServing(p, dst); err != nil {
				return err
			}
		}
	}
	return nil
}

// linkServing makes the staging entry dst a symlink to the serving entry p
// (absolute), for an entry mirrorDir does not copy.
func linkServing(p, dst string) error {
	_ = os.Remove(dst)
	return os.Symlink(p, dst)
}

// closeStaging removes the staging worktree; best effort (startup's
// PruneBatchWorktrees collects what this cannot).
func (w *Writer) closeStaging(st *stagingTree) {
	if err := w.gitExec("worktree", "remove", "--force", st.root); err != nil {
		slog.Warn("gitops: could not remove a batch worktree; removing the directory", "path", st.root, "error", err)
		_ = os.RemoveAll(st.root)
		_ = w.gitExec("worktree", "prune")
	}
}

// gitIn runs a git command in dir (a staging worktree), bounded like gitExec.
func (w *Writer) gitIn(dir string, args ...string) error {
	cmd, ctx, cancel := w.gitCmd(append([]string{"-C", dir}, args...)...)
	defer cancel()
	if out, err := cmd.CombinedOutput(); err != nil {
		return w.gitErr(ctx, args[0], err, out)
	}
	return nil
}

// PruneBatchWorktrees removes every staging worktree a previous process left
// behind (killed mid-batch) and git's records of them. Call it once at
// startup, before serving: nothing else uses those worktrees, and the serving
// tree and branch were never touched by an unfinished batch.
func (w *Writer) PruneBatchWorktrees() error {
	dir, err := w.batchWorktreesDir()
	if err != nil {
		return err
	}
	entries, err := os.ReadDir(dir)
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("read batch worktree dir: %w", err)
	}
	for _, e := range entries {
		p := filepath.Join(dir, e.Name())
		if err := os.RemoveAll(p); err != nil {
			return fmt.Errorf("remove leftover batch worktree %s: %w", e.Name(), err)
		}
		slog.Warn("gitops: removed a batch worktree left by an earlier process", "path", p)
	}
	return w.gitExec("worktree", "prune")
}

// scan is the staging tree's bounded placement walk, with its own prior.
func (w *Writer) stagingScan(st *stagingTree) func() (*cfg.TreeScan, error) {
	return func() (*cfg.TreeScan, error) {
		return w.walkDir(st.configDir, &w.stuckTreeScans, st.prior, func(s *cfg.TreeScan) { st.prior = s })
	}
}

// applyBatchOp judges one op on the staging tree as a single write judges it
// and, when it passes and changes the file, writes it there so the next op
// sees it. Caller holds w.mu.
//
// The file is replaced by rename (writeFileAtomic) in both modes, as every
// tenant write: the directory entry itself is replaced, so a tenant file
// that is a symlink — dangling, or pointing outside conf.d — becomes a
// regular file and nothing is written where it pointed (#2939: ADR-036 §4's
// repair path). PR mode used to write in place, through the link.
func (w *Writer) applyBatchOp(st *stagingTree, op BatchOp) BatchOpOutcome {
	if err := guardTenantID(op.TenantID); err != nil {
		return BatchOpOutcome{Err: err}
	}
	// #1673/#2078: one resolution per op, on the staging tree as the earlier
	// ops left it — the file read by readMergeValidate and the file written
	// below are the same one.
	filePath, err := confd.TenantFilePathForWrite(st.configDir, op.TenantID)
	if err == nil {
		st.targets[confRelKey(st.configDir, filePath)] = true
		err = w.ensureNotDeclaredElsewhereIn(w.stagingScan(st), op.TenantID, filePath)
	}
	if err != nil {
		return BatchOpOutcome{Err: err}
	}
	content, existing, notices, err := w.readMergeValidateIn(st.configDir, op.TenantID, filePath, op.Merge)
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
	if werr := w.writeTenant(filePath, []byte(content), 0644); werr != nil {
		return BatchOpOutcome{Err: fmt.Errorf("write file for %s: %w", op.TenantID, werr)}
	}
	rel, err := filepath.Rel(st.root, filePath)
	if err != nil {
		return BatchOpOutcome{Err: fmt.Errorf("write file for %s: %w", op.TenantID, err)}
	}
	if _, seen := st.changed[rel]; !seen {
		st.order = append(st.order, rel)
	}
	if !containsString(st.changed[rel], op.TenantID) {
		st.changed[rel] = append(st.changed[rel], op.TenantID)
	}
	return BatchOpOutcome{Notices: notices}
}

// applyBatch runs every op on the staging tree — every op is judged, even
// after one is refused, so a caller can report every refusal. ctx is checked
// before each op: a cancelled batch stops there and is not written.
//
// Then the whole tree (#2939, ADR-036 §4): judge is the walk of the tree the
// batch lands on, and an unreadable entry in it that no op of the batch
// replaces (refuseUnreadableTree, st.targets) refuses the batch — every op
// not already refused gets that *TreeUnreadableError as its own refusal, so
// the caller reports it per op like any other. A judge walk that fails is the
// whole batch's error (ErrTenantTreeScan), as a placement walk's is.
//
// judge is the SERVING tree's walk in direct mode (scanTree): the staging
// copy cannot reproduce a directory the serving walk could not list
// (mirrorServingConf leaves it out), and every other entry the batch did not
// replace is the serving tree's own. PR mode passes the staging walk: its
// tree is the fresh base, checked out there.
func (w *Writer) applyBatch(ctx context.Context, st *stagingTree, ops []BatchOp, judge func() (*cfg.TreeScan, error)) ([]BatchOpOutcome, error) {
	outcomes := make([]BatchOpOutcome, len(ops))
	refused := false
	for i, op := range ops {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		outcomes[i] = w.applyBatchOp(st, op)
		refused = refused || outcomes[i].Err != nil
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	scan, err := judge()
	if err != nil {
		return nil, fmt.Errorf("%w: batch: %w", ErrTenantTreeScan, err)
	}
	if treeErr := refuseUnreadableTree(scan, st.targets); treeErr != nil {
		for i := range outcomes {
			if outcomes[i].Err == nil {
				outcomes[i] = BatchOpOutcome{Err: treeErr}
			}
		}
		refused = true
	}
	if refused {
		return nil, &BatchRejectedError{Outcomes: outcomes}
	}
	return outcomes, nil
}

func containsString(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

// commitStaging commits the staging tree's changed paths there. It returns
// the new commit, or "" when nothing differed from the checked-out commit.
// A commit that git reports as failed after it was made still counts: the
// staging HEAD is what is published, and a failure before it moved is not.
func (w *Writer) commitStaging(st *stagingTree, before, authorEmail string) (string, error) {
	if len(st.order) == 0 {
		return "", nil
	}
	committed, err := w.gitCommitPaths(st.root, st.order, batchCommitMessage(st.changed), authorEmail)
	head, herr := w.gitOutput(st.root, "rev-parse", "HEAD")
	if herr != nil {
		if err == nil {
			err = herr
		}
		return "", fmt.Errorf("git commit: %w", err)
	}
	after := strings.TrimSpace(string(head))
	switch {
	case after != before:
		if err != nil {
			slog.Warn("gitops: batch commit reported an error after it was made; using it", "commit", after, "error", err)
		}
		return after, nil
	case err != nil:
		return "", fmt.Errorf("git commit: %w", err)
	case !committed:
		return "", nil
	}
	return after, nil
}

// WriteBatch is the direct-mode single-commit batch (#2900). See the file
// comment. Every op is judged even after one is refused, so the caller can
// report every refusal.
//
// Returns:
//   - outcomes, nil: every op accepted. The files that changed are in one
//     commit on the serving branch and in the serving worktree; a batch in
//     which nothing changes makes no commit.
//   - nil, *BatchRejectedError: at least one op refused (its Outcomes say
//     which and why). Nothing was written.
//   - nil, ctx.Err(): cancelled before the commit was published. Nothing was
//     written.
//   - nil, ErrConflict: the serving branch moved outside this writer while
//     the batch ran. Nothing was written; refresh and retry.
//   - outcomes, *BatchCommittedError: the batch IS committed, but the write
//     did not finish cleanly (see the type).
//   - nil, other err: the batch could not run or commit. Nothing was written.
func (w *Writer) WriteBatch(ctx context.Context, ops []BatchOp, authorEmail string) ([]BatchOpOutcome, error) {
	if len(ops) == 0 {
		return nil, fmt.Errorf("empty batch operations")
	}
	// Load-shedding admission (TRK-320) before w.mu, as every write.
	if err := w.acquireWrite(ctx); err != nil {
		return nil, err
	}
	defer w.releaseWrite()

	// #1723: the serving tree must be on base before the batch is cut from
	// its HEAD.
	if err := w.lockTreeOnBase(); err != nil {
		return nil, err
	}
	defer w.unlockTree()

	headBefore, err := w.currentHEAD()
	if err != nil {
		return nil, fmt.Errorf("read HEAD before batch: %w", err)
	}
	st, err := w.openStaging(headBefore, "", true)
	if err != nil {
		return nil, err
	}
	defer w.closeStaging(st) // also on a panic in an op's merge

	outcomes, err := w.applyBatch(ctx, st, ops, w.scanTree)
	if err != nil {
		return nil, err
	}
	commit, err := w.commitStaging(st, headBefore, authorEmail)
	if err != nil || commit == "" {
		return outcomes, err
	}
	// Last point of no return: cancelled now, nothing is published.
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := w.publishBatch(st, headBefore, commit); err != nil {
		var committed *BatchCommittedError
		if errors.As(err, &committed) {
			return outcomes, err
		}
		return nil, err
	}
	slog.Info("gitops: committed batch", "commit", commit, "files", len(st.order), "author", authorEmail)
	if w.onWrite != nil {
		for _, p := range st.order {
			for _, t := range st.changed[p] {
				w.onWrite(t)
			}
		}
	}
	return outcomes, nil
}

// publishBatch makes a batch's commit the serving state, in this order
// (ADR-036 §4): record the paths in the publish marker, advance the serving
// branch from headBefore to commit with a compare-and-swap, THEN bring those
// paths in the serving worktree up to it — atomically per file, as every
// write does — reset their index entries, and drop the marker. Caller holds
// w.mu.
//
// ⚠️ THE GUARANTEE IS "NEVER UNCOMMITTED", NOT "NEVER MIXED". A reader never
// sees content no commit holds; while the files are brought up one by one it
// can see some of the batch's files new and others still old — every one of
// them committed content. A process killed between the ref update and the
// file sync leaves the worktree behind the branch; the marker makes the next
// writer (or startup, RecoverBatchPublish) reset those paths to the branch.
func (w *Writer) publishBatch(st *stagingTree, headBefore, commit string) error {
	if err := w.writePublishMarker(commit, st.order); err != nil {
		return fmt.Errorf("record the batch publish: %w", err)
	}
	if err := w.gitExec("update-ref", "-m", "tenant-api: batch", "HEAD", commit, headBefore); err != nil {
		now, herr := w.currentHEAD()
		switch {
		case herr == nil && now == commit:
			// The ref moved although git reported an error: it is published.
			slog.Warn("gitops: batch ref update reported an error after it took effect", "commit", commit, "error", err)
		case herr == nil && now != headBefore:
			w.removePublishMarker()
			slog.Warn("gitops: serving branch moved outside tenant-api during a batch — nothing written",
				"expected", headBefore, "actual", now)
			return ErrConflict
		default:
			w.removePublishMarker()
			return fmt.Errorf("advance the serving branch: %w", err)
		}
	}
	// From here on the batch IS committed; a failure leaves the marker, so the
	// next write (or startup) brings the worktree up to the branch.
	if w.beforeBatchSync != nil {
		if err := w.beforeBatchSync(); err != nil {
			return &BatchCommittedError{Commit: commit, Cause: err}
		}
	}
	if err := w.syncServingPaths(commit, st.order); err != nil {
		return &BatchCommittedError{Commit: commit, Cause: err}
	}
	w.removePublishMarker()
	return nil
}

// publishMarkerName is the publish marker, in the common git dir: the commit
// a batch is publishing and its paths (relative to the top level), one per
// line after the commit.
const publishMarkerName = "tenant-api-batch-publish"

func (w *Writer) publishMarkerPath() (string, error) {
	dir, err := w.batchWorktreesDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(filepath.Dir(dir), publishMarkerName), nil
}

func (w *Writer) writePublishMarker(commit string, rels []string) error {
	p, err := w.publishMarkerPath()
	if err != nil {
		return err
	}
	return writeFileAtomic(p, []byte(commit+"\n"+strings.Join(rels, "\n")+"\n"), 0o644)
}

func (w *Writer) removePublishMarker() {
	if p, err := w.publishMarkerPath(); err == nil {
		if err := os.Remove(p); err != nil && !errors.Is(err, fs.ErrNotExist) {
			slog.Warn("gitops: could not remove the batch publish marker", "path", p, "error", err)
		}
	}
}

// syncServingPaths writes each path's content at commit into the serving
// worktree (writeFileAtomic) and resets its index entry to HEAD. A path
// commit does not have — the branch moved on and deleted it after an
// interrupted publish — is removed from the worktree, not treated as an
// error: otherwise the publish marker naming it could never be cleared and
// every later write would fail on it. An empty set is a no-op (a bare
// `git reset --` would reset the whole index).
func (w *Writer) syncServingPaths(commit string, rels []string) error {
	if len(rels) == 0 {
		return nil
	}
	top, err := w.gitOutput(w.gitDir, "rev-parse", "--show-toplevel")
	if err != nil {
		return fmt.Errorf("locate the serving worktree: %w", err)
	}
	serving := make([]string, len(rels))
	for i, rel := range rels {
		serving[i] = filepath.Join(strings.TrimSpace(string(top)), filepath.FromSlash(rel))
		// ⛔ Only a POSITIVE "not in this commit" deletes: ls-tree exits 0
		// with no output. Any failure (a missing object, a timeout, a lock)
		// is an error — `cat-file -e` fails the same way for a corrupt tree
		// as for an absent path, and deleting a committed file on a broken
		// repository cannot be undone. The marker stays for the next try.
		listed, err := w.gitOutput(w.gitDir, "ls-tree", "-z", "--full-tree", commit, "--", filepath.ToSlash(rel))
		if err != nil {
			return fmt.Errorf("look up %s in %s: %w", filepath.ToSlash(rel), commit, err)
		}
		if len(listed) == 0 {
			if err := os.Remove(serving[i]); err != nil && !errors.Is(err, fs.ErrNotExist) {
				return fmt.Errorf("update the serving worktree: %w", err)
			}
			continue
		}
		content, err := w.gitOutput(w.gitDir, "show", commit+":"+filepath.ToSlash(rel))
		if err == nil {
			err = writeFileAtomic(serving[i], content, 0644)
		}
		if err != nil {
			return fmt.Errorf("update the serving worktree: %w", err)
		}
	}
	if err := w.gitExec(append([]string{"reset", "-q", "--"}, serving...)...); err != nil {
		return fmt.Errorf("update the serving index: %w", err)
	}
	return nil
}

// RecoverBatchPublish brings the serving worktree up to its branch after a
// batch publish that a crash (or a failed file sync) interrupted between the
// branch update and the worktree update: the paths the publish marker names
// are reset to HEAD. If the branch never moved, the worktree was never
// touched and only the marker is dropped — but only when git positively
// says so; a failed ancestry check is an error and keeps the marker for the
// next try. It only touches the batch's own
// conf.d paths — never an uncommitted edit of any other file. Called at
// startup and, under the writer lock, before every write's first read of the
// tree (lockTreeRecovered); a no-op when there is no marker.
func (w *Writer) RecoverBatchPublish() error {
	p, err := w.publishMarkerPath()
	if err != nil {
		return nil // no git repository: nothing was ever published
	}
	raw, err := os.ReadFile(p)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return markerBlocksWrites(p, fmt.Errorf("read the batch publish marker: %w", err))
	}
	lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
	commit, rels := lines[0], lines[1:]
	head, err := w.currentHEAD()
	if err != nil {
		return markerBlocksWrites(p, fmt.Errorf("read HEAD to recover a batch publish: %w", err))
	}
	published := head == commit
	if !published {
		// ⛔ Only a POSITIVE answer decides: dropping the marker without a
		// sync on a failed check (a timeout, a lock, a missing object) would
		// lose the only record of which paths are behind the branch.
		if published, err = w.isAncestor(commit, head); err != nil {
			return markerBlocksWrites(p, fmt.Errorf("recover an interrupted batch publish: %w", err))
		}
	}
	if published {
		if err := w.syncServingPaths("HEAD", rels); err != nil {
			return markerBlocksWrites(p, fmt.Errorf("recover an interrupted batch publish: %w", err))
		}
		slog.Warn("gitops: brought the serving worktree up to the branch after an interrupted batch publish",
			"commit", commit, "paths", len(rels))
	}
	w.removePublishMarker()
	return nil
}

// markerBlocksWrites makes a recovery failure actionable: while it fails,
// every write fails with it (lockTreeRecovered), so the error names the
// marker and the way out.
func markerBlocksWrites(marker string, err error) error {
	return fmt.Errorf("%w (writes are blocked until the batch publish marker %s is resolved; "+
		"if its commit is gone, remove the marker after checking the serving worktree against the branch)", err, marker)
}

// isAncestor reports whether commit is an ancestor of head
// (`git merge-base --is-ancestor`). Exit 0 is yes and exit 1 is a positive
// no; any other failure — a deadline kill, a missing object, a lock — is an
// error, never a "no".
func (w *Writer) isAncestor(commit, head string) (bool, error) {
	cmd, ctx, cancel := w.gitCmd("--no-optional-locks", "-C", w.gitDir, "merge-base", "--is-ancestor", commit, head)
	defer cancel()
	out, err := cmd.CombinedOutput()
	if err == nil {
		return true, nil
	}
	var exit *exec.ExitError
	if ctx.Err() == nil && errors.As(err, &exit) && exit.ExitCode() == 1 {
		return false, nil
	}
	return false, w.gitReadErr(ctx, "merge-base", err, out)
}

// batchCommitMessage is the message of a batch's single commit: the
// single-write subject for one tenant, a batch subject naming the count
// otherwise, and every tenant in the body.
func batchCommitMessage(changed map[string][]string) string {
	var tenants []string
	for _, ts := range changed {
		for _, t := range ts {
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
