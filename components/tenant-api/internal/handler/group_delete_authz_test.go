package handler

// DeleteGroup authorizes against the group AS STORED, not the in-memory
// snapshot.
//
// DELETE is the one group operation whose target is the stored object rather
// than something the request supplies, so the member-permission gate and the
// thing being destroyed have to be read from the same bytes. The snapshot is
// not those bytes: groupMgr has no WatchLoop, and it only refreshes on a
// successful write's Reload — so once the file moves underneath it (a write
// that failed after writeFileAtomic, or a concurrent write), it stays wrong.
//
// The middleware here is mounted with PermRead purely to populate the verified
// principal; production mounts DELETE with PermWrite (cmd/server/routes.go).
// This test is about the handler's per-member gate, which is a separate,
// finer-grained check than the route's platform-scope one.

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
)

// partialWriterRBAC: read everywhere (so the route middleware admits the
// caller and attaches a principal), write on exactly one tenant.
const partialWriterRBAC = `groups:
  - name: readers
    tenants: ["*"]
    permissions: [read]
  - name: owners
    tenants: ["t-owned"]
    permissions: [write]
`

func TestDeleteGroup_AuthorizesStoredMembersNotSnapshot(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	path := filepath.Join(configDir, "_groups.yaml")

	writeAndCommit := func(content, msg string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
		runGit(t, configDir, "add", "_groups.yaml")
		runGit(t, configDir, "commit", "-m", msg)
	}

	// The snapshot is loaded from a version whose only member the caller owns.
	writeAndCommit("groups:\n  g-x:\n    label: gx\n    members:\n      - t-owned\n", "seed")
	gm := groups.NewManager(configDir)

	// The file then gains a member the caller may NOT write. The snapshot has
	// no way to learn this.
	writeAndCommit("groups:\n  g-x:\n    label: gx\n    members:\n      - t-owned\n      - t-foreign\n", "drift")

	rbacMgr := newRBACManager(t, partialWriterRBAC)
	d := &Deps{Writer: gitops.NewWriter(configDir, configDir), Groups: gm, RBAC: rbacMgr}
	h := wrapWithRBACMiddleware(DeleteGroup(d), rbacMgr, rbac.PermRead, nil)

	req := newRequestWithChiParam("DELETE", "/api/v1/groups/g-x", "id", "g-x", bytes.NewBuffer(nil))
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "readers,owners")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403 — the stored group holds a member the caller cannot write: %s",
			w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "t-foreign") {
		t.Errorf("403 body should name the offending member, got: %s", w.Body.String())
	}

	// And the group must still be there.
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	cfg, err := groups.ParseConfig(raw)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if _, present := cfg.Groups["g-x"]; !present {
		t.Error("the refused delete removed the group anyway")
	}
}

// The control: when the stored member set IS fully writable, the delete goes
// through. Without this, the test above would also pass if the handler simply
// 403'd everything.
func TestDeleteGroup_StoredMembersAllWritableDeletes(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	path := filepath.Join(configDir, "_groups.yaml")

	if err := os.WriteFile(path, []byte("groups:\n  g-x:\n    label: gx\n    members:\n      - t-owned\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runGit(t, configDir, "add", "_groups.yaml")
	runGit(t, configDir, "commit", "-m", "seed")

	rbacMgr := newRBACManager(t, partialWriterRBAC)
	d := &Deps{
		Writer: gitops.NewWriter(configDir, configDir),
		Groups: groups.NewManager(configDir),
		RBAC:   rbacMgr,
	}
	h := wrapWithRBACMiddleware(DeleteGroup(d), rbacMgr, rbac.PermRead, nil)

	req := newRequestWithChiParam("DELETE", "/api/v1/groups/g-x", "id", "g-x", bytes.NewBuffer(nil))
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "readers,owners")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", w.Code, w.Body.String())
	}
	raw, _ := os.ReadFile(path)
	cfg, err := groups.ParseConfig(raw)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if _, present := cfg.Groups["g-x"]; present {
		t.Error("group survived a permitted delete")
	}
}
