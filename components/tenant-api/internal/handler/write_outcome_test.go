package handler

// #3173: a direct write answers from what the writer knows it left on the
// serving branch — not committed (503 msgWriteCancelled: nothing written,
// safe to retry), committed (success, or the batch's 409 committed: true),
// or unknown (503 msgWriteOutcomeUnknown: may have been committed, refresh
// first) — never from whether the cause was a timeout.
//
// The end-to-end faults are a reference-transaction hook on refs/heads/main
// that sleeps past the Writer's own git timeout (Writer.SetGitTimeout, no
// process-wide env) in the "prepared" state (the ref has not moved) or the
// "committed" state (it has), the unknown variant first pointing HEAD at a
// missing branch and garbling the branch ref so neither can be read.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
)

// The answers, from the errors the writer returns. Unknown wins whatever its
// cause — a timeout, a refusal, lock contention — because the writer keeps
// the cause out of errors.Is.
func TestWriteOutcome_Answers(t *testing.T) {
	t.Parallel()
	notCommitted := fmt.Errorf("git commit: advance HEAD: git update-ref timed out: %w", gitops.ErrGitTimeout)
	unknown := func(cause error) error {
		return fmt.Errorf("git commit: %w", &gitops.WriteOutcomeUnknownError{Commit: "abc123", Cause: cause})
	}
	type answer struct {
		status int
		msg    string
	}
	cases := []struct {
		name  string
		err   error
		put   answer
		batch answer
	}{
		{"git timeout, not committed", notCommitted,
			answer{http.StatusServiceUnavailable, msgWriteCancelled}, answer{http.StatusServiceUnavailable, msgWriteCancelled}},
		{"request cancelled", context.Canceled,
			answer{http.StatusServiceUnavailable, msgWriteCancelled}, answer{http.StatusServiceUnavailable, msgWriteCancelled}},
		{"unknown after a timeout", unknown(gitops.ErrGitTimeout),
			answer{http.StatusServiceUnavailable, msgWriteOutcomeUnknown}, answer{http.StatusServiceUnavailable, msgWriteOutcomeUnknown}},
		{"unknown after a refusal", unknown(errors.New("exit status 1")),
			answer{http.StatusServiceUnavailable, msgWriteOutcomeUnknown}, answer{http.StatusServiceUnavailable, msgWriteOutcomeUnknown}},
		{"unknown after lock contention", unknown(fmt.Errorf("x: %w", gitops.ErrWriteOverloaded)),
			answer{http.StatusServiceUnavailable, msgWriteOutcomeUnknown}, answer{http.StatusServiceUnavailable, msgWriteOutcomeUnknown}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			req := httptest.NewRequest("PUT", "/api/v1/tenants/t-a", nil)
			for _, c := range []struct {
				name  string
				want  answer
				write func(http.ResponseWriter)
			}{
				{"PUT", tc.put, func(w http.ResponseWriter) { writeTenantWriteFailure(w, req, tc.err) }},
				{"batch", tc.batch, func(w http.ResponseWriter) { writeBatchFailure(w, req, &Deps{}, tc.err) }},
			} {
				rec := httptest.NewRecorder()
				c.write(rec)
				var got ErrorResponse
				if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
					t.Fatal(err)
				}
				if rec.Code != c.want.status || got.Error != c.want.msg {
					t.Errorf("%s: %d %q, want %d %q", c.name, rec.Code, got.Error, c.want.status, c.want.msg)
				}
			}
		})
	}
}

// outcomeHook installs the hook described in the file comment: in state,
// for a transaction on refs/heads/main, run act ($new is the new id), then
// sleep past the git timeout with output detached.
func outcomeHook(t *testing.T, repo, state, act string) {
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

// breakBranch is an outcomeHook act: save the new id, then make HEAD and
// refs/heads/main unreadable.
func breakBranch(repo, saved string) string {
	return "printf '%s\\n' \"$new\" > '" + saved + "'; " +
		"printf 'ref: refs/heads/zz-unreadable\\n' > '" + filepath.Join(repo, ".git", "HEAD") + "'; " +
		"printf 'garbage\\n' > '" + filepath.Join(repo, ".git", "refs", "heads", "main") + "'"
}

// repairBranchAt puts refs/heads/main at the id saved and HEAD on it, and
// removes the hook.
func repairBranchAt(t *testing.T, repo, saved string) {
	t.Helper()
	id, err := os.ReadFile(saved)
	if err != nil {
		t.Fatalf("the fault did not fire: %v", err)
	}
	if err := os.WriteFile(filepath.Join(repo, ".git", "refs", "heads", "main"), id, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, ".git", "HEAD"), []byte("ref: refs/heads/main\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(repo, ".git", "hooks", "reference-transaction")); err != nil {
		t.Fatal(err)
	}
}

func showMain(t *testing.T, dir, path string) string {
	t.Helper()
	out, err := exec.Command("git", "-C", dir, "show", "main:"+path).CombinedOutput()
	if err != nil {
		t.Fatalf("git show main:%s: %v\n%s", path, err, out)
	}
	return string(out)
}

// End to end, PUT and the synchronous batch: a git timeout at the ref update
// answers what the writer read the branch back as.
func TestDirectWrites_GitTimeoutAtRefUpdate_Outcomes(t *testing.T) {
	t.Parallel()
	const putBody = "tenants:\n  t-a:\n    mysql_connections: \"60\"\n"
	endpoints := map[string]func(d *Deps) *httptest.ResponseRecorder{
		"PUT":   func(d *Deps) *httptest.ResponseRecorder { return wmPut(d, putBody, "") },
		"batch": func(d *Deps) *httptest.ResponseRecorder { return wmBatch(d, "") },
	}
	for ename, call := range endpoints {
		for _, outcome := range []string{"not committed", "committed", "unknown"} {
			t.Run(ename+"/"+outcome, func(t *testing.T) {
				t.Parallel()
				dir := seedGitTree(t, wmTree())
				base := gitHead(t, dir)
				saved := filepath.Join(t.TempDir(), "new")
				switch outcome {
				case "not committed":
					outcomeHook(t, dir, "prepared", ":")
				case "committed":
					outcomeHook(t, dir, "committed", ":")
				default:
					outcomeHook(t, dir, "committed", breakBranch(dir, saved))
				}
				wr := newTestWriter(dir)
				wr.SetGitTimeout(2 * time.Second)
				d := &Deps{Writer: wr, ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}
				rec := call(d)
				var env ErrorResponse
				_ = json.Unmarshal(rec.Body.Bytes(), &env)

				switch outcome {
				case "not committed":
					if rec.Code != http.StatusServiceUnavailable || env.Error != msgWriteCancelled {
						t.Fatalf("%d %s, want 503 %q", rec.Code, rec.Body.String(), msgWriteCancelled)
					}
					if head := gitHead(t, dir); head != base {
						t.Errorf("HEAD moved to %s", head)
					}
					if st, _ := exec.Command("git", "-C", dir, "status", "--porcelain").CombinedOutput(); len(st) != 0 {
						t.Errorf("tree changed: %s", st)
					}
				case "committed":
					if rec.Code != http.StatusOK {
						t.Fatalf("%d %s, want 200: the ref moved before git was killed", rec.Code, rec.Body.String())
					}
					if head := gitHead(t, dir); head == base {
						t.Fatal("HEAD did not move: the fault did not fire")
					}
				default:
					if rec.Code != http.StatusServiceUnavailable || env.Error != msgWriteOutcomeUnknown {
						t.Fatalf("%d %s, want 503 %q", rec.Code, rec.Body.String(), msgWriteOutcomeUnknown)
					}
					// The answer was honest: the branch did move.
					repairBranchAt(t, dir, saved)
					if head := gitHead(t, dir); head == base {
						t.Fatal("the branch did not move: the unknown variant did not fire as meant")
					}
					if ename == "batch" {
						// The marker was kept: recovery brings the serving
						// file up to the branch.
						if err := d.Writer.RecoverBatchPublish(); err != nil {
							t.Fatalf("RecoverBatchPublish: %v", err)
						}
						got, _ := os.ReadFile(filepath.Join(dir, "t-a.yaml"))
						if want := showMain(t, dir, "t-a.yaml"); string(got) != want || !strings.Contains(want, "_silent_mode") {
							t.Errorf("serving file = %q after recovery, want the branch's %q", got, want)
						}
					}
				}
			})
		}
	}
}
