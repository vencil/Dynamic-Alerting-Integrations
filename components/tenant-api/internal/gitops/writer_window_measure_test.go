package gitops

// #1535 item 6 regression tests: what the direct write paths do when
// something outside tenant-api changes the serving repository —
//
//   - in the old window between the single-tenant write reading the tenant
//     file (the base_hash precondition, the merge base) and sampling HEAD
//     as the judged base — injected through afterTenantRead; and in the gap
//     left between the sample and the read (afterBaseSample);
//   - after the judged base is sampled (inside the validator, or at the
//     batch's staging seam) — the compare-and-swap's side, as a control;
//   - with the tenant file in the serving worktree or index differing from
//     HEAD (modified, staged, deleted, untracked, a symlink's target): the
//     write is refused (ErrTenantFileDirty) — git decides what differs,
//     .gitattributes normalisation and symlinks included.
//
// FIXED(#1535-6) marks a scenario whose behaviour before the fix was a defect,
// now asserted fixed. BUG(#3173) marks one still open: it asserts the
// CURRENT behaviour, so the fix turns it red on purpose. None is skipped.
//
// Tenant ids are fixture names (CLAUDE.md #9).

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

const winTenant = "tn-win-a" // the tenant the API writes
const winOther = "tn-win-b"  // an unrelated tenant an outside commit touches

func winBody(tn, threads, cpu string) string {
	s := "tenants:\n  " + tn + ":\n    mysql_threads_running: \"" + threads + "\"\n"
	if cpu != "" {
		s += "    container_cpu: \"" + cpu + "\"\n"
	}
	return s
}

// winRepo: a repository whose HEAD holds both tenants' files.
func winRepo(t *testing.T) string {
	t.Helper()
	dir := initRepoWithDefaults(t)
	for _, tn := range []string{winTenant, winOther} {
		if err := os.WriteFile(filepath.Join(dir, tn+".yaml"), []byte(winBody(tn, "60", "")), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	gitT(t, dir, "add", ".")
	gitT(t, dir, "commit", "-q", "-m", "seed")
	return dir
}

// winOutsideCommit is a commit made in the serving worktree without
// tenant-api: the file written, added and committed by hand.
func winOutsideCommit(t *testing.T, dir, tn, content string) string {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, tn+".yaml"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	gitT(t, dir, "add", "--", tn+".yaml")
	gitT(t, dir, "commit", "-q", "-m", "outside edit of "+tn)
	return gitRevParse(t, dir, "HEAD")
}

func winOutsideEdit(t *testing.T, dir, tn, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, tn+".yaml"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func winServing(t *testing.T, dir, tn string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(dir, tn+".yaml"))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func winSourceHash(s string) string { return cfg.ComputeSourceHash([]byte(s)) }

// winOnce runs fn the first time the returned func is called.
func winOnce(fn func()) func() {
	done := false
	return func() {
		if !done {
			done = true
			fn()
		}
	}
}

// winHookValidator runs fn on its first call (after the judged base was
// sampled), then passes.
type winHookValidator struct{ fn func() }

func (v *winHookValidator) ValidateWrites(context.Context, string, []ProposedFile) error {
	if v.fn != nil {
		v.fn()
		v.fn = nil
	}
	return nil
}

// --- the window: read → HEAD sample ------------------------------------

// W1: WriteIfUnchanged's base_hash matched the file it read; an outside
// commit of that file lands after the read, where HEAD used to be sampled.
//
// FIXED(#1535-6): before the fix — success, committed onto the
// outside commit (the outside change overwritten with 200). The judged base
// is now sampled before the read, so the outside commit moves HEAD off it:
// ErrConflict (409), the outside commit kept, nothing written.
func TestWindow_WriteIfUnchanged_OutsideCommitAfterRead(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	orig := winBody(winTenant, "60", "")
	outside := winBody(winTenant, "60", "75")
	var extHead string
	w.afterTenantRead = winOnce(func() { extHead = winOutsideCommit(t, dir, winTenant, outside) })

	_, err := w.WriteIfUnchanged(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", ""), winSourceHash(orig))
	if extHead == "" {
		t.Fatal("the seam did not fire")
	}
	wantOutsideKept(t, dir, err, extHead, outside, ErrConflict)
}

// W1b: the gap the fix leaves — an outside commit between the sample and
// the read. The read sees the outside bytes: the base_hash no longer
// matches (ErrPrecondition, 409), nothing written.
func TestWindow_WriteIfUnchanged_OutsideCommitBeforeRead(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	outside := winBody(winTenant, "60", "75")
	var extHead string
	w.afterBaseSample = winOnce(func() { extHead = winOutsideCommit(t, dir, winTenant, outside) })

	_, err := w.WriteIfUnchanged(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", ""), winSourceHash(winBody(winTenant, "60", "")))
	wantOutsideKept(t, dir, err, extHead, outside, ErrPrecondition)
}

// W2: WriteMerged read its merge base; an outside commit adds a key to the
// same file after the read.
//
// FIXED(#1535-6): before the fix — success, the merge (computed
// on the pre-commit bytes) committed onto the outside commit and the key it
// added dropped. Now ErrConflict, the outside commit kept, nothing written.
func TestWindow_WriteMerged_OutsideCommitAfterRead(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	outside := winBody(winTenant, "60", "75")
	var extHead string
	w.afterTenantRead = winOnce(func() { extHead = winOutsideCommit(t, dir, winTenant, outside) })

	_, err := w.WriteMerged(context.Background(), winTenant, "alice@example.com", bump90)
	if extHead == "" {
		t.Fatal("the seam did not fire")
	}
	wantOutsideKept(t, dir, err, extHead, outside, ErrConflict)
}

// W2b: an outside commit between the sample and the merge read: the merge
// is computed on the outside bytes, and the CAS refuses (ErrConflict).
func TestWindow_WriteMerged_OutsideCommitBeforeRead(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	outside := winBody(winTenant, "60", "75")
	var extHead string
	w.afterBaseSample = winOnce(func() { extHead = winOutsideCommit(t, dir, winTenant, outside) })

	_, err := w.WriteMerged(context.Background(), winTenant, "alice@example.com", bump90)
	wantOutsideKept(t, dir, err, extHead, outside, ErrConflict)
}

// bump90 is a merge that sets mysql_threads_running to 90, keeping the rest.
func bump90(existing []byte) (string, error) {
	return strings.Replace(string(existing), "\"60\"", "\"90\"", 1), nil
}

// wantOutsideKept: the write was refused with want, HEAD is the outside
// commit with its bytes, and the serving file is the outside commit's.
func wantOutsideKept(t *testing.T, dir string, err error, extHead, outside string, want error) {
	t.Helper()
	if !errors.Is(err, want) {
		t.Fatalf("err = %v, want %v (409, nothing written)", err, want)
	}
	if got := gitRevParse(t, dir, "HEAD"); got != extHead {
		t.Fatalf("HEAD = %s, want the outside commit %s", got, extHead)
	}
	if got := gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"); got != outside {
		t.Fatalf("HEAD's file = %q, want the outside commit's", got)
	}
	if got := winServing(t, dir, winTenant); got != outside {
		t.Fatalf("serving file = %q, want the outside commit's", got)
	}
}

// W3: an UNCOMMITTED outside edit of the tenant file lands after the read
// — after checkTenantFileClean passed. That window is outside the check's
// guarantee (tenant_file_clean.go): the write replaces the serving file, as
// any write does once its precondition passed; the edit is gone from the
// worktree and was never in a commit. Pinned as the documented limit.
func TestWindow_WriteIfUnchanged_UncommittedEditAfterRead(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	orig := winBody(winTenant, "60", "")
	base := gitRevParse(t, dir, "HEAD")
	w.afterTenantRead = winOnce(func() { winOutsideEdit(t, dir, winTenant, winBody(winTenant, "60", "75")) })

	body := winBody(winTenant, "90", "")
	if _, err := w.WriteIfUnchanged(context.Background(), winTenant, "alice@example.com", body, winSourceHash(orig)); err != nil {
		t.Fatalf("WriteIfUnchanged = %v", err)
	}
	if gitRevParse(t, dir, "HEAD~1") != base {
		t.Fatal("not committed onto the base")
	}
	if got := winServing(t, dir, winTenant); got != body {
		t.Fatalf("serving file = %q, want the API body", got)
	}
}

// --- control: after the judged base is sampled ---------------------------

// C1: the same outside commit, landing while the generator judges: the
// check refuses with ErrConflict and nothing is written. Not a defect.
func TestWindow_Control_OutsideCommitWhileJudged(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	orig := winBody(winTenant, "60", "")
	outside := winBody(winTenant, "60", "75")
	var extHead string
	w.SetValidator(&winHookValidator{fn: func() { extHead = winOutsideCommit(t, dir, winTenant, outside) }})

	_, err := w.WriteIfUnchanged(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", ""), winSourceHash(orig))
	wantOutsideKept(t, dir, err, extHead, outside, ErrConflict)
}

// C2 (409 judgement): an outside commit of an UNRELATED tenant's file
// after the judged base was sampled — after the read, or while the
// generator judges: ErrConflict — by design (ADR-036 §4: the verdict is on
// the judged tree, and the tree changed), not a misjudgement of the
// client's base: nothing is written, a retry succeeds. (Before the fix a
// commit after the read became the judged base and the write went
// through.)
func TestWindow_Control_UnrelatedOutsideCommit(t *testing.T) {
	t.Parallel()
	for _, where := range []string{"after read", "while judged"} {
		t.Run(where, func(t *testing.T) {
			t.Parallel()
			dir := winRepo(t)
			w := newTestWriter(dir, dir)
			var extHead string
			commit := func() { extHead = winOutsideCommit(t, dir, winOther, winBody(winOther, "61", "")) }
			if where == "after read" {
				w.afterTenantRead = winOnce(commit)
			} else {
				w.SetValidator(&winHookValidator{fn: commit})
			}
			_, err := w.WriteIfUnchanged(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", ""), winSourceHash(winBody(winTenant, "60", "")))
			if !errors.Is(err, ErrConflict) || gitRevParse(t, dir, "HEAD") != extHead {
				t.Fatalf("err = %v; want ErrConflict with HEAD at the outside commit", err)
			}
			if got := winServing(t, dir, winTenant); got != winBody(winTenant, "60", "") {
				t.Fatalf("serving file = %q, want HEAD's", got)
			}
		})
	}
}

// --- batch: the staging seam (after headBefore and the mirror) -----------

// B1: an outside commit of the op's file after the batch sampled
// headBefore and mirrored the serving tree: the CAS in publishBatch
// refuses with ErrConflict, nothing written. Not a defect.
func TestWindow_Batch_OutsideCommitAfterStaging(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	outside := winBody(winTenant, "60", "75")
	var extHead string
	w.onBatchStaging = func(*stagingTree) { extHead = winOutsideCommit(t, dir, winTenant, outside) }

	_, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant,
		Merge: func([]byte) (string, error) { return winBody(winTenant, "90", ""), nil }}}, "alice@example.com")
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("err = %v, want ErrConflict", err)
	}
	if gitRevParse(t, dir, "HEAD") != extHead || winServing(t, dir, winTenant) != outside {
		t.Fatal("HEAD / serving file is not the outside commit's")
	}
}

// B2: an UNCOMMITTED outside edit of the op's file after the mirror, before
// the op is judged: refused (ErrTenantFileDirty, checked on the serving
// file per op), nothing written, the edit kept.
func TestWindow_Batch_UncommittedEditAfterStaging(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	head := gitRevParse(t, dir, "HEAD")
	edit := winBody(winTenant, "60", "75")
	w.onBatchStaging = func(*stagingTree) { winOutsideEdit(t, dir, winTenant, edit) }
	_, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant,
		Merge: func([]byte) (string, error) { return winBody(winTenant, "90", ""), nil }}}, "alice@example.com")
	wantDirty(t, dir, err, head, edit)
}

// --- the tenant file differs from HEAD: refused (#1535 item 6) ----------
//
// FIXED(#1535-6): before the fix every case below read the uncommitted
// state as the base — a batch op equal to it was a NoOp with nothing
// committed, an unset-only op on an uncommitted removal was a no-op with
// HEAD still holding the key, WriteMerged committed an outside edit under
// the caller's name. Now git compares the tenant file with HEAD first, and
// a difference is ErrTenantFileDirty: nothing written, HEAD unmoved, the
// worktree left as it was.

// wantDirty: err is (or, for a batch, the op's refusal is)
// ErrTenantFileDirty; HEAD is head; the serving file is serving.
func wantDirty(t *testing.T, dir string, err error, head, serving string) {
	t.Helper()
	var rejected *BatchRejectedError
	if errors.As(err, &rejected) {
		if len(rejected.Outcomes) != 1 || !errors.Is(rejected.Outcomes[0].Err, ErrTenantFileDirty) {
			t.Fatalf("batch rejected with %+v, want the op refused with ErrTenantFileDirty", rejected.Outcomes)
		}
	} else if !errors.Is(err, ErrTenantFileDirty) {
		t.Fatalf("err = %v, want ErrTenantFileDirty", err)
	}
	if got := gitRevParse(t, dir, "HEAD"); got != head {
		t.Fatalf("HEAD = %s, want %s unmoved", got, head)
	}
	if got := winServing(t, dir, winTenant); got != serving {
		t.Fatalf("serving file = %q, want %q kept", got, serving)
	}
}

// winWriters runs fn with each direct write path on a fresh repository
// from setup: a batch op (merge), WriteMerged (merge), and Write (body).
func winWriters(t *testing.T, setup func(t *testing.T) string, merge MergeFunc, body string, fn func(t *testing.T, dir string, err error)) {
	t.Helper()
	run := func(name string, write func(w *Writer) error) {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := setup(t)
			fn(t, dir, write(newTestWriter(dir, dir)))
		})
	}
	ctx := context.Background()
	run("batch", func(w *Writer) error {
		_, err := w.WriteBatch(ctx, []BatchOp{{TenantID: winTenant, Merge: merge}}, "alice@example.com")
		return err
	})
	run("merged", func(w *Writer) error {
		_, err := w.WriteMerged(ctx, winTenant, "alice@example.com", merge)
		return err
	})
	run("write", func(w *Writer) error {
		_, err := w.Write(ctx, winTenant, "alice@example.com", body)
		return err
	})
}

// winUnsetCPU removes container_cpu, or reports ErrMergeNoOp when the file
// has none (as the handler's unset-only merge does).
func winUnsetCPU(existing []byte) (string, error) {
	var keep []string
	found := false
	for _, l := range strings.SplitAfter(string(existing), "\n") {
		if strings.Contains(l, "container_cpu:") {
			found = true
			continue
		}
		keep = append(keep, l)
	}
	if !found {
		return "", ErrMergeNoOp
	}
	return strings.Join(keep, ""), nil
}

// winSkipAbsent is a skip-absent op (as the handlers' group members): a
// no-op when existing declares no section for the tenant, else it sets the
// cpu key.
func winSkipAbsent(existing []byte) (string, error) {
	if !strings.Contains(string(existing), "  "+winTenant+":") {
		return "", ErrMergeNoOp
	}
	return strings.Replace(string(existing), "\"60\"\n", "\"60\"\n    container_cpu: \"75\"\n", 1), nil
}

func TestDirty_TenantFileDiffersFromHEAD(t *testing.T) {
	t.Parallel()
	x := winBody(winTenant, "90", "")
	for _, c := range []struct {
		name  string
		setup func(t *testing.T, dir string) // after winRepo; leaves the serving file as want
		want  string
		merge MergeFunc
		body  string
	}{
		// N1/N2/N3: the new content equals an uncommitted edit.
		{"content equals uncommitted edit", func(t *testing.T, dir string) { winOutsideEdit(t, dir, winTenant, x) }, x,
			func([]byte) (string, error) { return x, nil }, x},
		// N1b/N4: an unset-only op on an uncommitted removal of its key.
		{"merge no-op on uncommitted removal", func(t *testing.T, dir string) {
			winOutsideCommit(t, dir, winTenant, winBody(winTenant, "60", "75"))
			winOutsideEdit(t, dir, winTenant, winBody(winTenant, "60", ""))
		}, winBody(winTenant, "60", ""), winUnsetCPU, winBody(winTenant, "60", "")},
		// (a)/(b): WIP unrelated to the op.
		{"unrelated WIP", func(t *testing.T, dir string) { winOutsideEdit(t, dir, winTenant, winBody(winTenant, "77", "")) },
			winBody(winTenant, "77", ""), winUnsetCPU, x},
		// (d): WIP that validate would refuse is refused as dirty, not judged.
		{"invalid WIP", func(t *testing.T, dir string) {
			winOutsideEdit(t, dir, winTenant, winBody(winTenant, "60", "")+"    container_cpu{version=\"V2\"}: \"60\"\n")
		}, winBody(winTenant, "60", "") + "    container_cpu{version=\"V2\"}: \"60\"\n", winUnsetCPU, x},
		// (c) and the new-file case: an untracked tenant file where the
		// write would create one.
		{"untracked tenant file", func(t *testing.T, dir string) {
			gitT(t, dir, "rm", "-q", "--", winTenant+".yaml")
			gitT(t, dir, "commit", "-q", "-m", "seed")
			winOutsideEdit(t, dir, winTenant, "tenants: {}\n")
		}, "tenants: {}\n", winSkipAbsent, x},
		// A staged change (index differs from HEAD, worktree equal to the index).
		{"staged change", func(t *testing.T, dir string) {
			winOutsideEdit(t, dir, winTenant, winBody(winTenant, "88", ""))
			gitT(t, dir, "add", "--", winTenant+".yaml")
		}, winBody(winTenant, "88", ""), func([]byte) (string, error) { return x, nil }, x},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			setup := func(t *testing.T) string {
				dir := winRepo(t)
				c.setup(t, dir)
				return dir
			}
			winWriters(t, setup, c.merge, c.body, func(t *testing.T, dir string, err error) {
				// HEAD is whatever setup left; no write may have added to it.
				wantDirty(t, dir, err, gitRevParse(t, dir, "HEAD"), c.want)
				if gitT(t, dir, "log", "-1", "--format=%ae") == "alice@example.com\n" {
					t.Fatal("a commit was made under the caller's name")
				}
			})
		})
	}
}

// A tracked tenant file deleted from the worktree (not committed) is dirty
// too: Write would otherwise recreate it from a body judged against nothing.
func TestDirty_DeletedTenantFile(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	if err := os.Remove(filepath.Join(dir, winTenant+".yaml")); err != nil {
		t.Fatal(err)
	}
	head := gitRevParse(t, dir, "HEAD")
	_, err := newTestWriter(dir, dir).Write(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", ""))
	if !errors.Is(err, ErrTenantFileDirty) || gitRevParse(t, dir, "HEAD") != head {
		t.Fatalf("err = %v; want ErrTenantFileDirty with HEAD unmoved", err)
	}
	if _, serr := os.Stat(filepath.Join(dir, winTenant+".yaml")); !os.IsNotExist(serr) {
		t.Fatal("the deleted file was recreated")
	}
}

// Control: with the tenant file as HEAD has it, the old rules stand — an
// unset-only op on a file without the key is a no-op, a merge that changes
// it commits.
func TestDirty_CleanFileKeepsTheNoOpAndMergeRules(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	head := gitRevParse(t, dir, "HEAD")
	out, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant, Merge: winUnsetCPU}}, "alice@example.com")
	if err != nil || len(out) != 1 || !out[0].NoOp || gitRevParse(t, dir, "HEAD") != head {
		t.Fatalf("batch = %+v, %v; want a no-op with HEAD unmoved", out, err)
	}
	if _, err := w.WriteMerged(context.Background(), winTenant, "alice@example.com", winUnsetCPU); err != nil || gitRevParse(t, dir, "HEAD") != head {
		t.Fatalf("WriteMerged = %v; want a no-op with HEAD unmoved", err)
	}
	if _, err := w.WriteMerged(context.Background(), winTenant, "alice@example.com", bump90); err != nil {
		t.Fatalf("WriteMerged = %v", err)
	}
	if got := gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"); got != winBody(winTenant, "90", "") || gitRevParse(t, dir, "HEAD~1") != head {
		t.Fatalf("HEAD's file = %q, want the merge committed onto the old HEAD", got)
	}
}

// --- .gitattributes normalisation: git's own comparison -----------------

// winCRLFRepo: `tn-*.yaml text eol=crlf`, the tenant file committed (as an
// LF blob) and checked out by git, so the worktree holds CRLF.
func winCRLFRepo(t *testing.T) (dir, crlf string) {
	t.Helper()
	dir = winRepo(t)
	if err := os.WriteFile(filepath.Join(dir, ".gitattributes"), []byte("tn-*.yaml text eol=crlf\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitT(t, dir, "add", ".gitattributes")
	gitT(t, dir, "commit", "-q", "-m", "seed")
	if err := os.Remove(filepath.Join(dir, winTenant+".yaml")); err != nil {
		t.Fatal(err)
	}
	gitT(t, dir, "checkout", "--", winTenant+".yaml")
	crlf = strings.ReplaceAll(winBody(winTenant, "60", ""), "\n", "\r\n")
	if got := winServing(t, dir, winTenant); got != crlf {
		t.Fatalf("checkout left %q, want CRLF", got)
	}
	if blob := gitT(t, dir, "cat-file", "blob", "HEAD:"+winTenant+".yaml"); strings.Contains(blob, "\r") {
		t.Fatal("the blob is not LF")
	}
	return dir, crlf
}

// S2: a CRLF checkout of an LF blob under `eol=crlf` is what HEAD holds —
// git says so — so it is not dirty, and an identity op / a same-bytes PUT
// is a no-op (a byte compare with the blob called it changed, and the
// staged-bytes check then refused it).
func TestDirty_GitAttributesNormalisedFileIsClean(t *testing.T) {
	t.Parallel()
	t.Run("batch", func(t *testing.T) {
		t.Parallel()
		dir, _ := winCRLFRepo(t)
		head := gitRevParse(t, dir, "HEAD")
		out, err := newTestWriter(dir, dir).WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant,
			Merge: func(e []byte) (string, error) { return string(e), nil }}}, "alice@example.com")
		if err != nil || len(out) != 1 || !out[0].NoOp || gitRevParse(t, dir, "HEAD") != head {
			t.Fatalf("batch = %+v, %v; want a no-op with HEAD unmoved", out, err)
		}
	})
	t.Run("write", func(t *testing.T) {
		t.Parallel()
		dir, crlf := winCRLFRepo(t)
		head := gitRevParse(t, dir, "HEAD")
		if _, err := newTestWriter(dir, dir).Write(context.Background(), winTenant, "alice@example.com", crlf); err != nil || gitRevParse(t, dir, "HEAD") != head {
			t.Fatalf("Write = %v; want a no-op with HEAD unmoved", err)
		}
	})
}

// --- symlinked tenant files ---------------------------------------------

// symRepo: a repository with conf.d/ below its top (the Writer's conf.d),
// a shared/ directory beside it, and _silent_mode bodies (no defaults).
func symRepo(t *testing.T) (repo, conf string) {
	t.Helper()
	repo = t.TempDir()
	conf = filepath.Join(repo, "conf.d")
	for _, d := range []string{conf, filepath.Join(repo, "shared")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	gitT(t, repo, "init", "-q", "-b", "main")
	gitT(t, repo, "config", "user.email", "t@t.com")
	gitT(t, repo, "config", "user.name", "T")
	return repo, conf
}

func symBody(tn, mode string) string {
	return "tenants:\n  " + tn + ":\n    _silent_mode: \"" + mode + "\"\n"
}

func symLink(t *testing.T, target, link string) {
	t.Helper()
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("symlink: %v", err)
	}
}

func symWrite(t *testing.T, p, body string) {
	t.Helper()
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

// A tenant file that is a relative link to a file inside the repository:
// clean → the write goes through; the TARGET modified outside tenant-api →
// refused (git compares the target too), the target kept.
func TestDirty_SymlinkTargetInRepo(t *testing.T) {
	t.Parallel()
	for _, dirtyTarget := range []bool{false, true} {
		repo, conf := symRepo(t)
		const tn = "tn-sym"
		target := filepath.Join(repo, "shared", tn+".yaml")
		symWrite(t, target, symBody(tn, "warning"))
		symLink(t, filepath.Join("..", "shared", tn+".yaml"), filepath.Join(conf, tn+".yaml"))
		gitT(t, repo, "add", "-A")
		gitT(t, repo, "commit", "-q", "-m", "seed")
		if dirtyTarget {
			symWrite(t, target, symBody(tn, "critical"))
		}
		head := gitRevParse(t, repo, "HEAD")
		_, err := newTestWriter(conf, repo).Write(context.Background(), tn, "alice@example.com", symBody(tn, "disabled"))
		if !dirtyTarget {
			if err != nil {
				t.Fatalf("clean link: Write = %v", err)
			}
			continue
		}
		if !errors.Is(err, ErrTenantFileDirty) || gitRevParse(t, repo, "HEAD") != head {
			t.Fatalf("dirty target: err = %v; want ErrTenantFileDirty with HEAD unmoved", err)
		}
		if got, _ := os.ReadFile(target); string(got) != symBody(tn, "critical") {
			t.Fatalf("target = %q, want the outside edit kept", got)
		}
	}
}

// A tenant file that is an ABSOLUTE link to a file outside the repository:
// git cannot compare the target, so it is trusted as before the check
// existed — a modified target does not refuse the write.
func TestDirty_SymlinkTargetOutsideRepoIsTrusted(t *testing.T) {
	t.Parallel()
	repo, conf := symRepo(t)
	const tn = "tn-abs"
	outside := filepath.Join(t.TempDir(), tn+".yaml")
	symWrite(t, outside, symBody(tn, "warning"))
	symLink(t, outside, filepath.Join(conf, tn+".yaml"))
	gitT(t, repo, "add", "-A")
	gitT(t, repo, "commit", "-q", "-m", "seed")
	symWrite(t, outside, symBody(tn, "critical"))
	if _, err := newTestWriter(conf, repo).Write(context.Background(), tn, "alice@example.com", symBody(tn, "disabled")); err != nil {
		t.Fatalf("Write = %v; want accepted (target outside the repository is trusted)", err)
	}
}

// checkTenantFileClean through a DIRECTORY symlink below conf.d, and
// through a chain of more links than the previous hand-written resolver
// followed: the OS resolution reaches the file, and git compares it.
func TestDirty_DirectorySymlinkAndLongChain(t *testing.T) {
	t.Parallel()
	t.Run("directory link", func(t *testing.T) {
		t.Parallel()
		repo, conf := symRepo(t)
		file := filepath.Join(repo, "shared", "tn-dir.yaml")
		symWrite(t, file, symBody("tn-dir", "warning"))
		symLink(t, filepath.Join("..", "shared"), filepath.Join(conf, "team"))
		gitT(t, repo, "add", "-A")
		gitT(t, repo, "commit", "-q", "-m", "seed")
		w := newTestWriter(conf, repo)
		via := filepath.Join(conf, "team", "tn-dir.yaml")
		if err := w.checkTenantFileClean(via); err != nil {
			t.Fatalf("clean: %v", err)
		}
		symWrite(t, file, symBody("tn-dir", "critical"))
		if err := w.checkTenantFileClean(via); !errors.Is(err, ErrTenantFileDirty) {
			t.Fatalf("modified target: err = %v, want ErrTenantFileDirty", err)
		}
	})
	t.Run("chain of 12 links", func(t *testing.T) {
		t.Parallel()
		repo, conf := symRepo(t)
		file := filepath.Join(repo, "shared", "tn-chain.yaml")
		symWrite(t, file, symBody("tn-chain", "warning"))
		prev := filepath.Join("..", "shared", "tn-chain.yaml")
		for i := 0; i < 11; i++ {
			name := "link" + string(rune('a'+i)) + ".yaml"
			symLink(t, prev, filepath.Join(conf, name))
			prev = name
		}
		symLink(t, prev, filepath.Join(conf, "tn-chain.yaml"))
		gitT(t, repo, "add", "-A")
		gitT(t, repo, "commit", "-q", "-m", "seed")
		w := newTestWriter(conf, repo)
		via := filepath.Join(conf, "tn-chain.yaml")
		if err := w.checkTenantFileClean(via); err != nil {
			t.Fatalf("clean: %v", err)
		}
		symWrite(t, file, symBody("tn-chain", "critical"))
		if err := w.checkTenantFileClean(via); !errors.Is(err, ErrTenantFileDirty) {
			t.Fatalf("modified target: err = %v, want ErrTenantFileDirty", err)
		}
	})
}

// An outside edit hidden from `git status` by an index mark: skip-worktree
// or assume-unchanged. Status reports nothing for such a path; the check
// reads the marks (ls-files -v) and refuses — a file git is told not to
// look at cannot be shown to be what HEAD holds.
func TestDirty_IndexMarksHidingAnEdit(t *testing.T) {
	t.Parallel()
	for _, mark := range []string{"--skip-worktree", "--assume-unchanged"} {
		t.Run(mark, func(t *testing.T) {
			t.Parallel()
			dir := winRepo(t)
			edit := winBody(winTenant, "77", "")
			winOutsideEdit(t, dir, winTenant, edit)
			gitT(t, dir, "update-index", mark, "--", winTenant+".yaml")
			if st := gitT(t, dir, "status", "--porcelain", "--", winTenant+".yaml"); st != "" {
				t.Fatalf("setup: status reports %q; the mark should hide the edit", st)
			}
			head := gitRevParse(t, dir, "HEAD")
			_, err := newTestWriter(dir, dir).Write(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", ""))
			wantDirty(t, dir, err, head, edit)
		})
	}
}

// A link in the MIDDLE of the chain retargeted outside tenant-api (not
// committed): tn -> mid -> ../shared/one, and mid now points at
// ../shared/two. Every link on the way is checked, not only the file
// finally reached (which is itself clean here).
func TestDirty_MiddleLinkRetargeted(t *testing.T) {
	t.Parallel()
	repo, conf := symRepo(t)
	const tn = "tn-mid"
	one := filepath.Join(repo, "shared", "one.yaml")
	two := filepath.Join(repo, "shared", "two.yaml")
	symWrite(t, one, symBody(tn, "warning"))
	symWrite(t, two, symBody(tn, "critical"))
	symLink(t, filepath.Join("..", "shared", "one.yaml"), filepath.Join(conf, "mid.yaml"))
	symLink(t, "mid.yaml", filepath.Join(conf, tn+".yaml"))
	gitT(t, repo, "add", "-A")
	gitT(t, repo, "commit", "-q", "-m", "seed")
	w := newTestWriter(conf, repo)
	via := filepath.Join(conf, tn+".yaml")
	if err := w.checkTenantFileClean(via); err != nil {
		t.Fatalf("clean chain: %v", err)
	}
	if err := os.Remove(filepath.Join(conf, "mid.yaml")); err != nil {
		t.Fatal(err)
	}
	symLink(t, filepath.Join("..", "shared", "two.yaml"), filepath.Join(conf, "mid.yaml"))
	if err := w.checkTenantFileClean(via); !errors.Is(err, ErrTenantFileDirty) {
		t.Fatalf("middle link retargeted: err = %v, want ErrTenantFileDirty", err)
	}
}

// A target inside a NESTED repository (its own .git, below the conf
// repository). This pins GIT's behaviour, not a decision of ours: the conf
// repository's status and ls-files -v report nothing for a file inside a
// nested repository (or a submodule's working tree), so it reads as clean
// and is in effect "outside the repository" — trusted, like a target
// outside the top level (cleanCheckPaths). If git ever reported it, this
// test would say so.
func TestDirty_TargetInNestedRepoIsTrusted(t *testing.T) {
	t.Parallel()
	repo, conf := symRepo(t)
	const tn = "tn-nest"
	nested := filepath.Join(repo, "vendor-conf")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	gitT(t, nested, "init", "-q", "-b", "main")
	gitT(t, nested, "config", "user.email", "t@t.com")
	gitT(t, nested, "config", "user.name", "T")
	target := filepath.Join(nested, tn+".yaml")
	symWrite(t, target, symBody(tn, "warning"))
	gitT(t, nested, "add", "-A")
	gitT(t, nested, "commit", "-q", "-m", "nested seed")
	symLink(t, filepath.Join("..", "vendor-conf", tn+".yaml"), filepath.Join(conf, tn+".yaml"))
	gitT(t, repo, "add", "--", "conf.d/"+tn+".yaml")
	gitT(t, repo, "commit", "-q", "-m", "seed")
	symWrite(t, target, symBody(tn, "critical")) // edited, not committed in the nested repo
	w := newTestWriter(conf, repo)
	if err := w.checkTenantFileClean(filepath.Join(conf, tn+".yaml")); err != nil {
		t.Fatalf("target in a nested repository: err = %v, want trusted (nil)", err)
	}
	// The link itself is still the conf repository's: retargeting it is dirty.
	if err := os.Remove(filepath.Join(conf, tn+".yaml")); err != nil {
		t.Fatal(err)
	}
	symLink(t, filepath.Join("..", "shared", tn+".yaml"), filepath.Join(conf, tn+".yaml"))
	if err := w.checkTenantFileClean(filepath.Join(conf, tn+".yaml")); !errors.Is(err, ErrTenantFileDirty) {
		t.Fatalf("link retargeted: err = %v, want ErrTenantFileDirty", err)
	}
}

// An ABSOLUTE symlink to a file inside the repository: resolution restarts
// at the root for it (as the OS does), so the target is found in the
// repository and compared — an edit there refuses the write.
func TestDirty_AbsoluteSymlinkIntoRepo(t *testing.T) {
	t.Parallel()
	repo, conf := symRepo(t)
	const tn = "tn-absin"
	repoReal, err := filepath.EvalSymlinks(repo)
	if err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(repoReal, "shared", tn+".yaml")
	symWrite(t, target, symBody(tn, "warning"))
	symLink(t, target, filepath.Join(conf, tn+".yaml"))
	gitT(t, repo, "add", "-A")
	gitT(t, repo, "commit", "-q", "-m", "seed")
	w := newTestWriter(conf, repo)
	via := filepath.Join(conf, tn+".yaml")
	if err := w.checkTenantFileClean(via); err != nil {
		t.Fatalf("clean: %v", err)
	}
	symWrite(t, target, symBody(tn, "critical"))
	if err := w.checkTenantFileClean(via); !errors.Is(err, ErrTenantFileDirty) {
		t.Fatalf("target edited: err = %v, want ErrTenantFileDirty", err)
	}
}

// A symlink LOOP (a tenant file linking to itself): resolution stops at
// maxSymlinkHops instead of spinning, the link itself is still compared
// (uncommitted → refused), and a committed one passes the check — the
// write then takes the ADR-036 §4 repair path and replaces the link (no
// content is read). Run with a deadline so a check that never returns
// FAILS rather than hangs the package.
func TestDirty_SymlinkLoopIsBounded(t *testing.T) {
	t.Parallel()
	repo, conf := symRepo(t)
	const tn = "tn-loop"
	link := filepath.Join(conf, tn+".yaml")
	symLink(t, tn+".yaml", link)
	w := newTestWriter(conf, repo)
	check := func() error {
		done := make(chan error, 1)
		go func() { done <- w.checkTenantFileClean(link) }()
		select {
		case err := <-done:
			return err
		case <-time.After(2 * time.Second):
			t.Fatal("checkTenantFileClean did not return on a symlink loop")
			return nil
		}
	}
	if err := check(); !errors.Is(err, ErrTenantFileDirty) {
		t.Fatalf("uncommitted loop link: err = %v, want ErrTenantFileDirty", err)
	}
	symWrite(t, filepath.Join(conf, "keep.yaml"), symBody("keep", "warning"))
	gitT(t, repo, "add", "-A")
	gitT(t, repo, "commit", "-q", "-m", "seed")
	if err := check(); err != nil {
		t.Fatalf("committed loop link: err = %v, want clean", err)
	}
	if _, err := w.Write(context.Background(), tn, "alice@example.com", symBody(tn, "disabled")); err != nil {
		t.Fatalf("Write over the committed loop = %v, want the repair path", err)
	}
	if fi, err := os.Lstat(link); err != nil || !fi.Mode().IsRegular() {
		t.Fatalf("after the write: %v, %v; want a regular file", fi, err)
	}
}

// An untracked tenant file that .gitignore hides: status reports it only
// with --ignored ("!!"). Read as the current content it would make a
// same-body PUT a no-op success with HEAD holding no such file (and the
// commit's `git add` could not stage it): it is dirty like any untracked
// file. With the file absent nothing is reported and the write creates it.
func TestDirty_GitignoredTenantFile(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	const tn = "tn-win-ign"
	if err := os.WriteFile(filepath.Join(dir, ".gitignore"), []byte(tn+".yaml\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitT(t, dir, "add", ".gitignore")
	gitT(t, dir, "commit", "-q", "-m", "ignore")
	w := newTestWriter(dir, dir)
	path := filepath.Join(dir, tn+".yaml")
	if err := w.checkTenantFileClean(path); err != nil {
		t.Fatalf("absent ignored path: err = %v, want clean", err)
	}
	body := winBody(tn, "60", "")
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	head := gitRevParse(t, dir, "HEAD")
	_, err := w.Write(context.Background(), tn, "alice@example.com", body)
	if !errors.Is(err, ErrTenantFileDirty) || gitRevParse(t, dir, "HEAD") != head {
		t.Fatalf("same-body PUT over an ignored file: err = %v; want ErrTenantFileDirty with HEAD unmoved", err)
	}
	if got, _ := os.ReadFile(path); string(got) != body {
		t.Fatalf("ignored file changed: %q", got)
	}
}

// --- publishBatch: update-ref fails and HEAD is then unreadable ----------

// refAndHeadFailingGit: the batch's `update-ref` exits 1 (after running it
// when ranFirst), and every `rev-parse HEAD` after it exits 1.
func refAndHeadFailingGit(t *testing.T, ranFirst bool) string {
	t.Helper()
	d := t.TempDir()
	flag := filepath.Join(d, "ref-failed")
	onUpdateRef := "touch '" + flag + "'; exit 1"
	if ranFirst {
		onUpdateRef = "git \"$@\"; touch '" + flag + "'; exit 1"
	}
	stub := filepath.Join(d, "git")
	script := "#!/bin/sh\n" +
		"case \"$*\" in\n" +
		"  *update-ref*'tenant-api: batch'*) " + onUpdateRef + " ;;\n" +
		"  *rev-parse\\ HEAD) [ -e '" + flag + "' ] && exit 1 ;;\n" +
		"esac\n" +
		"exec git \"$@\"\n"
	if err := os.WriteFile(stub, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return stub
}

// P1/P2: whether the ref moved is unknown to the writer (HEAD unreadable);
// it returns a plain error — not ErrConflict, not *BatchCommittedError —
// and drops the publish marker.
//
// BUG(#3173) in P2 (ref moved; found in #1535 item 6, its fix tracked
// in #3173): the batch IS on the branch, yet the error is the "not
// committed" kind (the handler says "nothing was written", see handler
// TestAsyncBatch_RefStateUnknown…), and with the marker gone nothing
// brings the serving file up to the branch: the worktree stays at the old
// bytes behind a HEAD that has the new ones. The assertions pin that
// behaviour as it is today, NOT as it should be: the #3173 fix turns this
// test red on purpose and updates it.
func TestPublishBatch_RefFailsAndHeadUnreadable(t *testing.T) {
	t.Parallel()
	for _, ranFirst := range []bool{false, true} {
		name := "P1 ref unmoved"
		if ranFirst {
			name = "P2 ref moved"
		}
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := winRepo(t)
			w := newTestWriter(dir, dir)
			w.gitBinary = refAndHeadFailingGit(t, ranFirst)
			base := gitRevParse(t, dir, "HEAD")
			body := winBody(winTenant, "90", "")
			out, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant,
				Merge: func([]byte) (string, error) { return body, nil }}}, "alice@example.com")
			var committed *BatchCommittedError
			if err == nil || errors.Is(err, ErrConflict) || errors.As(err, &committed) || out != nil {
				t.Fatalf("out=%v err=%v; current (#3173): nil outcomes, a plain error", out, err)
			}
			if !strings.Contains(err.Error(), "advance the serving branch") {
				t.Fatalf("err = %v", err)
			}
			marker, merr := w.publishMarkerPath()
			if merr != nil {
				t.Fatal(merr)
			}
			if _, serr := os.Stat(marker); !os.IsNotExist(serr) {
				t.Fatalf("publish marker still present (%v)", serr)
			}
			head := gitRevParse(t, dir, "HEAD")
			serving := winServing(t, dir, winTenant)
			if ranFirst {
				// BUG(#3173): published, reported as not written, worktree behind.
				if head == base || gitT(t, dir, "show", "HEAD:"+winTenant+".yaml") != body {
					t.Fatal("the ref did not move")
				}
				if serving != winBody(winTenant, "60", "") {
					t.Fatalf("serving file = %q; current (#3173): still the old bytes", serving)
				}
				if st := gitT(t, dir, "status", "--porcelain", "--", winTenant+".yaml"); strings.TrimSpace(st) == "" {
					t.Fatal("git status clean; current (#3173): the path shows as changed (worktree behind HEAD)")
				}
			} else if head != base || serving != winBody(winTenant, "60", "") {
				t.Fatalf("HEAD moved or serving file changed")
			}
			t.Logf("current behaviour (#3173) %s: err=%q, marker removed, HEAD moved=%v, serving file old=%v",
				name, err, head != base, serving == winBody(winTenant, "60", ""))
		})
	}
}
