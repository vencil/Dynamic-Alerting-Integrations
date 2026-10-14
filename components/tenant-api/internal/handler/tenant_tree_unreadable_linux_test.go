//go:build linux

package handler

// #2939 (b) (d) and read_error through the handlers: a FIFO named like a
// config file, a symlink to a file whose read fails (/proc/self/mem: EIO
// even for root) and a directory past PATH_MAX (testutil.UnlistableDeepDir:
// unlistable even for root) are store faults (D3c-2): each refuses PUT and
// fails a batch whole with 500 TENANT_STORE_FAULT — at once, nothing
// written, the entry named only to the platform-wide writer. The shapes that
// fail for root are why this is Linux only. Seams: none.

import (
	"encoding/json"
	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/rbac"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/testutil"
)

func TestTreeUnreadable_LinuxShapes_PutAndBatch(t *testing.T) {
	t.Parallel()
	for _, shape := range []struct {
		name  string
		build func(t *testing.T, dir string) string // returns the entry's conf.d-relative name
	}{
		{"fifo", func(t *testing.T, dir string) string {
			p := filepath.Join(dir, "stuck.yaml")
			if err := syscall.Mkfifo(p, 0o644); err != nil {
				t.Skipf("mkfifo: %v", err)
			}
			t.Cleanup(func() { // release a reader a failing run left on it
				if f, err := os.OpenFile(p, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
					_ = f.Close()
				}
			})
			return "stuck.yaml"
		}},
		{"read_error", func(t *testing.T, dir string) string {
			mem, ok := testutil.ProcSelfMem()
			if !ok {
				t.Skip("no /proc/self/mem that fails to read")
			}
			if err := os.Symlink(mem, filepath.Join(dir, "eio.yaml")); err != nil {
				t.Fatal(err)
			}
			return "eio.yaml"
		}},
		{"walk_error", func(t *testing.T, dir string) string {
			return testutil.UnlistableDeepDir(t, dir, 100)
		}},
	} {
		for _, group := range []string{"platform", "team,readers"} {
			t.Run(shape.name+"/"+group, func(t *testing.T) {
				t.Parallel()
				f := newTreeFixture(t)
				entry := shape.build(t, f.dir)
				visible := group == "platform"
				head := f.head(t)
				start := time.Now()
				assertTreeRefusalAs(t, f.put(t, group, "tu-a"), http.StatusInternalServerError, CodeTenantStoreFault, 1, entry, visible)
				assertTreeRefusalAs(t, f.batch(t, group, "tu-a"), http.StatusInternalServerError, CodeTenantStoreFault, 1, entry, visible)
				if took := time.Since(start); took > 10*time.Second {
					t.Errorf("answered after %v: a walk or read blocked", took)
				}
				if got := f.head(t); got != head {
					t.Errorf("HEAD moved %s → %s", head, got)
				}
			})
		}
	}
}

// D3c-1 through the handlers: the written tenant file itself is a read_error
// or a FIFO → 409 TENANT_CONFIG_NOT_LOADABLE for the platform-wide writer
// and a scoped one alike (the file is never replaced), on PUT, on a batch
// op (422), and as /validate's text.
func TestTreeUnreadable_UnreadableTargetNotLoadableForEveryCaller(t *testing.T) {
	t.Parallel()
	for _, shape := range []struct{ name, configError string }{{"read_error", "unreadable"}, {"fifo", "not_regular_file"}} {
		for _, group := range []string{"platform", "team,readers"} {
			t.Run(shape.name+"/"+group, func(t *testing.T) {
				t.Parallel()
				f := newTreeFixture(t)
				target := filepath.Join(f.dir, "tu-x.yaml")
				switch shape.name {
				case "read_error":
					mem, ok := testutil.ProcSelfMem()
					if !ok {
						t.Skip("no /proc/self/mem that fails to read")
					}
					if err := os.Symlink(mem, target); err != nil {
						t.Fatal(err)
					}
				case "fifo":
					if err := syscall.Mkfifo(target, 0o644); err != nil {
						t.Skipf("mkfifo: %v", err)
					}
					t.Cleanup(func() {
						if fh, err := os.OpenFile(target, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
							_ = fh.Close()
						}
					})
				}
				head := f.head(t)
				pw := f.put(t, group, "tu-x")
				var pe map[string]any
				if err := json.Unmarshal(pw.Body.Bytes(), &pe); err != nil || pw.Code != http.StatusConflict ||
					pe["code"] != CodeTenantConfigNotLoadable || pe["config_error"] != shape.configError {
					t.Fatalf("PUT = %d %s, want 409 %s config_error %s", pw.Code, pw.Body.String(), CodeTenantConfigNotLoadable, shape.configError)
				}
				bw := f.batch(t, group, "tu-x")
				var br BatchResponse
				if err := json.Unmarshal(bw.Body.Bytes(), &br); err != nil || bw.Code != http.StatusUnprocessableEntity ||
					len(br.Results) != 1 || br.Results[0].Code != CodeTenantConfigNotLoadable {
					t.Fatalf("batch = %d %s, want 422 with the op %s", bw.Code, bw.Body.String(), CodeTenantConfigNotLoadable)
				}
				vw := f.do(t, ValidateTenant(f.d), rbac.PermRead, group, "POST", "/api/v1/tenants/tu-x/validate", "tu-x",
					"tenants:\n  tu-x:\n    _silent_mode: \"critical\"\n")
				var vr ValidateResponse
				if err := json.Unmarshal(vw.Body.Bytes(), &vr); err != nil || vr.Valid || len(vr.Warnings) != 1 || vr.Warnings[0] != pe["error"] {
					t.Errorf("validate = %s, want invalid with PUT's text %q", vw.Body.String(), pe["error"])
				}
				if strings.Contains(pw.Body.String()+bw.Body.String(), f.dir) {
					t.Errorf("a response names the server path")
				}
				if got := f.head(t); got != head {
					t.Errorf("HEAD moved %s → %s", head, got)
				}
			})
		}
	}
}

// D3c-2: a store fault and a dangling link elsewhere at once — PUT and the
// batch answer 500 TENANT_STORE_FAULT (the server is repaired first),
// counting both entries.
func TestTreeUnreadable_StoreFaultWinsOverDanglingLink_Handlers(t *testing.T) {
	t.Parallel()
	f := newTreeFixture(t)
	if err := os.Symlink("missing-target.yaml", filepath.Join(f.dir, "gone.yaml")); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Mkfifo(filepath.Join(f.dir, "stuck.yaml"), 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	assertTreeRefusalAs(t, f.put(t, "platform", "tu-a"), http.StatusInternalServerError, CodeTenantStoreFault, 2, "stuck.yaml", true)
	assertTreeRefusalAs(t, f.batch(t, "team,readers", "tu-a"), http.StatusInternalServerError, CodeTenantStoreFault, 2, "stuck.yaml", false)
}

// D3a for a store fault in an async batch: the task's stored results carry
// the code and the count, never the entry, whoever submitted or polls.
func TestTreeUnreadable_AsyncStoreFaultStoresNoEntryNames(t *testing.T) {
	t.Parallel()
	f := newTreeFixture(t)
	if err := syscall.Mkfifo(filepath.Join(f.dir, "secret-other-team-fifo.yaml"), 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	tasks := async.NewManager(1)
	defer tasks.Close()
	f.d.Tasks = tasks
	w := f.do(t, BatchTenants(f.d), rbac.PermRead, "platform", "POST", "/api/v1/tenants/batch?async=true", "",
		`{"operations":[{"tenant_id":"tu-a","patch":{"_silent_mode":"critical"}}]}`)
	var sub map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &sub); err != nil || w.Code != http.StatusAccepted {
		t.Fatalf("async submit = %d %s", w.Code, w.Body.String())
	}
	taskID, _ := sub["task_id"].(string)
	if final := pollUntilTerminal(t, tasks, taskID, 10*time.Second); final.Status != async.TaskFailed {
		t.Fatalf("task = %+v, want failed", final)
	}
	for _, poller := range []string{"platform", "team,readers"} {
		got := f.do(t, GetTask(f.d), rbac.PermRead, poller, "GET", "/api/v1/tasks/"+taskID, taskID, "")
		body := got.Body.String()
		if got.Code != http.StatusOK || !strings.Contains(body, CodeTenantStoreFault) || !strings.Contains(body, "(unreadable entries: 1)") {
			t.Fatalf("%s polls: %d %s, want 200 with %s and the count", poller, got.Code, body, CodeTenantStoreFault)
		}
		if strings.Contains(body, "secret-other-team-fifo") {
			t.Errorf("%s polls: the stored result names the entry: %s", poller, body)
		}
	}
}

// tenant_api_store_unreadable_entries renders the Writer's scrape-time
// walk: with no write, a FIFO counts as special_file 1, and nothing counts
// the dangling link.
func TestStoreUnreadableMetrics_RenderTheScrapeWalk(t *testing.T) {
	t.Parallel()
	f := newTreeFixture(t)
	if err := syscall.Mkfifo(filepath.Join(f.dir, "stuck.yaml"), 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	if err := os.Symlink("missing-target.yaml", filepath.Join(f.dir, "gone.yaml")); err != nil {
		t.Fatal(err)
	}
	var out strings.Builder
	writeStoreUnreadableMetricsFrom(&out, f.d.Writer)
	for _, line := range []string{
		`tenant_api_store_unreadable_entries{reason="special_file"} 1`,
		`tenant_api_store_unreadable_entries{reason="read_error"} 0`,
		`tenant_api_store_unreadable_entries{reason="walk_error"} 0`,
		`tenant_api_store_unreadable_entries{reason="stat_error"} 0`,
	} {
		if !strings.Contains(out.String(), line) {
			t.Errorf("missing %q in:\n%s", line, out.String())
		}
	}
}

// R4-N1 + R4-N3: the written file is a read_error AND a FIFO sits elsewhere.
// The store fault wins over the file's own unreadability, and PUT, /diff and
// PUT …/custom-alerts answer the one tree identically: 500
// TENANT_STORE_FAULT with the same text and fields.
func TestTreeUnreadable_StoreFaultWinsOverUnreadableTarget_AllEndpoints(t *testing.T) {
	t.Parallel()
	mem, ok := testutil.ProcSelfMem()
	if !ok {
		t.Skip("no /proc/self/mem that fails to read")
	}
	f := newTreeFixture(t)
	if err := os.Symlink(mem, filepath.Join(f.dir, "tu-x.yaml")); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Mkfifo(filepath.Join(f.dir, "stuck.yaml"), 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	body := "tenants:\n  tu-x:\n    _silent_mode: \"critical\"\n"
	responses := map[string]*httptest.ResponseRecorder{
		"PUT":           f.put(t, "platform", "tu-x"),
		"diff":          f.do(t, DiffTenant(f.d), rbac.PermRead, "platform", "POST", "/api/v1/tenants/tu-x/diff", "tu-x", body),
		"custom-alerts": f.do(t, PutTenantCustomAlerts(f.d), rbac.PermWrite, "platform", "PUT", "/api/v1/tenants/tu-x/custom-alerts", "tu-x", `{"custom_alerts":[],"base_hash":"0000000000000000"}`),
	}
	var want map[string]any
	for name, w := range responses {
		assertTreeRefusalAs(t, w, http.StatusInternalServerError, CodeTenantStoreFault, 1, "stuck.yaml", true)
		var got map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
			t.Fatal(err)
		}
		delete(got, "request_id")
		if want == nil {
			want = got
		} else if !reflect.DeepEqual(got, want) {
			t.Errorf("%s answers differently:\n got  %v\n want %v", name, got, want)
		}
	}
}
