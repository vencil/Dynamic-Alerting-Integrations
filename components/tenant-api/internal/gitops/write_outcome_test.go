package gitops

// #3173: a git command past its own deadline is ErrGitTimeout (not
// context.DeadlineExceeded), and a direct write whose ref update failed is
// decided by reading the branch back — not committed, committed, or (only
// when neither HEAD nor the branch can be read) unknown, with the write's
// publish marker (batch and single-tenant alike) kept for
// RecoverBatchPublish.
//
// The faults: a reference-transaction hook on refs/heads/main that sleeps
// past the Writer's git timeout in the "prepared" state (the ref has not
// moved) or the "committed" state (it has), optionally breaking HEAD and the
// branch ref first; and a git stub (Writer.gitBinary) that fails or hangs
// chosen commands. Every Writer gets its own timeout (no process-wide env).
// Tenant ids are fixture names (CLAUDE.md #9).

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

const outcomeTimeout = 2 * time.Second

// refTxHook installs a reference-transaction hook in repo that, for a
// transaction touching refs/heads/main in state, runs act (sh; $new is the
// new id) and then sleeps past outcomeTimeout with its output detached, so
// the deadline kill of git returns at once.
func refTxHook(t *testing.T, repo, state, act string) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("sh hooks; run on Linux")
	}
	hook := filepath.Join(repo, ".git", "hooks", "reference-transaction")
	if err := os.MkdirAll(filepath.Dir(hook), 0o755); err != nil {
		t.Fatal(err)
	}
	script := "#!/bin/sh\n" +
		"in=$(cat)\n" +
		"line=$(printf '%s\\n' \"$in\" | grep ' refs/heads/main$')\n" +
		"[ -z \"$line\" ] && exit 0\n" +
		"[ \"$1\" = " + state + " ] || exit 0\n" +
		"new=$(printf '%s\\n' \"$line\" | cut -d' ' -f2)\n" +
		act + "\n" +
		"exec sleep 10 >/dev/null 2>&1 </dev/null\n"
	if err := os.WriteFile(hook, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
}

// breakBranchAct is a refTxHook act that saves the new id to saved, points
// HEAD at a branch that does not exist and overwrites the branch ref with
// garbage: neither HEAD nor refs/heads/main can be read afterwards.
func breakBranchAct(repo, saved string) string {
	return "printf '%s\\n' \"$new\" > '" + saved + "'; " +
		"printf 'ref: refs/heads/zz-unreadable\\n' > '" + filepath.Join(repo, ".git", "HEAD") + "'; " +
		"printf 'garbage\\n' > '" + filepath.Join(repo, ".git", "refs", "heads", "main") + "'"
}

// repairBranch undoes breakBranchAct: the branch ref back at id, HEAD on
// main, and the hook removed.
func repairBranch(t *testing.T, repo, id string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(repo, ".git", "refs", "heads", "main"), []byte(id+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, ".git", "HEAD"), []byte("ref: refs/heads/main\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(repo, ".git", "hooks", "reference-transaction")); err != nil {
		t.Fatal(err)
	}
}

func outcomeWriter(t *testing.T, dir string) *Writer {
	t.Helper()
	w := newTestWriter(dir, dir)
	w.SetGitTimeout(outcomeTimeout)
	w.gitWaitDelay = 200 * time.Millisecond
	return w
}

func outcomeBatch(w *Writer, body string) ([]BatchOpOutcome, error) {
	return w.WriteBatch(context.Background(), []BatchOp{{TenantID: winTenant,
		Merge: func([]byte) (string, error) { return body, nil }}}, "alice@example.com")
}

func markerExists(t *testing.T, w *Writer) bool {
	t.Helper()
	p, err := w.publishMarkerPath()
	if err != nil {
		t.Fatal(err)
	}
	_, err = os.Stat(p)
	return err == nil
}

// The three outcomes of a git timeout at the ref update, single-tenant
// (gitCommitOnto) and batch (publishBatch) alike.
func TestGitTimeoutAtRefUpdate_Outcomes(t *testing.T) {
	t.Parallel()
	oldBody := winBody(winTenant, "60", "")
	newBody := winBody(winTenant, "90", "")
	for _, batch := range []bool{false, true} {
		write := func(w *Writer) error {
			if batch {
				_, err := outcomeBatch(w, newBody)
				return err
			}
			_, err := w.Write(context.Background(), winTenant, "alice@example.com", newBody)
			return err
		}
		kind := "single"
		if batch {
			kind = "batch"
		}

		t.Run(kind+"/not committed", func(t *testing.T) {
			t.Parallel()
			dir := winRepo(t)
			base := gitRevParse(t, dir, "HEAD")
			refTxHook(t, dir, "prepared", ":")
			w := outcomeWriter(t, dir)
			err := write(w)
			if !errors.Is(err, ErrGitTimeout) || OutcomeOf(err) != WriteNotCommitted {
				t.Fatalf("err = %v (outcome %d), want ErrGitTimeout, not committed", err, OutcomeOf(err))
			}
			if errors.Is(err, context.DeadlineExceeded) {
				t.Errorf("a git timeout unwraps to context.DeadlineExceeded: %v", err)
			}
			if head := gitRevParse(t, dir, "HEAD"); head != base {
				t.Errorf("HEAD moved to %s", head)
			}
			if got := winServing(t, dir, winTenant); got != oldBody {
				t.Errorf("serving file = %q, want the old bytes", got)
			}
			if markerExists(t, w) {
				t.Error("publish marker left behind a write that did not publish")
			}
		})

		t.Run(kind+"/committed", func(t *testing.T) {
			t.Parallel()
			dir := winRepo(t)
			base := gitRevParse(t, dir, "HEAD")
			refTxHook(t, dir, "committed", ":")
			w := outcomeWriter(t, dir)
			if err := write(w); err != nil {
				t.Fatalf("err = %v, want nil: the ref moved before git was killed", err)
			}
			if head := gitRevParse(t, dir, "HEAD"); head == base {
				t.Fatal("HEAD did not move: the fault did not fire")
			}
			if got := gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"); got != newBody {
				t.Errorf("HEAD holds %q", got)
			}
			if got := winServing(t, dir, winTenant); got != newBody {
				t.Errorf("serving file = %q, want the new bytes", got)
			}
			if markerExists(t, w) {
				t.Error("publish marker left behind a finished write")
			}
		})

		t.Run(kind+"/unknown", func(t *testing.T) {
			t.Parallel()
			dir := winRepo(t)
			saved := filepath.Join(t.TempDir(), "new")
			refTxHook(t, dir, "committed", breakBranchAct(dir, saved))
			w := outcomeWriter(t, dir)
			err := write(w)
			var unknown *WriteOutcomeUnknownError
			if !errors.As(err, &unknown) || OutcomeOf(err) != WriteUnknown {
				t.Fatalf("err = %v, want *WriteOutcomeUnknownError", err)
			}
			if errors.Is(err, ErrGitTimeout) || errors.Is(err, ErrConflict) {
				t.Errorf("an unknown outcome unwraps to its cause: %v", err)
			}
			if !errors.Is(unknown.Cause, ErrGitTimeout) {
				t.Errorf("Cause = %v, want the git timeout recorded", unknown.Cause)
			}
			moved, rerr := os.ReadFile(saved)
			if rerr != nil {
				t.Fatalf("the fault did not fire: %v", rerr)
			}
			if !markerExists(t, w) {
				t.Fatal("publish marker dropped: nothing can bring the worktree up to the branch")
			}
			// The branch did move: once it can be read, the next write's
			// recovery brings the serving file up to it.
			repairBranch(t, dir, strings.TrimSpace(string(moved)))
			if got := gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"); got != newBody {
				t.Fatalf("HEAD holds %q: the ref did not move", got)
			}
			// Single-tenant too (#3173): its index entry was put back to the
			// old blob (unsyncIndexEntry could not see the branch) — a staged
			// revert of the published write until recovery resets it.
			if err := w.RecoverBatchPublish(); err != nil {
				t.Fatalf("RecoverBatchPublish: %v", err)
			}
			if markerExists(t, w) {
				t.Error("publish marker still present after recovery")
			}
			if got := winServing(t, dir, winTenant); got != newBody {
				t.Errorf("serving file = %q after recovery, want the branch's", got)
			}
			if st := gitT(t, dir, "status", "--porcelain", "--", winTenant+".yaml"); st != "" {
				t.Errorf("git status after recovery: %q", st)
			}
		})
	}
}

// refAndHeadFailingGit is a git stub: the batch's `update-ref` exits 1
// (after running it when ranFirst), and after that every `rev-parse HEAD`
// exits 1 — and, when branchToo, every `rev-parse --verify -q refs/heads/…`.
func refAndHeadFailingGit(t *testing.T, ranFirst, branchToo bool) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("sh stub; run on Linux")
	}
	d := t.TempDir()
	flag := filepath.Join(d, "ref-failed")
	onUpdateRef := "touch '" + flag + "'; exit 1"
	if ranFirst {
		onUpdateRef = "git \"$@\"; touch '" + flag + "'; exit 1"
	}
	branchCase := ""
	if branchToo {
		branchCase = "  *rev-parse\\ --verify\\ -q\\ refs/heads/*) [ -e '" + flag + "' ] && exit 1 ;;\n"
	}
	stub := filepath.Join(d, "git")
	script := "#!/bin/sh\n" +
		"case \"$*\" in\n" +
		"  *update-ref*'tenant-api: batch'*) " + onUpdateRef + " ;;\n" +
		"  *rev-parse\\ HEAD) [ -e '" + flag + "' ] && exit 1 ;;\n" +
		branchCase +
		"esac\n" +
		"exec git \"$@\"\n"
	if err := os.WriteFile(stub, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return stub
}

// #1535 item 6 / #3173 P2: publishBatch's update-ref fails (not a timeout)
// and HEAD cannot be read afterwards. FIXED(#3173): the branch ref is read
// directly (around the HEAD symref) and decides; only when it cannot be read
// either is the outcome unknown, with the publish marker kept. Before the
// fix every variant was a plain "not committed" error with the marker
// dropped — in the "ref moved" variants the batch was on the branch, the
// handler said nothing was written, and the serving file stayed behind.
func TestPublishBatch_RefFailsAndHeadUnreadable(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name                string
		ranFirst, branchToo bool
	}{
		{"ref unmoved, branch readable", false, false},
		{"ref moved, branch readable", true, false},
		{"ref unmoved, branch unreadable", false, true},
		{"ref moved, branch unreadable", true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := winRepo(t)
			w := newTestWriter(dir, dir)
			w.gitBinary = refAndHeadFailingGit(t, tc.ranFirst, tc.branchToo)
			base := gitRevParse(t, dir, "HEAD")
			oldBody := winBody(winTenant, "60", "")
			body := winBody(winTenant, "90", "")
			out, err := outcomeBatch(w, body)
			head := gitRevParse(t, dir, "HEAD")
			if (head != base) != tc.ranFirst {
				t.Fatalf("HEAD moved = %v, want %v: the fault did not fire as meant", head != base, tc.ranFirst)
			}
			switch {
			case tc.branchToo:
				// Unknown: the marker stays, and recovery with a readable
				// HEAD settles it either way.
				var unknown *WriteOutcomeUnknownError
				if !errors.As(err, &unknown) || out != nil || unknown.Commit == "" {
					t.Fatalf("out=%v err=%v; want nil outcomes, *WriteOutcomeUnknownError", out, err)
				}
				if !markerExists(t, w) {
					t.Fatal("publish marker dropped on an unknown outcome")
				}
				if got := winServing(t, dir, winTenant); got != oldBody {
					t.Fatalf("serving file changed before recovery: %q", got)
				}
				w.gitBinary = "git"
				if err := w.RecoverBatchPublish(); err != nil {
					t.Fatalf("RecoverBatchPublish: %v", err)
				}
				if markerExists(t, w) {
					t.Error("publish marker still present after recovery")
				}
				want := oldBody
				if tc.ranFirst {
					want = body
				}
				if got := winServing(t, dir, winTenant); got != want {
					t.Errorf("serving file = %q after recovery, want %q", got, want)
				}
			case tc.ranFirst:
				// Committed, read through the branch ref: a clean success.
				if err != nil || out == nil {
					t.Fatalf("out=%v err=%v; want the batch's outcomes, nil", out, err)
				}
				if got := winServing(t, dir, winTenant); got != body {
					t.Errorf("serving file = %q, want the branch's", got)
				}
				if markerExists(t, w) {
					t.Error("publish marker left behind a finished batch")
				}
			default:
				// Not committed: a plain error, the marker gone.
				if err == nil || OutcomeOf(err) != WriteNotCommitted || errors.Is(err, ErrConflict) || out != nil {
					t.Fatalf("out=%v err=%v; want nil outcomes, a not-committed error", out, err)
				}
				if markerExists(t, w) {
					t.Error("publish marker left behind a batch that did not publish")
				}
				if got := winServing(t, dir, winTenant); got != oldBody {
					t.Errorf("serving file = %q, want the old bytes", got)
				}
			}
		})
	}
}

// statusHangingGit is a git stub whose `status --porcelain…` (the clean
// check, checkTenantFileClean) sleeps past the Writer's git timeout.
func statusHangingGit(t *testing.T) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("sh stub; run on Linux")
	}
	stub := filepath.Join(t.TempDir(), "git")
	script := "#!/bin/sh\n" +
		"case \"$*\" in\n" +
		"  *status\\ --porcelain*) exec sleep 10 >/dev/null 2>&1 </dev/null ;;\n" +
		"esac\n" +
		"exec git \"$@\"\n"
	if err := os.WriteFile(stub, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return stub
}

// A timeout in the clean check (#3176's checkTenantFileClean) comes before
// anything is written: ErrGitTimeout, not committed — single and batch.
func TestGitTimeoutInCleanCheck_NotCommitted(t *testing.T) {
	t.Parallel()
	for _, batch := range []bool{false, true} {
		name := "single"
		if batch {
			name = "batch"
		}
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := winRepo(t)
			base := gitRevParse(t, dir, "HEAD")
			w := outcomeWriter(t, dir)
			w.gitBinary = statusHangingGit(t)
			body := winBody(winTenant, "90", "")
			var err error
			if batch {
				_, err = outcomeBatch(w, body)
			} else {
				_, err = w.Write(context.Background(), winTenant, "alice@example.com", body)
			}
			if !errors.Is(err, ErrGitTimeout) || OutcomeOf(err) != WriteNotCommitted {
				t.Fatalf("err = %v (outcome %d), want ErrGitTimeout, not committed", err, OutcomeOf(err))
			}
			if !strings.Contains(err.Error(), "check the tenant file against HEAD") {
				t.Errorf("err = %v: not the clean check's timeout", err)
			}
			if head := gitRevParse(t, dir, "HEAD"); head != base {
				t.Errorf("HEAD moved to %s", head)
			}
			if got := winServing(t, dir, winTenant); got != winBody(winTenant, "60", "") {
				t.Errorf("serving file = %q, want the old bytes", got)
			}
		})
	}
}

func TestOutcomeOf(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name string
		err  error
		want WriteOutcome
	}{
		{"nil", nil, WriteCommitted},
		{"batch committed", &BatchCommittedError{Commit: "c", Cause: ErrGitTimeout}, WriteCommitted},
		{"unknown", &WriteOutcomeUnknownError{Commit: "c", Cause: ErrGitTimeout}, WriteUnknown},
		{"unknown, wrapped", errors.Join(errors.New("git commit"), &WriteOutcomeUnknownError{Commit: "c"}), WriteUnknown},
		{"git timeout", errors.Join(errors.New("advance HEAD"), ErrGitTimeout), WriteNotCommitted},
		{"cancelled", context.Canceled, WriteNotCommitted},
		{"conflict", ErrConflict, WriteNotCommitted},
	} {
		if got := OutcomeOf(tc.err); got != tc.want {
			t.Errorf("%s: OutcomeOf(%v) = %d, want %d", tc.name, tc.err, got, tc.want)
		}
	}
}

// What singleRefFailingGit makes unreadable once the ref update failed.
const (
	revParseHEAD  = iota // `rev-parse HEAD` only: restoreFromHEAD still works
	allHEAD              // every command naming HEAD (ls-tree, restore, cat-file, HEAD:<path>), as a real unreadable HEAD
	headAndBranch        // allHEAD, and `rev-parse --verify -q refs/heads/…`
)

// singleRefFailingGit is a git stub for a single-tenant write: its
// `update-ref` exits 1 (after running it when ranFirst). After that what
// unreadable names exits 1; when failIndex, every `update-index` too
// (unsyncIndexEntry cannot put the index entry back).
func singleRefFailingGit(t *testing.T, ranFirst bool, unreadable int, failIndex bool) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("sh stub; run on Linux")
	}
	d := t.TempDir()
	flag := filepath.Join(d, "ref-failed")
	onUpdateRef := "touch '" + flag + "'; exit 1"
	if ranFirst {
		onUpdateRef = "git \"$@\"; touch '" + flag + "'; exit 1"
	}
	headCase := "  *rev-parse\\ HEAD) [ -e '" + flag + "' ] && exit 1 ;;\n"
	if unreadable != revParseHEAD {
		headCase = "  *HEAD*) [ -e '" + flag + "' ] && exit 1 ;;\n"
	}
	if unreadable == headAndBranch {
		headCase += "  *rev-parse\\ --verify\\ -q\\ refs/heads/*) [ -e '" + flag + "' ] && exit 1 ;;\n"
	}
	if failIndex {
		headCase += "  *update-index*) [ -e '" + flag + "' ] && exit 1 ;;\n"
	}
	stub := filepath.Join(d, "git")
	script := "#!/bin/sh\n" +
		"case \"$*\" in\n" +
		"  *update-ref*'tenant-api: tenant/'*) " + onUpdateRef + " ;;\n" +
		headCase +
		"esac\n" +
		"exec git \"$@\"\n"
	if err := os.WriteFile(stub, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return stub
}

// #3173 root fix: a single-tenant write whose update-ref fails while
// neither HEAD nor the branch can be read keeps a publish marker that
// names its commit and path, as a batch does. Before the fix the serving
// file kept the write (restoreFromHEAD cannot read HEAD either) with no
// record of it: when the commit had not landed, every later write to the
// tenant was TENANT_FILE_DIRTY until an operator repaired the file by hand.
// Known outcomes leave no marker.
func TestGitCommitOnto_RefFailsAndHeadUnreadable(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name                string
		ranFirst, branchToo bool
	}{
		{"ref unmoved, branch readable", false, false},
		{"ref moved, branch readable", true, false},
		{"ref unmoved, branch unreadable", false, true},
		{"ref moved, branch unreadable", true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := winRepo(t)
			w := newTestWriter(dir, dir)
			// failIndex: the index entry keeps the unpublished commit's blob,
			// so recovery has to reset it.
			unreadable := revParseHEAD
			if tc.branchToo {
				unreadable = headAndBranch
			}
			w.gitBinary = singleRefFailingGit(t, tc.ranFirst, unreadable, tc.branchToo)
			base := gitRevParse(t, dir, "HEAD")
			oldBody := winBody(winTenant, "60", "")
			body := winBody(winTenant, "90", "")
			_, err := w.Write(context.Background(), winTenant, "alice@example.com", body)
			head := gitRevParse(t, dir, "HEAD")
			if (head != base) != tc.ranFirst {
				t.Fatalf("HEAD moved = %v, want %v: the fault did not fire as meant", head != base, tc.ranFirst)
			}
			if !tc.branchToo {
				if tc.ranFirst && err != nil {
					t.Fatalf("err = %v, want nil: the branch ref shows the commit", err)
				}
				if !tc.ranFirst && (err == nil || OutcomeOf(err) != WriteNotCommitted || errors.Is(err, ErrConflict)) {
					t.Fatalf("err = %v, want a not-committed error", err)
				}
				// Not published: the marker goes only because restoreFromHEAD
				// succeeded (rev-parse HEAD alone fails here).
				if markerExists(t, w) {
					t.Error("publish marker left behind a known outcome whose serving file is back")
				}
				if got := winServing(t, dir, winTenant); got != map[bool]string{true: body, false: oldBody}[tc.ranFirst] {
					t.Errorf("serving file = %q", got)
				}
				return
			}

			var unknown *WriteOutcomeUnknownError
			if !errors.As(err, &unknown) || unknown.Commit == "" {
				t.Fatalf("err = %v, want *WriteOutcomeUnknownError", err)
			}
			if !markerExists(t, w) {
				t.Fatal("publish marker dropped on an unknown outcome")
			}
			// The defect's state: the serving file holds the write.
			if got := winServing(t, dir, winTenant); got != body {
				t.Fatalf("serving file = %q, want the written bytes before recovery (fault not as meant)", got)
			}

			// While HEAD stays unreadable, recovery cannot decide: the
			// marker stays and every write is refused, naming it.
			marker, _ := w.publishMarkerPath()
			_, err = w.Write(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "75", ""))
			if err == nil || !strings.Contains(err.Error(), marker) || OutcomeOf(err) != WriteNotCommitted {
				t.Fatalf("write while HEAD is unreadable: err = %v, want a refusal naming %s", err, marker)
			}
			if !markerExists(t, w) {
				t.Fatal("publish marker dropped while HEAD is unreadable")
			}

			w.gitBinary = "git"
			if err := w.RecoverBatchPublish(); err != nil {
				t.Fatalf("RecoverBatchPublish: %v", err)
			}
			if markerExists(t, w) {
				t.Error("publish marker still present after recovery")
			}
			want := oldBody
			if tc.ranFirst {
				want = body
			}
			if got := winServing(t, dir, winTenant); got != want {
				t.Errorf("serving file = %q after recovery, want %q", got, want)
			}
			if st := gitT(t, dir, "status", "--porcelain", "--", winTenant+".yaml"); st != "" {
				t.Errorf("git status after recovery: %q, want clean (worktree and index at HEAD)", st)
			}

			// The tenant is writable again: no TENANT_FILE_DIRTY.
			next := winBody(winTenant, "75", "")
			if _, err := w.Write(context.Background(), winTenant, "alice@example.com", next); err != nil {
				t.Fatalf("next write after recovery: %v", err)
			}
			if got := gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"); got != next {
				t.Errorf("HEAD holds %q after the next write", got)
			}
			if markerExists(t, w) {
				t.Error("publish marker left behind a successful write")
			}
		})
	}
}

// The next write runs the recovery itself (lockTreeRecovered): an
// unpublished single-tenant write left by an unknown outcome does not make
// the following write TENANT_FILE_DIRTY.
func TestGitCommitOnto_UnknownUnpublished_NextWriteRecovers(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	w.gitBinary = singleRefFailingGit(t, false, headAndBranch, false)
	base := gitRevParse(t, dir, "HEAD")
	if _, err := w.Write(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", "")); OutcomeOf(err) != WriteUnknown {
		t.Fatalf("err = %v, want an unknown outcome", err)
	}
	w.gitBinary = "git"
	next := winBody(winTenant, "75", "")
	if _, err := w.Write(context.Background(), winTenant, "alice@example.com", next); err != nil {
		t.Fatalf("next write: %v", err)
	}
	if got := gitRevParse(t, dir, "HEAD~1"); got != base {
		t.Errorf("HEAD~1 = %s, want the base %s: the unpublished commit is not on the branch", got, base)
	}
	if got := winServing(t, dir, winTenant); got != next {
		t.Errorf("serving file = %q", got)
	}
	if markerExists(t, w) {
		t.Error("publish marker left behind")
	}
}

// Recovery of an unpublished single-tenant write resets the path's index
// entry only while it names the unpublished commit's blob: an entry an
// operator staged afterwards is theirs and stays staged. The serving file
// is still put back as HEAD has it.
func TestRecoverPublish_UnpublishedLeavesOperatorIndexEntry(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	w.gitBinary = singleRefFailingGit(t, false, headAndBranch, true)
	oldBody := winBody(winTenant, "60", "")
	if _, err := w.Write(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", "")); OutcomeOf(err) != WriteUnknown {
		t.Fatalf("err = %v, want an unknown outcome", err)
	}
	w.gitBinary = "git"
	staged := winBody(winTenant, "33", "")
	if err := os.WriteFile(filepath.Join(dir, winTenant+".yaml"), []byte(staged), 0o644); err != nil {
		t.Fatal(err)
	}
	gitT(t, dir, "add", "--", winTenant+".yaml")
	if err := w.RecoverBatchPublish(); err != nil {
		t.Fatalf("RecoverBatchPublish: %v", err)
	}
	if got := winServing(t, dir, winTenant); got != oldBody {
		t.Errorf("serving file = %q, want HEAD's", got)
	}
	if got := gitT(t, dir, "show", ":"+winTenant+".yaml"); got != staged {
		t.Errorf("index holds %q, want the operator's staged bytes", got)
	}
	if markerExists(t, w) {
		t.Error("publish marker still present after recovery")
	}
}

// A marker is one format for both writers: the commit alone on the first
// line is a batch's (as every marker from before #3173), the commit and
// markerServingWritten a single-tenant write's, and any other first line is
// an error that keeps the marker (never read as a batch's and dropped).
func TestParsePublishMarker(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		raw            string
		commit         string
		rels           []string
		servingWritten bool
		wantErr        bool
	}{
		{raw: "abc\na.yaml\nb.yaml\n", commit: "abc", rels: []string{"a.yaml", "b.yaml"}},
		{raw: "abc serving-written\nconf.d/a.yaml\n", commit: "abc", rels: []string{"conf.d/a.yaml"}, servingWritten: true},
		{raw: "abc something-new\na.yaml\n", wantErr: true},
		{raw: "\n", wantErr: true},
		{raw: "- serving-written\nconf.d/a.yaml\n", commit: "", rels: []string{"conf.d/a.yaml"}, servingWritten: true},
		{raw: "-\na.yaml\n", wantErr: true},
	} {
		commit, rels, sw, err := parsePublishMarker([]byte(tc.raw))
		if (err != nil) != tc.wantErr {
			t.Errorf("%q: err = %v, wantErr %v", tc.raw, err, tc.wantErr)
			continue
		}
		if tc.wantErr {
			continue
		}
		if commit != tc.commit || sw != tc.servingWritten || strings.Join(rels, ",") != strings.Join(tc.rels, ",") {
			t.Errorf("%q: got (%q, %q, %v), want (%q, %q, %v)", tc.raw, commit, rels, sw, tc.commit, tc.rels, tc.servingWritten)
		}
	}
}

// HEAD unreadable but the branch ref readable: the ref update is positively
// NOT published (a known outcome), yet commitFileChange's restoreFromHEAD
// fails on the same unreadable HEAD. The marker follows the serving file,
// not the outcome: it stays, and the next write's recovery puts the file
// back — no TENANT_FILE_DIRTY. (Published with the same fault: the marker
// goes, the write is a success.)
func TestGitCommitOnto_NotPublishedRestoreFails_MarkerKept(t *testing.T) {
	t.Parallel()
	for _, ranFirst := range []bool{false, true} {
		name := "ref unmoved"
		if ranFirst {
			name = "ref moved"
		}
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := winRepo(t)
			w := newTestWriter(dir, dir)
			w.gitBinary = singleRefFailingGit(t, ranFirst, allHEAD, false)
			base := gitRevParse(t, dir, "HEAD")
			body := winBody(winTenant, "90", "")
			_, err := w.Write(context.Background(), winTenant, "alice@example.com", body)
			if ranFirst {
				if err != nil {
					t.Fatalf("err = %v, want nil: the branch ref shows the commit", err)
				}
				if markerExists(t, w) {
					t.Error("publish marker left behind a published write")
				}
				return
			}
			if err == nil || OutcomeOf(err) != WriteNotCommitted || errors.Is(err, ErrConflict) {
				t.Fatalf("err = %v, want a not-committed error", err)
			}
			if got := winServing(t, dir, winTenant); got != body {
				t.Fatalf("serving file = %q: restoreFromHEAD did not fail as meant", got)
			}
			if !markerExists(t, w) {
				t.Fatal("publish marker dropped although the serving file still holds the unpublished write")
			}

			w.gitBinary = "git"
			next := winBody(winTenant, "75", "")
			if _, err := w.Write(context.Background(), winTenant, "alice@example.com", next); err != nil {
				t.Fatalf("next write: %v", err)
			}
			if got := gitRevParse(t, dir, "HEAD~1"); got != base {
				t.Errorf("HEAD~1 = %s, want the base %s", got, base)
			}
			if got := winServing(t, dir, winTenant); got != next {
				t.Errorf("serving file = %q", got)
			}
			if markerExists(t, w) {
				t.Error("publish marker left behind")
			}
		})
	}
}

// A conflict (HEAD moved outside while the write ran) is not published;
// its serving file is put back by restoreFromHEAD, so no marker is left.
func TestGitCommitOnto_ConflictLeavesNoMarker(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	w.beforeCASAdvance = func() { outsideCommit(t, dir, "landed in the window") }
	_, err := w.Write(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", ""))
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("err = %v, want ErrConflict", err)
	}
	if got := winServing(t, dir, winTenant); got != winBody(winTenant, "60", "") {
		t.Errorf("serving file = %q, want the old bytes", got)
	}
	if markerExists(t, w) {
		t.Error("publish marker left behind a conflict whose serving file is back")
	}
}

// commitTreeFailingGit is a git stub whose `commit-tree` exits 1 — a
// failure before the write has a commit — after which every command naming
// HEAD exits 1 too, so commitFileChange's restoreFromHEAD fails.
func commitTreeFailingGit(t *testing.T) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("sh stub; run on Linux")
	}
	d := t.TempDir()
	flag := filepath.Join(d, "failed")
	stub := filepath.Join(d, "git")
	script := "#!/bin/sh\n" +
		"case \"$*\" in\n" +
		"  *commit-tree*) touch '" + flag + "'; exit 1 ;;\n" +
		"  *HEAD*) [ -e '" + flag + "' ] && exit 1 ;;\n" +
		"esac\n" +
		"exec git \"$@\"\n"
	if err := os.WriteFile(stub, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return stub
}

// The marker's lifetime starts at the serving-file write, not at
// commit-tree (#3173): a write that fails before it has a commit, and
// whose restoreFromHEAD fails as well, leaves a commit-less marker; the
// next write's recovery puts the file back (index untouched) — no
// TENANT_FILE_DIRTY.
func TestCommitFileChange_FailsBeforeCommit_RestoreFails_MarkerRecovers(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	w.gitBinary = commitTreeFailingGit(t)
	base := gitRevParse(t, dir, "HEAD")
	body := winBody(winTenant, "90", "")
	_, err := w.Write(context.Background(), winTenant, "alice@example.com", body)
	if err == nil || OutcomeOf(err) != WriteNotCommitted {
		t.Fatalf("err = %v, want a not-committed error", err)
	}
	if got := winServing(t, dir, winTenant); got != body {
		t.Fatalf("serving file = %q: restoreFromHEAD did not fail as meant", got)
	}
	p, _ := w.publishMarkerPath()
	raw, rerr := os.ReadFile(p)
	if rerr != nil {
		t.Fatalf("publish marker missing although the serving file holds an uncommitted write: %v", rerr)
	}
	if first, _, _ := strings.Cut(string(raw), "\n"); first != markerNoCommit+" "+markerServingWritten {
		t.Errorf("marker first line = %q, want the commit-less form", first)
	}
	indexBefore := gitT(t, dir, "ls-files", "-s", "--", winTenant+".yaml")

	w.gitBinary = "git"
	if err := w.RecoverBatchPublish(); err != nil {
		t.Fatalf("RecoverBatchPublish: %v", err)
	}
	if got := winServing(t, dir, winTenant); got != winBody(winTenant, "60", "") {
		t.Errorf("serving file = %q after recovery, want HEAD's", got)
	}
	if got := gitT(t, dir, "ls-files", "-s", "--", winTenant+".yaml"); got != indexBefore {
		t.Errorf("index entry %q changed by recovery (was %q): a commit-less marker must not touch it", got, indexBefore)
	}
	if markerExists(t, w) {
		t.Error("publish marker still present after recovery")
	}
	next := winBody(winTenant, "75", "")
	if _, err := w.Write(context.Background(), winTenant, "alice@example.com", next); err != nil {
		t.Fatalf("next write: %v", err)
	}
	if got := gitRevParse(t, dir, "HEAD~1"); got != base {
		t.Errorf("HEAD~1 = %s, want the base %s", got, base)
	}
	if markerExists(t, w) {
		t.Error("publish marker left behind a successful write")
	}
}

// Same failure, recovered by the next write itself (lockTreeRecovered).
func TestCommitFileChange_FailsBeforeCommit_NextWriteRecovers(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	w.gitBinary = commitTreeFailingGit(t)
	if _, err := w.Write(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", "")); err == nil {
		t.Fatal("first write succeeded: the fault did not fire")
	}
	w.gitBinary = "git"
	next := winBody(winTenant, "75", "")
	if _, err := w.Write(context.Background(), winTenant, "alice@example.com", next); err != nil {
		t.Fatalf("next write: %v", err)
	}
	if got := winServing(t, dir, winTenant); got != next {
		t.Errorf("serving file = %q", got)
	}
	if markerExists(t, w) {
		t.Error("publish marker left behind")
	}
}

// A successful write, and a write git finds unchanged (committed=false in
// commitFileChange: the file was written, but equals HEAD), leave no marker.
func TestCommitFileChange_SuccessAndNoChangeLeaveNoMarker(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	body := winBody(winTenant, "90", "")
	if _, err := w.Write(context.Background(), winTenant, "alice@example.com", body); err != nil {
		t.Fatalf("write: %v", err)
	}
	if markerExists(t, w) {
		t.Error("publish marker left behind a successful write")
	}

	head := gitRevParse(t, dir, "HEAD")
	if err := w.lockTreeRecovered(); err != nil {
		t.Fatal(err)
	}
	err := w.commitFileChange(filepath.Join(dir, winTenant+".yaml"), winTenant, "alice@example.com", []byte(body), head)
	w.unlockTree()
	if err != nil {
		t.Fatalf("no-change commitFileChange: %v", err)
	}
	if got := gitRevParse(t, dir, "HEAD"); got != head {
		t.Fatalf("HEAD moved to %s: not the no-change path", got)
	}
	if markerExists(t, w) {
		t.Error("publish marker left behind a no-change write")
	}
}

// A not-published write whose serving file restoreFromHEAD put back, but
// whose index entry unsyncIndexEntry could not put back: the file is clean,
// the index is not, so the marker stays and recovery resets the entry.
func TestGitCommitOnto_NotPublishedIndexNotPutBack_MarkerKept(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	w := newTestWriter(dir, dir)
	w.gitBinary = singleRefFailingGit(t, false, revParseHEAD, true)
	_, err := w.Write(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", ""))
	if err == nil || OutcomeOf(err) != WriteNotCommitted {
		t.Fatalf("err = %v, want a not-committed error", err)
	}
	if got := winServing(t, dir, winTenant); got != winBody(winTenant, "60", "") {
		t.Fatalf("serving file = %q: restoreFromHEAD did not succeed as meant", got)
	}
	if st := gitT(t, dir, "status", "--porcelain", "--", winTenant+".yaml"); st == "" {
		t.Fatal("index clean: the update-index fault did not fire")
	}
	if !markerExists(t, w) {
		t.Fatal("publish marker dropped although the index still holds the unpublished blob")
	}
	w.gitBinary = "git"
	next := winBody(winTenant, "75", "")
	if _, err := w.Write(context.Background(), winTenant, "alice@example.com", next); err != nil {
		t.Fatalf("next write: %v", err)
	}
	if markerExists(t, w) {
		t.Error("publish marker left behind")
	}
}
