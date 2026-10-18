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
//   - with an uncommitted edit of the tenant file in the serving worktree,
//     which the no-op rules and the merge base must not take for the base
//     commit's content (including under .gitattributes normalisation).
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

// W3: an UNCOMMITTED outside edit of the tenant file lands after the read.
// Not a defect of the window: the write replaces the serving file, as any
// write does once its precondition passed; the edit is gone from the
// worktree and was never in a commit. (The worktree is not compared with
// the judged base's blob — see sampleJudgedBase.) Pinned as a data point.
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

// B2: an UNCOMMITTED outside edit of the op's file after the mirror: the
// batch commits from its copy and syncServingPaths overwrites the serving
// file — the edit is lost from the worktree (as W3; data point).
func TestWindow_Batch_UncommittedEditAfterStaging(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	base := gitRevParse(t, dir, "HEAD")
	w.onBatchStaging = func(*stagingTree) { winOutsideEdit(t, dir, winTenant, winBody(winTenant, "60", "75")) }
	body := winBody(winTenant, "90", "")
	if _, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant,
		Merge: func([]byte) (string, error) { return body, nil }}}, "alice@example.com"); err != nil {
		t.Fatalf("batch: %v", err)
	}
	if gitRevParse(t, dir, "HEAD~1") != base || winServing(t, dir, winTenant) != body {
		t.Fatal("want committed on base and the serving file = the batch's")
	}
}

// --- no-op rules vs an uncommitted outside edit -------------------------

// N1: the serving file holds an uncommitted outside edit X; a batch op's
// new content is exactly X.
//
// FIXED(#1535-6): before the fix — the op was NoOp, the batch
// succeeded and nothing was committed (the no-op test compared with the
// serving file only). Now it is unchangedFile's rule, against the batch's
// base: X is committed and the op is not a no-op.
func TestNoOp_Batch_ContentEqualsUncommittedEdit(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	headOld := gitRevParse(t, dir, "HEAD")
	x := winBody(winTenant, "90", "")
	winOutsideEdit(t, dir, winTenant, x)

	out, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant,
		Merge: func([]byte) (string, error) { return x, nil }}}, "alice@example.com")
	if err != nil || len(out) != 1 || out[0].NoOp {
		t.Fatalf("batch = %+v, %v; want success, not a no-op", out, err)
	}
	wantCommitted(t, dir, headOld, x)
}

// wantCommitted: one commit onto headOld whose tenant file is want, and the
// serving file is want with nothing left to commit.
func wantCommitted(t *testing.T, dir, headOld, want string) {
	t.Helper()
	if got := gitRevParse(t, dir, "HEAD~1"); got != headOld {
		t.Fatalf("HEAD~1 = %s, want the old HEAD %s (one commit)", got, headOld)
	}
	if got := gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"); got != want {
		t.Fatalf("HEAD's file = %q, want %q", got, want)
	}
	if got := winServing(t, dir, winTenant); got != want {
		t.Fatalf("serving file = %q, want %q", got, want)
	}
	if st := gitT(t, dir, "status", "--porcelain", "--", winTenant+".yaml"); strings.TrimSpace(st) != "" {
		t.Fatalf("git status shows the path: %q", st)
	}
}

// N1b: the same through ErrMergeNoOp — the serving file has an uncommitted
// removal of the key an unset-only op would remove; the merge (judging the
// serving bytes) says "nothing to remove".
//
// FIXED(#1535-6): before the fix — NoOp, no commit, HEAD still
// carrying the key. Now the merge is run again on HEAD's blob, which does
// carry the key: its removal is committed.
func TestNoOp_Batch_MergeNoOpOnUncommittedEdit(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	winOutsideCommit(t, dir, winTenant, winBody(winTenant, "60", "75")) // HEAD has container_cpu
	w := newTestWriter(dir, dir)
	headOld := gitRevParse(t, dir, "HEAD")
	winOutsideEdit(t, dir, winTenant, winBody(winTenant, "60", "")) // uncommitted removal

	out, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant, Merge: winUnsetCPU}}, "alice@example.com")
	if err != nil || len(out) != 1 || out[0].NoOp {
		t.Fatalf("batch = %+v, %v; want success, not a no-op", out, err)
	}
	wantCommitted(t, dir, headOld, winBody(winTenant, "60", ""))
}

// N1c (control): ErrMergeNoOp with the serving file equal to HEAD stays a
// no-op — nothing committed.
func TestNoOp_Batch_MergeNoOpMatchingHEAD(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	headOld := gitRevParse(t, dir, "HEAD")
	out, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant, Merge: winUnsetCPU}}, "alice@example.com")
	if err != nil || len(out) != 1 || !out[0].NoOp || gitRevParse(t, dir, "HEAD") != headOld {
		t.Fatalf("batch = %+v, %v; want a no-op with HEAD unmoved", out, err)
	}
	if _, err := w.WriteMerged(context.Background(), winTenant, "alice@example.com", winUnsetCPU); err != nil || gitRevParse(t, dir, "HEAD") != headOld {
		t.Fatalf("WriteMerged = %v; want a no-op with HEAD unmoved", err)
	}
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

// N2 (control): Write with the body equal to the uncommitted edit commits
// it — unchangedFile asks HEAD too. Not a defect.
func TestNoOp_Write_ContentEqualsUncommittedEdit(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	headOld := gitRevParse(t, dir, "HEAD")
	x := winBody(winTenant, "90", "")
	winOutsideEdit(t, dir, winTenant, x)
	if _, err := w.Write(context.Background(), winTenant, "alice@example.com", x); err != nil {
		t.Fatalf("Write = %v", err)
	}
	if gitRevParse(t, dir, "HEAD~1") != headOld || gitT(t, dir, "show", "HEAD:"+winTenant+".yaml") != x {
		t.Fatal("want X committed onto the old HEAD")
	}
}

// N3 (control): WriteMerged whose merge yields the uncommitted edit's
// bytes commits them. Not a defect.
func TestNoOp_WriteMerged_ContentEqualsUncommittedEdit(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	headOld := gitRevParse(t, dir, "HEAD")
	x := winBody(winTenant, "90", "")
	winOutsideEdit(t, dir, winTenant, x)
	if _, err := w.WriteMerged(context.Background(), winTenant, "alice@example.com", func(e []byte) (string, error) { return string(e), nil }); err != nil {
		t.Fatalf("WriteMerged = %v", err)
	}
	if gitRevParse(t, dir, "HEAD~1") != headOld || gitT(t, dir, "show", "HEAD:"+winTenant+".yaml") != x {
		t.Fatal("want X committed onto the old HEAD")
	}
}

// N4: WriteMerged's ErrMergeNoOp short-circuit — the same
// uncommitted-removal setup as N1b.
//
// FIXED(#1535-6): before the fix — success with no commit, HEAD
// still carrying the key the caller asked to remove (the short-circuit
// returned before unchangedFile). Now mergeNoOpContent merges the judged
// base's blob again: the key's removal is committed.
func TestNoOp_WriteMerged_MergeNoOpOnUncommittedEdit(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	winOutsideCommit(t, dir, winTenant, winBody(winTenant, "60", "75"))
	w := newTestWriter(dir, dir)
	headOld := gitRevParse(t, dir, "HEAD")
	winOutsideEdit(t, dir, winTenant, winBody(winTenant, "60", ""))

	if _, err := w.WriteMerged(context.Background(), winTenant, "alice@example.com", winUnsetCPU); err != nil {
		t.Fatalf("WriteMerged = %v", err)
	}
	wantCommitted(t, dir, headOld, winBody(winTenant, "60", ""))
}

// N5: WriteMerged on a file with an uncommitted outside edit, the patch
// touching another key: the commit carries the outside edit too, under the
// tenant write's author. It IS judged (the generator saw these bytes), so
// not (c); pinned as a data point about whose change a commit holds.
func TestNoOp_WriteMerged_CommitsUncommittedEditAlong(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	v := &recordingValidator{}
	w.SetValidator(v)
	winOutsideEdit(t, dir, winTenant, winBody(winTenant, "60", "75")) // outside adds container_cpu
	if _, err := w.WriteMerged(context.Background(), winTenant, "alice@example.com", func(e []byte) (string, error) {
		return strings.Replace(string(e), "\"60\"", "\"90\"", 1), nil
	}); err != nil {
		t.Fatalf("WriteMerged = %v", err)
	}
	got := gitT(t, dir, "show", "HEAD:"+winTenant+".yaml")
	if got != winBody(winTenant, "90", "75") || len(v.calls) != 1 || string(v.calls[0].files[0].Content) != got {
		t.Fatalf("HEAD's file = %q, validator calls = %d", got, len(v.calls))
	}
	if a := strings.TrimSpace(gitT(t, dir, "log", "-1", "--format=%ae")); a != "alice@example.com" {
		t.Fatalf("author = %q", a)
	}
}

// --- a no-op merge on a dirty worktree: merged again on the base --------

// winWriters runs fn once with a batch write and once with WriteMerged,
// each on a fresh repository from setup; noop reports the batch outcome's
// NoOp (WriteMerged has none: false).
func winWriters(t *testing.T, setup func(t *testing.T) string, merge MergeFunc, fn func(t *testing.T, dir string, err error, noop bool)) {
	t.Helper()
	t.Run("batch", func(t *testing.T) {
		t.Parallel()
		dir := setup(t)
		w := newTestWriter(dir, dir)
		out, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant, Merge: merge}}, "alice@example.com")
		fn(t, dir, err, err == nil && len(out) == 1 && out[0].NoOp)
	})
	t.Run("merged", func(t *testing.T) {
		t.Parallel()
		dir := setup(t)
		w := newTestWriter(dir, dir)
		_, err := w.WriteMerged(context.Background(), winTenant, "alice@example.com", merge)
		fn(t, dir, err, false)
	})
}

// wantUntouched: nothing committed (HEAD is head) and the serving file
// still holds the uncommitted edit.
func wantUntouched(t *testing.T, dir, head, serving string) {
	t.Helper()
	if got := gitRevParse(t, dir, "HEAD"); got != head {
		t.Fatalf("HEAD = %s, want %s unmoved (nothing committed)", got, head)
	}
	if got := winServing(t, dir, winTenant); got != serving {
		t.Fatalf("serving file = %q, want the uncommitted edit %q kept", got, serving)
	}
}

// S1a: the worktree has an uncommitted edit unrelated to the op (WIP); the
// unset-only op changes nothing on it, nor on HEAD's blob. A no-op: nothing
// committed — the WIP is not committed under the caller's name.
func TestNoOp_DirtyWorktree_MergeNoOpOnBaseToo(t *testing.T) {
	t.Parallel()
	wip := winBody(winTenant, "77", "")
	setup := func(t *testing.T) string {
		dir := winRepo(t)
		winOutsideEdit(t, dir, winTenant, wip)
		return dir
	}
	winWriters(t, setup, winUnsetCPU, func(t *testing.T, dir string, err error, noop bool) {
		if err != nil {
			t.Fatalf("err = %v, want a no-op success", err)
		}
		if strings.HasSuffix(t.Name(), "batch") && !noop {
			t.Fatal("batch outcome is not NoOp")
		}
		wantUntouched(t, dir, gitRevParse(t, dir, "HEAD"), wip)
		if gitT(t, dir, "log", "-1", "--format=%s") != "seed\n" {
			t.Fatal("a commit was made")
		}
	})
}

// S1b: the same WIP, but HEAD's blob carries the key the op removes: the
// write is the merge of HEAD's blob — the key removed, the WIP not in it.
func TestNoOp_DirtyWorktree_MergeOfBaseIsCommitted(t *testing.T) {
	t.Parallel()
	setup := func(t *testing.T) string {
		dir := winRepo(t)
		winOutsideCommit(t, dir, winTenant, winBody(winTenant, "60", "75"))
		winOutsideEdit(t, dir, winTenant, winBody(winTenant, "77", "")) // WIP: threads 77, cpu removed
		return dir
	}
	winWriters(t, setup, winUnsetCPU, func(t *testing.T, dir string, err error, noop bool) {
		if err != nil || noop {
			t.Fatalf("err = %v, noop = %v; want a write", err, noop)
		}
		if got := gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"); got != winBody(winTenant, "60", "") {
			t.Fatalf("HEAD's file = %q, want HEAD's blob merged (threads 60, no cpu) — not the WIP", got)
		}
		if got := winServing(t, dir, winTenant); got != winBody(winTenant, "60", "") {
			t.Fatalf("serving file = %q, want the committed content", got)
		}
	})
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

// S1c: a skip-absent op whose tenant has no file in HEAD; the worktree
// holds an uncommitted file for it without its section. Skipped on both:
// nothing committed, the file not added.
func TestNoOp_DirtyWorktree_SkipAbsentStaysSkipped(t *testing.T) {
	t.Parallel()
	const dirty = "tenants: {}\n"
	setup := func(t *testing.T) string {
		dir := winRepo(t)
		gitT(t, dir, "rm", "-q", "--", winTenant+".yaml")
		gitT(t, dir, "commit", "-q", "-m", "seed")
		winOutsideEdit(t, dir, winTenant, dirty)
		return dir
	}
	winWriters(t, setup, winSkipAbsent, func(t *testing.T, dir string, err error, noop bool) {
		if err != nil {
			t.Fatalf("err = %v, want the op skipped", err)
		}
		if strings.HasSuffix(t.Name(), "batch") && !noop {
			t.Fatal("batch outcome is not NoOp")
		}
		if out := gitT(t, dir, "ls-tree", "--name-only", "HEAD", "--", winTenant+".yaml"); strings.TrimSpace(out) != "" {
			t.Fatal("the tenant file was committed")
		}
		wantUntouched(t, dir, gitRevParse(t, dir, "HEAD"), dirty)
		if gitT(t, dir, "log", "-1", "--format=%s") != "seed\n" {
			t.Fatal("a commit was made")
		}
	})
}

// S1d: the worktree's uncommitted edit carries a key validate refuses (a
// bad version label); the op changes nothing on it nor on HEAD's blob. A
// no-op success, not ErrValidation for bytes this write does not write.
func TestNoOp_DirtyWorktree_InvalidEditIsNotThisWritesRefusal(t *testing.T) {
	t.Parallel()
	dirty := winBody(winTenant, "60", "") + "    container_cpu{version=\"V2\"}: \"60\"\n"
	setup := func(t *testing.T) string {
		dir := winRepo(t)
		winOutsideEdit(t, dir, winTenant, dirty)
		return dir
	}
	winWriters(t, setup, winUnsetCPU, func(t *testing.T, dir string, err error, noop bool) {
		if err != nil {
			t.Fatalf("err = %v, want a no-op success (errors.Is ErrValidation: %v)", err, errors.Is(err, ErrValidation))
		}
		if strings.HasSuffix(t.Name(), "batch") && !noop {
			t.Fatal("batch outcome is not NoOp")
		}
		wantUntouched(t, dir, gitRevParse(t, dir, "HEAD"), dirty)
		if gitT(t, dir, "log", "-1", "--format=%s") != "seed\n" {
			t.Fatal("a commit was made")
		}
	})
}

// --- the no-op rule under .gitattributes normalisation -------------------

// winCRLFRepo: `tn-*.yaml text eol=crlf`, the tenant file committed (as an
// LF blob) and checked out as CRLF, as git's own checkout leaves it.
func winCRLFRepo(t *testing.T) (dir, crlf string) {
	t.Helper()
	dir = winRepo(t)
	if err := os.WriteFile(filepath.Join(dir, ".gitattributes"), []byte("tn-*.yaml text eol=crlf\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitT(t, dir, "add", ".gitattributes")
	gitT(t, dir, "commit", "-q", "-m", "seed")
	crlf = strings.ReplaceAll(winBody(winTenant, "60", ""), "\n", "\r\n")
	winOutsideEdit(t, dir, winTenant, crlf)
	// `git diff` (content, after the clean filter) — `status` also flags
	// the size change against the index entry.
	if d := gitT(t, dir, "diff", "--", winTenant+".yaml"); strings.TrimSpace(d) != "" {
		t.Fatalf("the CRLF worktree file is not what the blob checks out as: %q", d)
	}
	if blob := gitT(t, dir, "cat-file", "blob", "HEAD:"+winTenant+".yaml"); strings.Contains(blob, "\r") {
		t.Fatal("the blob is not LF")
	}
	return dir, crlf
}

// S2: an identity op / a same-bytes PUT on a file git stores normalised is
// a no-op, not a write the staged-bytes check then refuses. (A byte
// compare of the CRLF worktree against the LF blob called it changed.)
func TestNoOp_GitAttributesNormalisedFileIsUnchanged(t *testing.T) {
	t.Parallel()
	t.Run("batch", func(t *testing.T) {
		t.Parallel()
		dir, _ := winCRLFRepo(t)
		head := gitRevParse(t, dir, "HEAD")
		w := newTestWriter(dir, dir)
		out, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant,
			Merge: func(e []byte) (string, error) { return string(e), nil }}}, "alice@example.com")
		if err != nil || len(out) != 1 || !out[0].NoOp || gitRevParse(t, dir, "HEAD") != head {
			t.Fatalf("batch = %+v, %v; want a no-op with HEAD unmoved", out, err)
		}
	})
	t.Run("write", func(t *testing.T) {
		t.Parallel()
		dir, crlf := winCRLFRepo(t)
		head := gitRevParse(t, dir, "HEAD")
		w := newTestWriter(dir, dir)
		if _, err := w.Write(context.Background(), winTenant, "alice@example.com", crlf); err != nil || gitRevParse(t, dir, "HEAD") != head {
			t.Fatalf("Write = %v; want a no-op with HEAD unmoved", err)
		}
	})
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
