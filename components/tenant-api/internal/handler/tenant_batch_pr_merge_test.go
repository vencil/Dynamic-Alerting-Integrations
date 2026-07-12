package handler

// #1097 self-review coverage: the PR-mode batch path builds a merge CLOSURE into
// each PRBatchOp (BatchTenants) and runs the REAL mergePatchYAML under
// WritePRBatch on a feature branch. The direct-path test drives the real merge
// but the gitops PR tests use a passthrough merge, so a wiring regression (wrong
// closure into PRBatchOp.Merge) would slip. This drives it end-to-end and
// inspects the committed branch content.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
)

func TestBatchTenants_PRMode_PreservesExistingKeys(t *testing.T) {
	configDir := t.TempDir()
	// _defaults so the whole merged doc validates; an existing multi-key tenant
	// file with a comment; both committed on main so WritePRBatch branches from them.
	if err := os.WriteFile(filepath.Join(configDir, "_defaults.yaml"),
		[]byte("defaults:\n  mysql_connections: 80\n  mysql_cpu: 90\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(configDir, "db-a.yaml"), []byte(existingTenantYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{
		{"init"}, {"config", "user.email", "t@t.com"}, {"config", "user.name", "T"},
		{"add", "."}, {"commit", "-m", "seed"}, {"branch", "-M", "main"},
	} {
		if out, err := exec.Command("git", append([]string{"-C", configDir}, args...)...).CombinedOutput(); err != nil {
			t.Skipf("git %v: %v\n%s", args, err, out)
		}
	}

	rbacMgr := adminRBAC(t)
	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, head string, labels []string) (*platform.PRInfo, error) {
			return &platform.PRInfo{Number: 7, WebURL: "https://example/pr/7", State: "open"}, nil
		},
	}
	h := BatchTenants(&Deps{
		Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: rbacMgr,
		WriteMode: WriteModePR, PRClient: mockClient, PRTracker: &mockPlatformTracker{},
	})

	body := `{"operations":[{"tenant_id":"db-a","patch":{"_silent_mode":"warning"}}]}`
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	rbacMgr.Middleware(rbac.PermRead, nil)(h).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.Status != "pending_review" {
		t.Fatalf("status = %q, want pending_review; body: %s", resp.Status, w.Body.String())
	}

	// Push failed (no origin, swallowed) → the batch branch is retained. Inspect
	// its committed db-a.yaml: the real merge must have preserved the other keys.
	branches, _ := exec.Command("git", "-C", configDir, "branch", "--format=%(refname:short)").Output()
	var branch string
	for _, ln := range strings.Split(string(branches), "\n") {
		if ln = strings.TrimSpace(ln); strings.HasPrefix(ln, "tenant-api/batch/") {
			branch = ln
			break
		}
	}
	if branch == "" {
		t.Fatalf("no tenant-api/batch/* branch found:\n%s", branches)
	}
	committed, err := exec.Command("git", "-C", configDir, "show", branch+":db-a.yaml").Output()
	if err != nil {
		t.Fatalf("git show %s:db-a.yaml: %v", branch, err)
	}
	got := string(committed)
	for _, want := range []string{"_silent_mode", "mysql_connections", "mysql_connections_critical", "mysql_cpu", "_metadata", "platform-db-team", "# warning threshold"} {
		if !strings.Contains(got, want) {
			t.Errorf("#1097 PR-mode wiring regression: committed branch file missing %q:\n%s", want, got)
		}
	}
}
