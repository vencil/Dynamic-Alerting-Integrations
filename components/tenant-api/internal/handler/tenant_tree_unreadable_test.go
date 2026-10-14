package handler

// #2939, ADR-036 §4: a write whose tree-after holds an entry the route
// generator cannot read is refused — PUT 409 / batch 422 per op, code
// TENANT_TREE_UNREADABLE — and the entries are named only to a caller with
// write permission on all tenants (D3a). Portable shape: a dangling symlink.
// The special-file, read_error and walk_error shapes are in the _linux twin.

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/testutil"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// treeRBACYAML: "platform" writes every tenant (rbac.PlatformUnrestricted,
// sees entry names); "team" writes only tu-* (sees a count) and, with
// "readers", reads every tenant — the batch route's read gate on "*".
const treeRBACYAML = `groups:
  - name: platform
    tenants: ["*"]
    permissions: [read, write]
  - name: team
    tenants: ["tu-*"]
    permissions: [read, write]
  - name: readers
    tenants: ["*"]
    permissions: [read]
`

// treeFixture is a git-backed conf.d holding tu-a.yaml, on a direct-mode
// Deps with treeRBACYAML.
type treeFixture struct {
	dir string
	mgr *rbac.Manager
	d   *Deps
}

func newTreeFixture(t *testing.T) *treeFixture {
	t.Helper()
	dir := setupConfigDir(t, map[string]string{"tu-a.yaml": "tenants:\n  tu-a:\n    _silent_mode: \"warning\"\n"})
	initGitRepo(t, dir)
	mgr := newRBACManager(t, treeRBACYAML)
	return &treeFixture{dir: dir, mgr: mgr,
		d: &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: mgr, WriteMode: WriteModeDirect}}
}

// do serves h for group behind the RBAC middleware; tenantID "" is a
// non-tenant route (batch).
func (f *treeFixture) do(t *testing.T, h http.HandlerFunc, perm rbac.Permission, group, method, path, tenantID, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := newRequestWithChiParam(method, path, "id", tenantID, bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", group)
	if strings.HasPrefix(body, "{") {
		req.Header.Set("Content-Type", "application/json")
	}
	idFn := TenantIDFromPath
	if tenantID == "" {
		idFn = nil
	}
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, f.mgr, perm, idFn).ServeHTTP(w, req)
	return w
}

func (f *treeFixture) put(t *testing.T, group, tenantID string) *httptest.ResponseRecorder {
	return f.do(t, PutTenant(f.d), rbac.PermWrite, group, "PUT", "/api/v1/tenants/"+tenantID, tenantID,
		"tenants:\n  "+tenantID+":\n    _silent_mode: \"critical\"\n")
}

func (f *treeFixture) batch(t *testing.T, group, tenantID string) *httptest.ResponseRecorder {
	return f.do(t, BatchTenants(f.d), rbac.PermRead, group, "POST", "/api/v1/tenants/batch", "",
		`{"operations":[{"tenant_id":"`+tenantID+`","patch":{"_silent_mode":"critical"}}]}`)
}

func (f *treeFixture) head(t *testing.T) string {
	t.Helper()
	out, err := exec.Command("git", "-C", f.dir, "rev-parse", "HEAD").Output()
	if err != nil {
		t.Fatalf("git rev-parse: %v", err)
	}
	return strings.TrimSpace(string(out))
}

// assertTreeRefusal checks a 409 TENANT_TREE_UNREADABLE single-write
// envelope with one entry (assertTreeRefusalAs).
func assertTreeRefusal(t *testing.T, w *httptest.ResponseRecorder, entry string, visible bool) {
	t.Helper()
	assertTreeRefusalAs(t, w, http.StatusConflict, CodeTenantTreeUnreadable, 1, entry, visible)
}

// assertTreeRefusalAs checks a whole-tree refusal envelope: the status, the
// code, the count, and — only when visible — the entry's name.
func assertTreeRefusalAs(t *testing.T, w *httptest.ResponseRecorder, status int, code string, count int, entry string, visible bool) {
	t.Helper()
	if w.Code != status {
		t.Fatalf("status = %d, want %d: %s", w.Code, status, w.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("body: %v: %s", err, w.Body.String())
	}
	if body["code"] != code {
		t.Errorf("code = %v, want %s", body["code"], code)
	}
	if body["unreadable_count"] != float64(count) {
		t.Errorf("unreadable_count = %v, want %d", body["unreadable_count"], count)
	}
	if got := strings.Contains(w.Body.String(), entry); got != visible {
		t.Errorf("body names %s = %v, want %v: %s", entry, got, visible, w.Body.String())
	}
	if _, has := body["unreadable"]; has != visible {
		t.Errorf("unreadable list present = %v, want %v", has, visible)
	}
}

// assertBatchTreeRefusal checks a 422 batch whose only op carries the code,
// naming the entry only when visible.
func assertBatchTreeRefusal(t *testing.T, w *httptest.ResponseRecorder, entry string, visible bool) {
	t.Helper()
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 422: %s", w.Code, w.Body.String())
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("body: %v: %s", err, w.Body.String())
	}
	if resp.Status != batchStatusRejected || len(resp.Results) != 1 {
		t.Fatalf("batch = %+v, want rejected with one result", resp)
	}
	r := resp.Results[0]
	if r.Status != "error" || r.Code != CodeTenantTreeUnreadable {
		t.Errorf("result = %+v, want error %s", r, CodeTenantTreeUnreadable)
	}
	if !strings.Contains(r.Message, msgTenantTreeUnreadable) || !strings.Contains(r.Message, "(unreadable entries: 1)") {
		t.Errorf("message = %q, want the fixed text with the count", r.Message)
	}
	if got := strings.Contains(w.Body.String(), entry); got != visible {
		t.Errorf("body names %s = %v, want %v: %s", entry, got, visible, w.Body.String())
	}
}

// (c) + (f): a dangling symlink elsewhere refuses PUT (409) and a batch
// (422, the op's code), nothing is written, and only the platform-wide
// writer is told which entry.
func TestTreeUnreadable_DanglingElsewhere_PutAndBatchByCaller(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		group   string
		visible bool
	}{{"platform", true}, {"team,readers", false}} {
		t.Run(c.group, func(t *testing.T) {
			t.Parallel()
			f := newTreeFixture(t)
			if err := os.Symlink("missing-target.yaml", filepath.Join(f.dir, "gone.yaml")); err != nil {
				t.Skipf("symlink: %v", err)
			}
			head := f.head(t)
			assertTreeRefusal(t, f.put(t, c.group, "tu-a"), "gone.yaml", c.visible)
			assertBatchTreeRefusal(t, f.batch(t, c.group, "tu-a"), "gone.yaml", c.visible)
			if got := f.head(t); got != head {
				t.Errorf("HEAD moved %s → %s", head, got)
			}
		})
	}
}

// (a) ADR-036 §4's repair path through the API: PUT over a tenant file that
// is a dangling symlink succeeds, and the tree after it is readable.
func TestTreeUnreadable_PutOverDanglingTargetRepairsTheTree(t *testing.T) {
	t.Parallel()
	f := newTreeFixture(t)
	target := filepath.Join(f.dir, "tu-b.yaml")
	if err := os.Symlink("missing-target.yaml", target); err != nil {
		t.Skipf("symlink: %v", err)
	}
	if w := f.put(t, "team", "tu-b"); w.Code != http.StatusOK {
		t.Fatalf("PUT over the dangling tenant file = %d, want 200: %s", w.Code, w.Body.String())
	}
	if fi, err := os.Lstat(target); err != nil || !fi.Mode().IsRegular() {
		t.Fatalf("tu-b.yaml after PUT: %v, %v; want a regular file", fi, err)
	}
	scan, err := cfg.ScanDirTree(f.dir, nil, nil, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	if len(scan.Unreadable) != 0 {
		t.Errorf("tree after the repair still has unreadable entries: %v", scan.Unreadable)
	}
}

// (e): validate and diff, the previews of PUT, give PUT's verdict on the
// same tree — refused with the same text the caller's PUT gets (and the
// same disclosure), or accepted.
func TestTreeUnreadable_PreviewsGivePutsVerdict(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		name, link, tenant string
		refused            bool
	}{
		{"elsewhere", "gone.yaml", "tu-a", true},
		{"target", "tu-c.yaml", "tu-c", false},
	} {
		for _, group := range []string{"platform", "team,readers"} {
			t.Run(c.name+"/"+group, func(t *testing.T) {
				t.Parallel()
				f := newTreeFixture(t)
				if err := os.Symlink("missing-target.yaml", filepath.Join(f.dir, c.link)); err != nil {
					t.Skipf("symlink: %v", err)
				}
				body := "tenants:\n  " + c.tenant + ":\n    _silent_mode: \"critical\"\n"
				vw := f.do(t, ValidateTenant(f.d), rbac.PermRead, group, "POST", "/api/v1/tenants/"+c.tenant+"/validate", c.tenant, body)
				var vr ValidateResponse
				if err := json.Unmarshal(vw.Body.Bytes(), &vr); err != nil || vw.Code != http.StatusOK {
					t.Fatalf("validate = %d %s (%v)", vw.Code, vw.Body.String(), err)
				}
				dw := f.do(t, DiffTenant(f.d), rbac.PermRead, group, "POST", "/api/v1/tenants/"+c.tenant+"/diff", c.tenant, body)
				pw := f.put(t, group, c.tenant) // last: an accepted PUT changes the tree
				if !c.refused {
					if !vr.Valid || dw.Code != http.StatusOK || pw.Code != http.StatusOK {
						t.Fatalf("want all accepted: validate %+v, diff %d %s, put %d %s",
							vr, dw.Code, dw.Body.String(), pw.Code, pw.Body.String())
					}
					return
				}
				var pe ErrorResponse
				if err := json.Unmarshal(pw.Body.Bytes(), &pe); err != nil || pw.Code != http.StatusConflict {
					t.Fatalf("put = %d %s (%v), want 409", pw.Code, pw.Body.String(), err)
				}
				if vr.Valid || len(vr.Warnings) != 1 || vr.Warnings[0] != pe.Error {
					t.Errorf("validate = %+v, want invalid with PUT's text %q", vr, pe.Error)
				}
				var de ErrorResponse
				if err := json.Unmarshal(dw.Body.Bytes(), &de); err != nil || dw.Code != http.StatusConflict ||
					de.Code != CodeTenantTreeUnreadable || de.Error != pe.Error {
					t.Errorf("diff = %d %s, want PUT's 409 %s %q", dw.Code, dw.Body.String(), CodeTenantTreeUnreadable, pe.Error)
				}
			})
		}
	}
}

// D3a for an async batch: the task's results are stored and served to every
// poller, who is not the submitter — so they never name the entries, even
// when the submitter could see them; the poller sees the code and the count.
func TestTreeUnreadable_AsyncBatchStoresNoEntryNames(t *testing.T) {
	t.Parallel()
	f := newTreeFixture(t)
	if err := os.Symlink("missing-target.yaml", filepath.Join(f.dir, "secret-other-team-file.yaml")); err != nil {
		t.Skipf("symlink: %v", err)
	}
	tasks := async.NewManager(1)
	defer tasks.Close()
	f.d.Tasks = tasks
	w := f.do(t, BatchTenants(f.d), rbac.PermRead, "platform", "POST", "/api/v1/tenants/batch?async=true", "",
		`{"operations":[{"tenant_id":"tu-a","patch":{"_silent_mode":"critical"}}]}`)
	if w.Code != http.StatusAccepted {
		t.Fatalf("async submit = %d, want 202: %s", w.Code, w.Body.String())
	}
	var sub map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &sub); err != nil {
		t.Fatal(err)
	}
	taskID, _ := sub["task_id"].(string)
	if final := pollUntilTerminal(t, tasks, taskID, 10*time.Second); final.Status != async.TaskFailed {
		t.Fatalf("task = %+v, want failed (rejected)", final)
	}
	for _, poller := range []string{"platform", "team,readers"} {
		got := f.do(t, GetTask(f.d), rbac.PermRead, poller, "GET", "/api/v1/tasks/"+taskID, taskID, "")
		if got.Code != http.StatusOK || !strings.Contains(got.Body.String(), CodeTenantTreeUnreadable) ||
			!strings.Contains(got.Body.String(), "(unreadable entries: 1)") {
			t.Fatalf("%s polls: %d %s, want 200 with %s and the count", poller, got.Code, got.Body.String(), CodeTenantTreeUnreadable)
		}
		if strings.Contains(got.Body.String(), "secret-other-team-file") {
			t.Errorf("%s polls: the stored result names the entry: %s", poller, got.Body.String())
		}
	}
}

// The group batch form of TestTreeUnreadable_AsyncBatchStoresNoEntryNames:
// POST /groups/{id}/batch?async=true runs the same pipeline
// (executeBatchOps, batchResultsStored), so its stored results name no
// entry either, for any poller.
func TestTreeUnreadable_AsyncGroupBatchStoresNoEntryNames(t *testing.T) {
	t.Parallel()
	f := newTreeFixture(t)
	setupGroupsFile(t, f.dir, "groups:\n  tu-team:\n    label: TU\n    members:\n      - tu-a\n")
	if err := os.Symlink("missing-target.yaml", filepath.Join(f.dir, "secret-other-team-file.yaml")); err != nil {
		t.Skipf("symlink: %v", err)
	}
	tasks := async.NewManager(1)
	defer tasks.Close()
	f.d.Tasks = tasks
	f.d.Groups = groups.NewDirManager(f.dir)
	f.d.State = mustDirState(f.dir)
	w := f.do(t, GroupBatch(f.d), rbac.PermRead, "platform", "POST", "/api/v1/groups/tu-team/batch?async=true", "tu-team",
		`{"patch":{"_silent_mode":"critical"}}`)
	if w.Code != http.StatusAccepted {
		t.Fatalf("async submit = %d, want 202: %s", w.Code, w.Body.String())
	}
	var sub map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &sub); err != nil {
		t.Fatal(err)
	}
	taskID, _ := sub["task_id"].(string)
	if final := pollUntilTerminal(t, tasks, taskID, 10*time.Second); final.Status != async.TaskFailed {
		t.Fatalf("task = %+v, want failed (rejected)", final)
	}
	for _, poller := range []string{"platform", "team,readers"} {
		got := f.do(t, GetTask(f.d), rbac.PermRead, poller, "GET", "/api/v1/tasks/"+taskID, taskID, "")
		if got.Code != http.StatusOK || !strings.Contains(got.Body.String(), CodeTenantTreeUnreadable) ||
			!strings.Contains(got.Body.String(), "(unreadable entries: 1)") {
			t.Fatalf("%s polls: %d %s, want 200 with %s and the count", poller, got.Code, got.Body.String(), CodeTenantTreeUnreadable)
		}
		if strings.Contains(got.Body.String(), "secret-other-team-file") {
			t.Errorf("%s polls: the stored result names the entry: %s", poller, got.Body.String())
		}
	}
}

// R4-N2: an unresolvable symlink — no content, recorded by git as 120000 —
// elsewhere is git-fixable whatever makes it unresolvable: a loop or a path
// through a non-directory answers like a dangling link, 409 on PUT and
// /diff and 422 per op on a batch.
func TestTreeUnreadable_UnresolvableLinksElsewhereAre409(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ name, link, target string }{
		{"loop", "loop.yaml", "loop.yaml"},
		{"enotdir", "notdir.yaml", "tu-a.yaml/below.yaml"},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			f := newTreeFixture(t)
			if err := os.Symlink(c.target, filepath.Join(f.dir, c.link)); err != nil {
				t.Skipf("symlink: %v", err)
			}
			assertTreeRefusal(t, f.put(t, "platform", "tu-a"), c.link, true)
			assertTreeRefusal(t, f.do(t, DiffTenant(f.d), rbac.PermRead, "platform", "POST", "/api/v1/tenants/tu-a/diff", "tu-a",
				"tenants:\n  tu-a:\n    _silent_mode: \"critical\"\n"), c.link, true)
			assertBatchTreeRefusal(t, f.batch(t, "platform", "tu-a"), c.link, true)
		})
	}
}

// R4-N2: the written tenant file being a self-loop (`tu-l.yaml -> tu-l.yaml`)
// is the repair path, as a dangling one: PUT goes through, for a scoped
// caller too, and leaves a regular file and a readable tree.
func TestTreeUnreadable_PutOverSelfLoopTargetRepairsTheTree(t *testing.T) {
	t.Parallel()
	f := newTreeFixture(t)
	target := filepath.Join(f.dir, "tu-l.yaml")
	if err := os.Symlink("tu-l.yaml", target); err != nil {
		t.Skipf("symlink: %v", err)
	}
	if w := f.put(t, "team,readers", "tu-l"); w.Code != http.StatusOK {
		t.Fatalf("PUT over the self-loop tenant file = %d, want 200: %s", w.Code, w.Body.String())
	}
	if fi, err := os.Lstat(target); err != nil || !fi.Mode().IsRegular() {
		t.Fatalf("tu-l.yaml after PUT: %v, %v; want a regular file", fi, err)
	}
	scan, err := cfg.ScanDirTree(f.dir, nil, nil, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	if len(scan.Unreadable) != 0 {
		t.Errorf("tree after the repair still has unreadable entries: %v", scan.Unreadable)
	}
}

// R4-N2: a symlink whose stat fails with EACCES is not unresolvable — git
// cannot fix a permission on the server — so it is a store fault (500).
// Under euid 0 the permission bits never refuse the stat, so the shape
// cannot be built there: skipped, saying so.
func TestTreeUnreadable_EACCESLinkElsewhereIsStoreFault(t *testing.T) {
	t.Parallel()
	if os.Geteuid() == 0 {
		t.Skip("euid 0: a mode-000 directory does not refuse root's stat, so no EACCES symlink can be built")
	}
	f := newTreeFixture(t)
	locked := t.TempDir()
	testutil.WriteYAML(t, locked, "x.yaml", "tenants: {}\n")
	if err := os.Chmod(locked, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(locked, 0o755) })
	if err := os.Symlink(filepath.Join(locked, "x.yaml"), filepath.Join(f.dir, "denied.yaml")); err != nil {
		t.Fatal(err)
	}
	assertTreeRefusalAs(t, f.put(t, "platform", "tu-a"), http.StatusInternalServerError, CodeTenantStoreFault, 1, "denied.yaml", true)
}
