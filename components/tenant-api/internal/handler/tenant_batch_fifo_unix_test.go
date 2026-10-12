//go:build unix

package handler

// #2900: a direct-mode batch first copies the serving conf.d into its
// staging worktree. A special file there (a FIFO named stuck.yaml) fails that
// copy before any op is judged; the whole request is a 500 with the same
// fixed scan-failure message as PUT, never the file's name (#1700).

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

func TestBatchTenants_StagingScanFailureIsFixedInternalError(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "fifo-t.yaml"),
		[]byte("tenants:\n  fifo-t:\n    _silent_mode: \"warning\"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{
		{"init"}, {"config", "user.email", "t@t.com"}, {"config", "user.name", "T"},
		{"add", "."}, {"commit", "-m", "seed"}, {"branch", "-M", "main"},
	} {
		if out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).CombinedOutput(); err != nil {
			t.Skipf("git %v: %v\n%s", args, err, out)
		}
	}
	if err := syscall.Mkfifo(filepath.Join(dir, "stuck.yaml"), 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	head, _ := exec.Command("git", "-C", dir, "rev-parse", "HEAD").Output()

	rbacMgr := adminRBAC(t)
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: rbacMgr, WriteMode: WriteModeDirect}
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch",
		bytes.NewBufferString(`{"operations":[{"tenant_id":"fifo-t","patch":{"_silent_mode":"critical"}}]}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	rbacMgr.Middleware(rbac.PermRead, nil)(BatchTenants(d)).ServeHTTP(w, req)

	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500; body=%s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), "stuck") {
		t.Errorf("response names the special file: %s", w.Body.String())
	}
	var env ErrorResponse
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("unmarshal: %v; body=%s", err, w.Body.String())
	}
	if env.Code != CodeInternal || env.Error != msgTenantTreeScan {
		t.Errorf("envelope = (%q, %q), want (%s, %q)", env.Code, env.Error, CodeInternal, msgTenantTreeScan)
	}
	if after, _ := exec.Command("git", "-C", dir, "rev-parse", "HEAD").Output(); string(after) != string(head) {
		t.Error("HEAD moved")
	}
}
