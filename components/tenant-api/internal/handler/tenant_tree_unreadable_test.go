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

	"github.com/vencil/tenant-api/internal/rbac"
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

// assertTreeRefusal checks a 409 single-write envelope: the code, the count,
// and — only when visible — the entry's name.
func assertTreeRefusal(t *testing.T, w *httptest.ResponseRecorder, entry string, visible bool) {
	t.Helper()
	if w.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409: %s", w.Code, w.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("body: %v: %s", err, w.Body.String())
	}
	if body["code"] != CodeTenantTreeUnreadable {
		t.Errorf("code = %v, want %s", body["code"], CodeTenantTreeUnreadable)
	}
	if body["unreadable_count"] != float64(1) {
		t.Errorf("unreadable_count = %v, want 1", body["unreadable_count"])
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
