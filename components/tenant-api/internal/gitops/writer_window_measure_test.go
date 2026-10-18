package gitops

// #1535 item 6 — MEASUREMENT, not a fix. These tests pin what the direct
// write paths do TODAY when something outside tenant-api changes the
// serving repository at the points the issue named:
//
//   - the window between the single-tenant write reading the tenant file
//     (the base_hash precondition, the merge base) and sampling HEAD as the
//     judged base (validateTenantFile) — injected through beforeBaseSample;
//   - after the judged base is sampled (inside the validator, or at the
//     batch's staging seam) — the compare-and-swap's side, as a control;
//   - an uncommitted edit of the tenant file in the serving worktree, which
//     the no-op rules compare against.
//
// A scenario that shows a defect asserts the CURRENT behaviour and is
// marked BUG(#1535-6): the assertion documents the defect, so a fix turns
// the test red on purpose and the fix updates it. None is skipped.
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
// commit of that file lands before HEAD is sampled.
//
// BUG(#1535-6): the precondition passed on bytes that are no longer the
// base, the judged base is the outside commit, and the write commits onto
// it — the outside change is overwritten with 200 instead of a 409.
func TestWindow_WriteIfUnchanged_OutsideCommitBeforeBaseSample(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	orig := winBody(winTenant, "60", "")
	outside := winBody(winTenant, "60", "75")
	var extHead string
	w.beforeBaseSample = winOnce(func() { extHead = winOutsideCommit(t, dir, winTenant, outside) })

	body := winBody(winTenant, "90", "")
	_, err := w.WriteIfUnchanged(context.Background(), winTenant, "alice@example.com", body, winSourceHash(orig))

	// Current behaviour (the defect): success, committed onto the outside commit.
	if err != nil {
		t.Fatalf("WriteIfUnchanged = %v; the measured behaviour was success (BUG #1535-6) — if this is now a 409, the window is closed: update this test", err)
	}
	if extHead == "" {
		t.Fatal("the seam did not fire")
	}
	if got := gitRevParse(t, dir, "HEAD~1"); got != extHead {
		t.Fatalf("HEAD~1 = %s, want the outside commit %s", got, extHead)
	}
	if got := gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"); got != body {
		t.Fatalf("HEAD's file = %q, want the API body", got)
	}
	if strings.Contains(gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"), "container_cpu") {
		t.Fatal("outside change survived")
	}
	t.Logf("MEASURED W1: err=nil, HEAD~1=outside commit, outside key container_cpu lost from HEAD")
}

// W2: WriteMerged read its merge base; an outside commit adds a key to the
// same file before HEAD is sampled.
//
// BUG(#1535-6): the merge was computed on the pre-commit bytes and is
// committed onto the outside commit — the key the outside commit added is
// dropped with no error (a lost update on the path whose purpose is to
// preserve keys the patch did not name).
func TestWindow_WriteMerged_OutsideCommitBeforeBaseSample(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	outside := winBody(winTenant, "60", "75")
	var extHead string
	w.beforeBaseSample = winOnce(func() { extHead = winOutsideCommit(t, dir, winTenant, outside) })

	var mergeBase string
	merged := winBody(winTenant, "90", "")
	_, err := w.WriteMerged(context.Background(), winTenant, "alice@example.com", func(existing []byte) (string, error) {
		mergeBase = string(existing)
		return strings.Replace(string(existing), "\"60\"", "\"90\"", 1), nil
	})
	if err != nil {
		t.Fatalf("WriteMerged = %v; the measured behaviour was success (BUG #1535-6)", err)
	}
	if mergeBase != winBody(winTenant, "60", "") {
		t.Fatalf("merge base = %q", mergeBase)
	}
	if got := gitRevParse(t, dir, "HEAD~1"); got != extHead {
		t.Fatalf("HEAD~1 = %s, want the outside commit %s", got, extHead)
	}
	if got := gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"); got != merged {
		t.Fatalf("HEAD's file = %q, want %q (outside key dropped)", got, merged)
	}
	t.Logf("MEASURED W2: err=nil, HEAD~1=outside commit, HEAD file=%q (outside container_cpu dropped)", merged)
}

// W3: an UNCOMMITTED outside edit of the tenant file lands in the window.
// Not a window-specific defect: the write replaces the serving file, as
// any write does once its precondition passed; the edit is gone from the
// worktree and was never in a commit. Pinned as a data point.
func TestWindow_WriteIfUnchanged_UncommittedEditBeforeBaseSample(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	orig := winBody(winTenant, "60", "")
	base := gitRevParse(t, dir, "HEAD")
	w.beforeBaseSample = winOnce(func() { winOutsideEdit(t, dir, winTenant, winBody(winTenant, "60", "75")) })

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
	t.Logf("MEASURED W3: err=nil, committed on base, uncommitted outside edit overwritten in the worktree")
}

// --- control: after the judged base is sampled ---------------------------

// C1: the same outside commit, landing after HEAD was sampled (while the
// generator judges): the existing check refuses with ErrConflict and
// nothing is written. Not a defect.
func TestWindow_Control_OutsideCommitAfterBaseSample(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	orig := winBody(winTenant, "60", "")
	outside := winBody(winTenant, "60", "75")
	var extHead string
	w.SetValidator(&winHookValidator{fn: func() { extHead = winOutsideCommit(t, dir, winTenant, outside) }})

	_, err := w.WriteIfUnchanged(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", ""), winSourceHash(orig))
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("err = %v, want ErrConflict", err)
	}
	if gitRevParse(t, dir, "HEAD") != extHead {
		t.Fatal("HEAD is not the outside commit")
	}
	if got := winServing(t, dir, winTenant); got != outside {
		t.Fatalf("serving file = %q, want the outside commit's", got)
	}
	t.Logf("MEASURED C1: err=ErrConflict, HEAD=outside commit, serving file=outside content")
}

// C2 (409 judgement): an outside commit of an UNRELATED tenant's file,
// before vs after the base sample. Before: no 409, the write commits onto
// it. After: ErrConflict — by design (ADR-036 §4: the verdict is on the
// judged tree, and the tree changed), not a misjudgement of the client's
// base: nothing is written, a retry succeeds.
func TestWindow_Control_UnrelatedOutsideCommit(t *testing.T) {
	t.Parallel()
	t.Run("before base sample", func(t *testing.T) {
		t.Parallel()
		dir := winRepo(t)
		w := newTestWriter(dir, dir)
		var extHead string
		w.beforeBaseSample = winOnce(func() { extHead = winOutsideCommit(t, dir, winOther, winBody(winOther, "61", "")) })
		_, err := w.WriteIfUnchanged(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", ""), winSourceHash(winBody(winTenant, "60", "")))
		if err != nil || gitRevParse(t, dir, "HEAD~1") != extHead {
			t.Fatalf("err = %v; want committed onto the unrelated outside commit", err)
		}
		t.Logf("MEASURED C2a: err=nil, committed onto the unrelated outside commit")
	})
	t.Run("after base sample", func(t *testing.T) {
		t.Parallel()
		dir := winRepo(t)
		w := newTestWriter(dir, dir)
		var extHead string
		w.SetValidator(&winHookValidator{fn: func() { extHead = winOutsideCommit(t, dir, winOther, winBody(winOther, "61", "")) }})
		_, err := w.WriteIfUnchanged(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", ""), winSourceHash(winBody(winTenant, "60", "")))
		if !errors.Is(err, ErrConflict) || gitRevParse(t, dir, "HEAD") != extHead {
			t.Fatalf("err = %v; want ErrConflict with HEAD at the outside commit", err)
		}
		t.Logf("MEASURED C2b: err=ErrConflict (retryable), HEAD=unrelated outside commit")
	})
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
	t.Logf("MEASURED B1: err=ErrConflict, HEAD=outside commit, serving file=outside content")
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
	t.Logf("MEASURED B2: err=nil, committed on base, uncommitted outside edit overwritten in the worktree")
}

// --- no-op rules vs an uncommitted outside edit (item 3) ----------------

// N1: the serving file holds an uncommitted outside edit X; a batch op's
// new content is exactly X.
//
// BUG(#1535-6): applyBatchOp's no-op test compares with the serving file
// (writer_batch.go `existing != nil && string(existing) == content`), not
// with HEAD: the op is reported NoOp, the batch returns success, and no
// commit is made — HEAD still holds the old bytes. The single-tenant
// writes check HEAD as well (unchangedFile, N2/N3) and commit.
func TestNoOp_Batch_ContentEqualsUncommittedEdit(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	headOld := gitRevParse(t, dir, "HEAD")
	x := winBody(winTenant, "90", "")
	winOutsideEdit(t, dir, winTenant, x)

	out, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant,
		Merge: func([]byte) (string, error) { return x, nil }}}, "alice@example.com")
	if err != nil {
		t.Fatalf("batch = %v; measured: success (BUG #1535-6)", err)
	}
	if len(out) != 1 || !out[0].NoOp {
		t.Fatalf("outcome = %+v; measured: NoOp", out)
	}
	if gitRevParse(t, dir, "HEAD") != headOld {
		t.Fatal("HEAD moved: the no-op hole is closed — update this test")
	}
	if got := gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"); got != winBody(winTenant, "60", "") {
		t.Fatalf("HEAD's file = %q", got)
	}
	t.Logf("MEASURED N1: err=nil, outcome NoOp=true, HEAD unmoved, HEAD file=old, serving file=X (uncommitted)")
}

// N1b: the same through ErrMergeNoOp — the serving file has an uncommitted
// removal of the key an unset-only op would remove; the merge (judging the
// serving bytes) says "nothing to remove".
//
// BUG(#1535-6): success, no commit; HEAD still carries the key.
func TestNoOp_Batch_MergeNoOpOnUncommittedEdit(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	winOutsideCommit(t, dir, winTenant, winBody(winTenant, "60", "75")) // HEAD has container_cpu
	w := newTestWriter(dir, dir)
	headOld := gitRevParse(t, dir, "HEAD")
	winOutsideEdit(t, dir, winTenant, winBody(winTenant, "60", "")) // uncommitted removal

	out, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant, Merge: winUnsetCPU}}, "alice@example.com")
	if err != nil || len(out) != 1 || !out[0].NoOp {
		t.Fatalf("batch = %+v, %v; measured: success with NoOp (BUG #1535-6)", out, err)
	}
	if gitRevParse(t, dir, "HEAD") != headOld || !strings.Contains(gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"), "container_cpu") {
		t.Fatal("HEAD changed: update this test")
	}
	t.Logf("MEASURED N1b: err=nil, NoOp=true, HEAD unmoved and still holds container_cpu")
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
	t.Logf("MEASURED N2: err=nil, X committed")
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
	t.Logf("MEASURED N3: err=nil, X committed")
}

// N4: WriteMerged's ErrMergeNoOp short-circuit does NOT ask HEAD — the
// same uncommitted-removal setup as N1b.
//
// BUG(#1535-6): success, no commit; HEAD still carries the key the caller
// asked to remove (readMergeValidateIn returns ErrMergeNoOp on the serving
// bytes; WriteMerged returns before unchangedFile).
func TestNoOp_WriteMerged_MergeNoOpOnUncommittedEdit(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	winOutsideCommit(t, dir, winTenant, winBody(winTenant, "60", "75"))
	w := newTestWriter(dir, dir)
	headOld := gitRevParse(t, dir, "HEAD")
	winOutsideEdit(t, dir, winTenant, winBody(winTenant, "60", ""))

	if _, err := w.WriteMerged(context.Background(), winTenant, "alice@example.com", winUnsetCPU); err != nil {
		t.Fatalf("WriteMerged = %v; measured: success (BUG #1535-6)", err)
	}
	if gitRevParse(t, dir, "HEAD") != headOld || !strings.Contains(gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"), "container_cpu") {
		t.Fatal("HEAD changed: update this test")
	}
	t.Logf("MEASURED N4: err=nil, HEAD unmoved and still holds container_cpu")
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
	t.Logf("MEASURED N5: err=nil, commit (author alice) holds the outside container_cpu too; validator judged those bytes")
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
// BUG(#1535-6) in P2 (ref moved): the batch IS on the branch, yet the
// error is the "not committed" kind (the handler says "nothing was
// written", see handler TestAsyncBatch_RefStateUnknown…), and with the
// marker gone nothing brings the serving file up to the branch: the
// worktree stays at the old bytes behind a HEAD that has the new ones.
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
				t.Fatalf("out=%v err=%v; measured: nil outcomes, a plain error", out, err)
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
				// BUG(#1535-6): published, reported as not written, worktree behind.
				if head == base || gitT(t, dir, "show", "HEAD:"+winTenant+".yaml") != body {
					t.Fatal("the ref did not move")
				}
				if serving != winBody(winTenant, "60", "") {
					t.Fatalf("serving file = %q; measured: still the old bytes", serving)
				}
				if st := gitT(t, dir, "status", "--porcelain", "--", winTenant+".yaml"); strings.TrimSpace(st) == "" {
					t.Fatal("git status clean; measured: the path shows as changed (worktree behind HEAD)")
				}
			} else if head != base || serving != winBody(winTenant, "60", "") {
				t.Fatalf("HEAD moved or serving file changed")
			}
			t.Logf("MEASURED %s: err=%q, marker removed, HEAD moved=%v, serving file old=%v",
				name, err, head != base, serving == winBody(winTenant, "60", ""))
		})
	}
}
