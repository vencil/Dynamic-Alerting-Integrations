package handler

// #1535 item 6 / #3173 P2: what an async direct batch's task says when
// publishBatch's compare-and-swap (update-ref) fails AND HEAD cannot be read
// afterwards. The fault is a reference-transaction hook on the serving
// branch's ref: it refuses the update in "prepared" (git exits 1); in the
// "moved" variants its "aborted" call then writes the new id into the branch
// ref itself (a ref that moved although git reported an error); every
// variant then points HEAD at a missing branch so `rev-parse HEAD` fails,
// and the "branch unreadable" one also overwrites the branch ref with
// garbage.
//
// FIXED(#3173): the writer reads the branch ref directly when HEAD cannot be
// read, so a moved branch is a published batch (the task completes, the
// serving file is brought up) and an unmoved one is "nothing was written".
// Only when the branch cannot be read either is the task's error "may have
// been committed" — never "nothing was written" — and the publish marker
// stays, so recovery brings the serving file up once the branch reads again.
// Before the fix every variant said "batch failed, nothing was written".
// Tenant ids are fixture names (CLAUDE.md #9).

import (
	"bytes"
	"encoding/json"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/rbac"
)

// refFailThenHeadUnreadable installs the hook described in the file comment.
// With branchToo it saves the new id to saved and garbles the branch ref.
func refFailThenHeadUnreadable(t *testing.T, repo string, moved, branchToo bool, saved string) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("sh hooks; run on Linux")
	}
	hook := filepath.Join(repo, ".git", "hooks", "reference-transaction")
	if err := os.MkdirAll(filepath.Dir(hook), 0o755); err != nil {
		t.Fatal(err)
	}
	head := filepath.Join(repo, ".git", "HEAD")
	mainRef := filepath.Join(repo, ".git", "refs", "heads", "main")
	onAborted := "printf 'ref: refs/heads/zz-unreadable\\n' > '" + head + "'"
	if moved {
		onAborted = "printf '%s\\n' \"$new\" > '" + mainRef + "'; " + onAborted
	}
	if branchToo {
		onAborted = "printf '%s\\n' \"$new\" > '" + saved + "'; " + onAborted +
			"; printf 'garbage\\n' > '" + mainRef + "'"
	}
	script := "#!/bin/sh\n" +
		"in=$(cat)\n" +
		"line=$(printf '%s\\n' \"$in\" | grep ' refs/heads/main$')\n" +
		"[ -z \"$line\" ] && exit 0\n" +
		"new=$(printf '%s\\n' \"$line\" | cut -d' ' -f2)\n" +
		"case \"$1\" in\n" +
		"  prepared) exit 1 ;;\n" +
		"  aborted) " + onAborted + " ;;\n" +
		"esac\n" +
		"exit 0\n"
	if err := os.WriteFile(hook, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
}

func TestAsyncBatch_RefStateUnknown_TaskErrorText(t *testing.T) {
	t.Parallel()
	const tn = "tn-win-a"
	const orig = "tenants:\n  " + tn + ":\n    mysql_connections: \"70\"\n"
	for _, tc := range []struct {
		name             string
		moved, branchToo bool
	}{
		{"unmoved", false, false},
		{"moved", true, false},
		{"moved, branch unreadable", true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := seedGitTree(t, map[string]string{"_defaults.yaml": "defaults:\n  mysql_connections: 80\n", tn + ".yaml": orig})
			base := gitHead(t, dir)
			saved := filepath.Join(t.TempDir(), "new")
			refFailThenHeadUnreadable(t, dir, tc.moved, tc.branchToo, saved)
			tasks := async.NewManager(1)
			defer func() { _ = tasks.Close() }()
			d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect,
				Tasks: tasks, WriteTimeoutDur: 30 * time.Second}
			req := httptest.NewRequest("POST", "/api/v1/tenants/batch?async=true",
				bytes.NewBufferString(`{"operations":[{"tenant_id":"`+tn+`","patch":{"_silent_mode":"warning"}}]}`))
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("X-Forwarded-Email", "alice@example.com")
			req.Header.Set("X-Forwarded-Groups", "admins")
			w := httptest.NewRecorder()
			d.RBAC.Middleware(rbac.PermRead, nil)(BatchTenants(d)).ServeHTTP(w, req)
			var acc struct {
				TaskID string `json:"task_id"`
			}
			if err := json.Unmarshal(w.Body.Bytes(), &acc); err != nil || acc.TaskID == "" {
				t.Fatalf("no task id: %d %s", w.Code, w.Body.String())
			}
			task := pollUntilTerminal(t, tasks, acc.TaskID, 20*time.Second)

			// HEAD was made unreadable by the hook: confirm, then repair it
			// (and the branch ref) to read where the branch is.
			if out, err := exec.Command("git", "-C", dir, "rev-parse", "HEAD").CombinedOutput(); err == nil {
				t.Fatalf("HEAD is readable (%s): the fault did not fire", out)
			}
			if tc.branchToo {
				moved, err := os.ReadFile(saved)
				if err != nil {
					t.Fatalf("the branch fault did not fire: %v", err)
				}
				if err := os.WriteFile(filepath.Join(dir, ".git", "refs", "heads", "main"), moved, 0o644); err != nil {
					t.Fatal(err)
				}
			}
			if err := os.WriteFile(filepath.Join(dir, ".git", "HEAD"), []byte("ref: refs/heads/main\n"), 0o644); err != nil {
				t.Fatal(err)
			}
			branch := gitHead(t, dir)
			committed, _ := exec.Command("git", "-C", dir, "show", "main:"+tn+".yaml").Output()
			serving := func() string {
				b, _ := os.ReadFile(filepath.Join(dir, tn+".yaml"))
				return string(b)
			}
			if (branch != base) != tc.moved {
				t.Fatalf("branch moved = %v, want %v: the fault did not fire as meant", branch != base, tc.moved)
			}
			if tc.moved && !strings.Contains(string(committed), "_silent_mode") {
				t.Fatalf("the branch holds %q, not the batch", committed)
			}

			switch {
			case tc.branchToo:
				if task.Status != async.TaskFailed || task.Error != msgWriteOutcomeUnknown {
					t.Fatalf("task = %q %q, want failed with exactly %q", task.Status, task.Error, msgWriteOutcomeUnknown)
				}
				if strings.Contains(task.Error, "nothing was written") {
					t.Errorf("task error claims nothing was written: %q", task.Error)
				}
				for _, r := range task.Results {
					if r.Message != msgWriteOutcomeUnknown {
						t.Errorf("op result %+v, want message %q", r, msgWriteOutcomeUnknown)
					}
				}
				// The marker was kept: the next write's recovery brings the
				// serving file up to the branch the batch did reach.
				if serving() != orig {
					t.Fatalf("serving file changed before recovery: %q", serving())
				}
				if err := d.Writer.RecoverBatchPublish(); err != nil {
					t.Fatalf("RecoverBatchPublish: %v", err)
				}
				if got := serving(); got != string(committed) {
					t.Errorf("serving file = %q after recovery, want the branch's %q", got, committed)
				}
			case tc.moved:
				// Read through the branch ref: published, and the task says so.
				if task.Status != async.TaskCompleted || task.Error != "" {
					t.Fatalf("task = %q %q, want completed", task.Status, task.Error)
				}
				if got := serving(); got != string(committed) {
					t.Errorf("serving file = %q, want the branch's %q", got, committed)
				}
			default:
				// The branch did not move: nothing was written, and it says so.
				if task.Status != async.TaskFailed || !strings.HasPrefix(task.Error, "batch failed, nothing was written: ") {
					t.Fatalf("task = %q %q, want failed, \"batch failed, nothing was written: …\"", task.Status, task.Error)
				}
				if serving() != orig {
					t.Errorf("serving file changed: %q", serving())
				}
			}
		})
	}
}
