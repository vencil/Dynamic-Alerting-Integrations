package handler

// _groups.yaml / _views.yaml are single shared objects: every write rewrites
// EVERY entry, not just the one named in the URL. These tests pin that the
// rewrite is computed from the copy on disk, read under the writer lock —
// not from the in-memory snapshot, which only refreshes on the Reload a
// failed write never reaches.
//
// The scenario is not hypothetical. commitFileChange writes the file BEFORE
// the git commit, so a write that fails at commit time leaves the new content
// on disk while the handler returns an error and skips Reload. The snapshot is
// then permanently behind, and the next edit to any OTHER entry used to delete
// the one that "failed" — silently, with a 200.

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/views"
)

const rebuildRBAC = `groups:
  - name: admins
    tenants: ["*"]
    permissions: [read, write, admin]
`

func rebuildRequest(t *testing.T, method, path, id, body string) *http.Request {
	t.Helper()
	req := newRequestWithChiParam(method, path, "id", id, bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	return req
}

func TestPutGroup_RebuildsFromDiskNotSnapshot(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	d := &Deps{
		Writer: gitops.NewWriter(configDir, configDir),
		Groups: groups.NewManager(configDir),
		RBAC:   newRBACManager(t, rebuildRBAC),
	}
	h := wrapWithRBACMiddleware(PutGroup(d), d.RBAC, rbac.PermWrite, nil)

	put := func(id string) int {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, rebuildRequest(t, "PUT", "/api/v1/groups/"+id, id,
			`{"label":"`+id+`","members":[]}`))
		return w.Code
	}

	if code := put("g-a"); code != http.StatusOK {
		t.Fatalf("first group: status = %d, want 200", code)
	}

	// Make the next commit fail after the file is already written, so the
	// handler errors out and never reloads its snapshot.
	installExternalCommitHook(t, configDir)
	if code := put("g-b"); code != http.StatusConflict {
		t.Fatalf("group written under an external commit: status = %d, want 409", code)
	}
	assertGroupIDs(t, configDir, "g-a", "g-b") // on disk despite the error

	// A later edit to an unrelated group must not resurrect the stale
	// snapshot's view of the world.
	if code := put("g-c"); code != http.StatusOK {
		t.Fatalf("third group: status = %d, want 200", code)
	}
	assertGroupIDs(t, configDir, "g-a", "g-b", "g-c")
}

func TestDeleteGroup_RemovesOnlyItsOwnEntry(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	d := &Deps{
		Writer: gitops.NewWriter(configDir, configDir),
		Groups: groups.NewManager(configDir),
		RBAC:   newRBACManager(t, rebuildRBAC),
	}
	put := wrapWithRBACMiddleware(PutGroup(d), d.RBAC, rbac.PermWrite, nil)
	del := wrapWithRBACMiddleware(DeleteGroup(d), d.RBAC, rbac.PermWrite, nil)

	for _, id := range []string{"g-a", "g-b"} {
		w := httptest.NewRecorder()
		put.ServeHTTP(w, rebuildRequest(t, "PUT", "/api/v1/groups/"+id, id,
			`{"label":"`+id+`","members":[]}`))
		if w.Code != http.StatusOK {
			t.Fatalf("seed %s: status = %d", id, w.Code)
		}
	}

	w := httptest.NewRecorder()
	del.ServeHTTP(w, rebuildRequest(t, "DELETE", "/api/v1/groups/g-a", "g-a", ""))
	if w.Code != http.StatusOK {
		t.Fatalf("delete: status = %d, want 200: %s", w.Code, w.Body.String())
	}
	assertGroupIDs(t, configDir, "g-b")
}

func TestPutView_RebuildsFromDiskNotSnapshot(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	d := &Deps{
		Writer: gitops.NewWriter(configDir, configDir),
		Views:  views.NewManager(configDir),
		RBAC:   newRBACManager(t, rebuildRBAC),
	}
	h := wrapWithRBACMiddleware(PutView(d), d.RBAC, rbac.PermWrite, nil)

	put := func(id string) int {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, rebuildRequest(t, "PUT", "/api/v1/views/"+id, id,
			`{"label":"`+id+`","filters":{"tier":"gold"}}`))
		return w.Code
	}

	if code := put("v-a"); code != http.StatusOK {
		t.Fatalf("first view: status = %d, want 200", code)
	}
	installExternalCommitHook(t, configDir)
	if code := put("v-b"); code != http.StatusConflict {
		t.Fatalf("view written under an external commit: status = %d, want 409", code)
	}
	if code := put("v-c"); code != http.StatusOK {
		t.Fatalf("third view: status = %d, want 200", code)
	}
	assertViewIDs(t, configDir, "v-a", "v-b", "v-c")
}

func assertGroupIDs(t *testing.T, configDir string, want ...string) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(configDir, "_groups.yaml"))
	if err != nil {
		t.Fatalf("read _groups.yaml: %v", err)
	}
	cfg, err := groups.ParseConfig(raw)
	if err != nil {
		t.Fatalf("parse _groups.yaml: %v", err)
	}
	assertKeySet(t, "_groups.yaml", keysOfGroups(cfg), want)
}

func assertViewIDs(t *testing.T, configDir string, want ...string) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(configDir, "_views.yaml"))
	if err != nil {
		t.Fatalf("read _views.yaml: %v", err)
	}
	cfg, err := views.ParseConfig(raw)
	if err != nil {
		t.Fatalf("parse _views.yaml: %v", err)
	}
	assertKeySet(t, "_views.yaml", keysOfViews(cfg), want)
}

func keysOfGroups(cfg *groups.GroupsConfig) map[string]bool {
	out := make(map[string]bool, len(cfg.Groups))
	for k := range cfg.Groups {
		out[k] = true
	}
	return out
}

func keysOfViews(cfg *views.ViewsConfig) map[string]bool {
	out := make(map[string]bool, len(cfg.Views))
	for k := range cfg.Views {
		out[k] = true
	}
	return out
}

func assertKeySet(t *testing.T, file string, got map[string]bool, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%s holds %d entries %v, want %d %v", file, len(got), got, len(want), want)
	}
	for _, id := range want {
		if !got[id] {
			t.Errorf("%s is missing %q (has %v)", file, id, got)
		}
	}
}
