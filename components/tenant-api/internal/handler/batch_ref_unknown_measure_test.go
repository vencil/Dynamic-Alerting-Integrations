package handler

// #1535 item 6 — a pin of CURRENT, to-be-fixed behaviour (BUG(#3173)):
// what an async direct batch's task says when publishBatch's
// compare-and-swap (update-ref) fails AND HEAD cannot be read afterwards,
// so the writer cannot tell whether the branch moved. The fault is a
// reference-transaction hook on the serving branch's ref: it refuses the
// update in "prepared" (git exits 1); in the "moved" variant its "aborted"
// call then writes the new id into the branch ref itself (a ref that moved
// although git reported an error); both variants then point HEAD at a
// missing branch so `rev-parse HEAD` fails.
//
// In the "moved" variant the batch IS on the branch, yet the task error
// says "nothing was written". The assertions pin that text as it is today,
// NOT as it should be: the #3173 fix turns them red on purpose and updates
// them. Tenant ids are fixture names (CLAUDE.md #9).

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
func refFailThenHeadUnreadable(t *testing.T, repo string, moved bool) {
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
	for _, moved := range []bool{false, true} {
		name := "unmoved"
		if moved {
			name = "moved"
		}
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := seedGitTree(t, map[string]string{"_defaults.yaml": "defaults:\n  mysql_connections: 80\n", tn + ".yaml": orig})
			base := gitHead(t, dir)
			refFailThenHeadUnreadable(t, dir, moved)
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

			// HEAD was made unreadable by the hook: confirm, then repair it to
			// read where the branch is.
			if out, err := exec.Command("git", "-C", dir, "rev-parse", "HEAD").CombinedOutput(); err == nil {
				t.Fatalf("HEAD is readable (%s): the fault did not fire", out)
			}
			if err := os.WriteFile(filepath.Join(dir, ".git", "HEAD"), []byte("ref: refs/heads/main\n"), 0o644); err != nil {
				t.Fatal(err)
			}
			branch := gitHead(t, dir)
			committed, _ := exec.Command("git", "-C", dir, "show", "main:"+tn+".yaml").Output()
			serving, _ := os.ReadFile(filepath.Join(dir, tn+".yaml"))
			var results []string
			for _, r := range task.Results {
				b, _ := json.Marshal(r)
				results = append(results, string(b))
			}
			t.Logf("current behaviour (#3173) %s: task status=%q error=%q results=%v branch moved=%v serving file changed=%v",
				name, task.Status, task.Error, results, branch != base, string(serving) != orig)

			if task.Status != async.TaskFailed || !strings.HasPrefix(task.Error, "batch failed, nothing was written: ") {
				t.Fatalf("task = %q %q; current (#3173): failed, \"batch failed, nothing was written: …\"", task.Status, task.Error)
			}
			if moved {
				// BUG(#3173): the branch holds the batch, the task says nothing was written,
				// and the serving file was never brought up to the branch.
				if branch == base || !strings.Contains(string(committed), "_silent_mode") {
					t.Fatalf("the branch did not move (%s): the moved variant did not fire", branch)
				}
				if string(serving) != orig {
					t.Fatalf("serving file = %q; current (#3173): still the original", serving)
				}
			} else if branch != base || string(serving) != orig {
				t.Fatalf("branch moved or serving file changed in the unmoved variant")
			}
		})
	}
}
