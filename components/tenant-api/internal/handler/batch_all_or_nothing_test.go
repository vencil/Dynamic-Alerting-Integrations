package handler

// #2900 (ADR-036 §4 "批次"): a batch is one request, one commit, all or
// nothing — on POST /tenants/batch and POST /groups/{id}/batch, in direct and
// PR write-back mode, sync and ?async=true (which PR mode answers
// synchronously). Each cell of the matrix checks the repository, not the
// response alone:
//
//   - every op passes  → exactly ONE commit carrying every changed file
//     (direct: on main; PR: on the batch branch, main untouched);
//   - one op fails     → ZERO commits, no batch branch, every file as it was
//     and the worktree clean;
//   - a group member that does not exist is "skipped", which does not fail
//     the batch (#2882/#2910).
//
// The failing op is either refused before anything is written (an id the
// tenant-id rule refuses) or refused under the writer lock, after an earlier
// op of the same batch was already merged (a member file threshold-exporter
// cannot load), so both refusal points are covered.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
)

const aonBroken = "tenants: [\n" // a file the batch merge refuses (TENANT_CONFIG_NOT_LOADABLE)

// aonTree: two healthy tenants, one tenant file that cannot be loaded, and
// groups over them. g-ok: two healthy members; g-bad: a healthy member, then
// the broken one; g-skip: a healthy member and one with no file at all.
func aonTree() map[string]string {
	return map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"t-a.yaml":       "tenants:\n  t-a:\n    mysql_connections: \"70\"\n",
		"t-b.yaml":       "tenants:\n  t-b:\n    mysql_connections: \"75\"\n",
		"t-bad.yaml":     aonBroken,
		"_groups.yaml": "groups:\n" +
			"  g-ok:\n    label: OK\n    members: [t-a, t-b]\n" +
			"  g-bad:\n    label: Bad\n    members: [t-a, t-bad]\n" +
			"  g-skip:\n    label: Skip\n    members: [t-a, t-gone]\n",
	}
}

type aonFixture struct {
	dir   string
	deps  *Deps
	tasks *async.Manager
	prs   int
	// lastBody is the raw response of the last run (async: the finished
	// task as JSON), for tests that check what a response must not say.
	lastBody string
}

func newAONFixture(t *testing.T, mode WriteMode) *aonFixture {
	t.Helper()
	f := &aonFixture{dir: seedGitTree(t, aonTree())}
	f.tasks = async.NewManager(1)
	t.Cleanup(func() { _ = f.tasks.Close() })
	f.deps = &Deps{State: mustDirState(f.dir), Writer: newTestWriter(f.dir), ConfigDir: f.dir, RBAC: adminRBAC(t),
		Groups: groups.NewDirManager(f.dir), WriteMode: mode, Tasks: f.tasks}
	if mode == WriteModePR {
		f.deps.PRClient = &mockPlatformClient{
			providerName: "github",
			createPRFunc: func(title, body, head string, labels []string) (*platform.PRInfo, error) {
				f.prs++
				return &platform.PRInfo{Number: 7, WebURL: "https://example/pr/7", State: "open"}, nil
			},
		}
		f.deps.PRTracker = &mockPlatformTracker{}
	}
	return f
}

func (f *aonFixture) git(t *testing.T, args ...string) string {
	t.Helper()
	out, err := exec.Command("git", append([]string{"-C", f.dir}, args...)...).CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

// batchBranches lists the PR-mode batch branches (the push to the missing
// origin fails, so a created branch stays local).
func (f *aonFixture) batchBranches(t *testing.T) []string {
	t.Helper()
	var out []string
	for _, b := range strings.Fields(f.git(t, "branch", "--format=%(refname:short)")) {
		if strings.HasPrefix(b, platform.BranchPrefix) {
			out = append(out, b)
		}
	}
	return out
}

func (f *aonFixture) snapshot(t *testing.T) map[string]string {
	t.Helper()
	out := map[string]string{}
	for name := range aonTree() {
		b, _ := os.ReadFile(filepath.Join(f.dir, name))
		out[name] = string(b)
	}
	return out
}

// aonRun posts the request and returns the HTTP status and the per-op
// results — for ?async=true in direct mode, the finished task's.
func (f *aonFixture) run(t *testing.T, group, body string, async_ bool) (int, []BatchResult, *async.Task) {
	t.Helper()
	path := "/api/v1/tenants/batch"
	h := BatchTenants(f.deps)
	var req *http.Request
	if group != "" {
		path = "/api/v1/groups/" + group + "/batch"
		h = GroupBatch(f.deps)
	}
	if async_ {
		path += "?async=true"
	}
	if group != "" {
		req = newRequestWithChiParam("POST", path, "id", group, bytes.NewBufferString(body))
	} else {
		req = httptest.NewRequest("POST", path, bytes.NewBufferString(body))
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	f.deps.RBAC.Middleware(rbac.PermRead, nil)(h).ServeHTTP(w, req)

	if w.Code == http.StatusAccepted {
		var acc struct {
			TaskID string `json:"task_id"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &acc); err != nil || acc.TaskID == "" {
			t.Fatalf("202 without task_id: %s", w.Body.String())
		}
		task := pollUntilTerminal(t, f.tasks, acc.TaskID, 20*time.Second)
		if raw, err := json.Marshal(task); err == nil {
			f.lastBody = string(raw)
		}
		res := make([]BatchResult, len(task.Results))
		for i, r := range task.Results {
			res[i] = BatchResult{TenantID: r.TenantID, Status: r.Status, Message: r.Message, Code: r.Code}
		}
		return w.Code, res, task
	}
	f.lastBody = w.Body.String()
	var resp struct {
		Results []BatchResult `json:"results"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	return w.Code, resp.Results, nil
}

func sortedLines(s string) []string {
	out := strings.Fields(s)
	sort.Strings(out)
	return out
}

func TestBatch_AllOrNothing_Matrix(t *testing.T) {
	type endpoint struct {
		name, group string
		pass        string   // body whose every op passes
		passFiles   []string // the files the passing batch changes
		fail        string   // body with one failing op
		failCode    int      // HTTP status of the failing batch (PR mode, sync)
	}
	endpoints := []endpoint{
		{name: "tenant/refused before the write", group: "",
			pass:      `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}},{"tenant_id":"t-b","patch":{"_silent_mode":"critical"}}]}`,
			passFiles: []string{"t-a.yaml", "t-b.yaml"},
			fail:      `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}},{"tenant_id":"Bad_Id","patch":{"_silent_mode":"warning"}}]}`,
			failCode:  http.StatusUnprocessableEntity},
		{name: "tenant/refused under the writer lock", group: "",
			pass:      `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}},{"tenant_id":"t-a","patch":{"mysql_connections":"71"}},{"tenant_id":"t-b","patch":{"_silent_mode":"critical"}}]}`,
			passFiles: []string{"t-a.yaml", "t-b.yaml"},
			fail:      `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}},{"tenant_id":"t-bad","patch":{"_silent_mode":"warning"}}]}`,
			failCode:  http.StatusUnprocessableEntity},
		{name: "group", group: "g-ok",
			pass:      `{"patch":{"_silent_mode":"warning"}}`,
			passFiles: []string{"t-a.yaml", "t-b.yaml"},
			fail:      `{"patch":{"_silent_mode":"warning"}}`, // posted to g-bad
			failCode:  http.StatusUnprocessableEntity},
	}
	for _, mode := range []WriteMode{WriteModeDirect, WriteModePR} {
		for _, async_ := range []bool{false, true} {
			for _, ep := range endpoints {
				name := string(mode) + "/sync/" + ep.name
				if async_ {
					name = string(mode) + "/async/" + ep.name
				}
				// The task is async only in direct mode: PR mode answers
				// ?async=true synchronously.
				isTask := async_ && mode == WriteModeDirect

				t.Run(name+"/all pass: one commit", func(t *testing.T) {
					f := newAONFixture(t, mode)
					mainBefore := f.git(t, "rev-parse", "main")
					code, results, task := f.run(t, ep.group, ep.pass, async_)
					want := "ok"
					if mode == WriteModePR {
						want = "included"
					}
					for _, r := range results {
						if r.Status != want {
							t.Fatalf("result %+v, want %s (status %d)", r, want, code)
						}
					}
					if isTask && task.Status != async.TaskCompleted {
						t.Fatalf("task = %+v, want completed", task)
					}
					if !isTask && code != http.StatusOK {
						t.Fatalf("status = %d, want 200", code)
					}
					var commits, files string
					switch mode {
					case WriteModeDirect:
						commits = f.git(t, "rev-list", "--count", mainBefore+"..main")
						files = f.git(t, "show", "--name-only", "--format=", "main")
					case WriteModePR:
						if got := f.git(t, "rev-parse", "main"); got != mainBefore {
							t.Fatalf("PR mode moved main %s → %s", mainBefore, got)
						}
						br := f.batchBranches(t)
						if len(br) != 1 || f.prs != 1 {
							t.Fatalf("batch branches %v, PRs %d; want one of each", br, f.prs)
						}
						commits = f.git(t, "rev-list", "--count", "main.."+br[0])
						files = f.git(t, "show", "--name-only", "--format=", br[0])
					}
					if commits != "1" {
						t.Errorf("commits = %s, want exactly 1 for the whole batch", commits)
					}
					if got := sortedLines(files); strings.Join(got, ",") != strings.Join(ep.passFiles, ",") {
						t.Errorf("the commit carries %v, want %v", got, ep.passFiles)
					}
				})

				t.Run(name+"/one fails: zero commits, nothing changed", func(t *testing.T) {
					f := newAONFixture(t, mode)
					mainBefore := f.git(t, "rev-parse", "main")
					before := f.snapshot(t)
					group := ep.group
					if group != "" {
						group = "g-bad"
					}
					code, results, task := f.run(t, group, ep.fail, async_)
					switch {
					case isTask:
						if task.Status != async.TaskFailed || task.Error == "" {
							t.Errorf("task = %+v, want failed with an error", task)
						}
					case mode == WriteModeDirect:
						if code != http.StatusUnprocessableEntity {
							t.Errorf("status = %d, want 422", code)
						}
					default:
						if code != ep.failCode {
							t.Errorf("status = %d, want %d", code, ep.failCode)
						}
					}
					// Where per-op results are returned, they name the
					// refused op and mark the others not applied.
					if mode == WriteModeDirect || ep.failCode == http.StatusUnprocessableEntity {
						got := statuses(results)
						if got != "not_applied,error" {
							t.Errorf("statuses = %s, want not_applied,error: %+v", got, results)
						}
					}
					if got := f.git(t, "rev-parse", "main"); got != mainBefore {
						t.Errorf("main moved %s → %s: a failed batch committed", mainBefore, got)
					}
					if br := f.batchBranches(t); len(br) != 0 || f.prs != 0 {
						t.Errorf("batch branches %v, PRs %d after a failed batch; want none", br, f.prs)
					}
					if after := f.snapshot(t); !mapsEqual(before, after) {
						t.Errorf("a failed batch changed files:\nbefore %q\nafter  %q", before, after)
					}
					if st := f.git(t, "status", "--porcelain", "--untracked-files=all"); st != "" {
						t.Errorf("worktree not clean after a failed batch:\n%s", st)
					}
				})
			}

			t.Run(string(mode)+"/"+map[bool]string{false: "sync", true: "async"}[async_]+"/group/skipped member: still one commit", func(t *testing.T) {
				f := newAONFixture(t, mode)
				mainBefore := f.git(t, "rev-parse", "main")
				_, results, task := f.run(t, "g-skip", `{"patch":{"_silent_mode":"warning"}}`, async_)
				if task != nil && task.Status != async.TaskCompleted {
					t.Fatalf("task = %+v, want completed", task)
				}
				ok := "ok"
				if mode == WriteModePR {
					ok = "included"
				}
				if got := statuses(results); got != ok+",skipped" {
					t.Fatalf("statuses = %s, want %s,skipped: %+v", got, ok, results)
				}
				ref := "main"
				if mode == WriteModePR {
					br := f.batchBranches(t)
					if len(br) != 1 {
						t.Fatalf("batch branches %v, want one", br)
					}
					ref = br[0]
				}
				if n := f.git(t, "rev-list", "--count", mainBefore+".."+ref); n != "1" {
					t.Errorf("commits = %s, want 1", n)
				}
				if files := f.git(t, "show", "--name-only", "--format=", ref); files != "t-a.yaml" {
					t.Errorf("the commit carries %q, want t-a.yaml only", files)
				}
				if _, err := os.Stat(filepath.Join(f.dir, "t-gone.yaml")); err == nil {
					t.Error("the skipped member was created")
				}
			})
		}
	}
}

func mapsEqual(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}

// #2900 F4: every op is judged, in both modes — a batch with two refused ops
// reports both (PR mode used to stop at the first one refused on the fresh
// base), with the op between them not applied.
func TestBatch_AllOrNothing_EveryRefusalReported(t *testing.T) {
	body := `{"operations":[{"tenant_id":"t-bad","patch":{"_silent_mode":"warning"}},` +
		`{"tenant_id":"t-a","patch":{"_silent_mode":"warning"}},` +
		`{"tenant_id":"t-bad","patch":{"_silent_mode":"critical"}}]}`
	for _, mode := range []WriteMode{WriteModeDirect, WriteModePR} {
		f := newAONFixture(t, mode)
		code, results, _ := f.run(t, "", body, false)
		if code != http.StatusUnprocessableEntity || statuses(results) != "error,not_applied,error" {
			t.Errorf("%s: status %d, results %+v; want 422 error,not_applied,error", mode, code, results)
		}
	}
}
