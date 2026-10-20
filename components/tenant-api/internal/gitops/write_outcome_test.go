package gitops

// #3173: a git command past its own deadline is ErrGitTimeout (not
// context.DeadlineExceeded), and a direct write whose ref update failed is
// decided by reading the branch back — not committed, committed, or (only
// when neither HEAD nor the branch can be read) unknown, with the batch's
// publish marker kept for RecoverBatchPublish.
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
			if batch && markerExists(t, w) {
				t.Error("publish marker left behind a batch that did not publish")
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
			if batch && markerExists(t, w) {
				t.Error("publish marker left behind a finished batch")
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
			if batch && !markerExists(t, w) {
				t.Fatal("publish marker dropped: nothing can bring the worktree up to the branch")
			}
			// The branch did move: once it can be read, the next write's
			// recovery brings the serving file up to it.
			repairBranch(t, dir, strings.TrimSpace(string(moved)))
			if got := gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"); got != newBody {
				t.Fatalf("HEAD holds %q: the ref did not move", got)
			}
			if batch {
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
