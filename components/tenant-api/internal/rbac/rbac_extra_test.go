package rbac

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// --- NewManager tests ---

func TestNewManager_EmptyPath(t *testing.T) {
	m, err := NewManager("")
	if err != nil {
		t.Fatalf("NewManager('') returned error: %v", err)
	}
	cfg := m.Get()
	if len(cfg.Groups) != 0 {
		t.Errorf("expected empty groups in open mode, got %d", len(cfg.Groups))
	}
}

func TestNewManager_FileNotFound(t *testing.T) {
	m, err := NewManager("/nonexistent/path/_rbac.yaml")
	if err != nil {
		t.Fatalf("NewManager(nonexistent) returned error: %v", err)
	}
	// Should fall back to open-read mode
	cfg := m.Get()
	if len(cfg.Groups) != 0 {
		t.Errorf("expected empty groups for missing file, got %d", len(cfg.Groups))
	}
}

func TestNewManager_ValidFile(t *testing.T) {
	dir := t.TempDir()
	rbacFile := filepath.Join(dir, "_rbac.yaml")
	content := `groups:
  - name: admins
    tenants: ["*"]
    permissions: [admin]
  - name: db-ops
    tenants: ["db-a-*", "db-b-*"]
    permissions: [read, write]
`
	if err := os.WriteFile(rbacFile, []byte(content), 0644); err != nil {
		t.Fatalf("write: %v", err)
	}

	m, err := NewManager(rbacFile)
	if err != nil {
		t.Fatalf("NewManager returned error: %v", err)
	}
	cfg := m.Get()
	if len(cfg.Groups) != 2 {
		t.Errorf("expected 2 groups, got %d", len(cfg.Groups))
	}
}

func TestNewManager_InvalidYAML(t *testing.T) {
	dir := t.TempDir()
	rbacFile := filepath.Join(dir, "_rbac.yaml")
	if err := os.WriteFile(rbacFile, []byte("{{not valid yaml"), 0644); err != nil {
		t.Fatalf("write: %v", err)
	}

	_, err := NewManager(rbacFile)
	if err == nil {
		t.Error("expected error for invalid YAML, got nil")
	}
}

// --- Load / hot-reload tests ---

func TestLoad_NoChangeSkipsUpdate(t *testing.T) {
	dir := t.TempDir()
	rbacFile := filepath.Join(dir, "_rbac.yaml")
	content := `groups:
  - name: admins
    tenants: ["*"]
    permissions: [admin]
`
	if err := os.WriteFile(rbacFile, []byte(content), 0644); err != nil {
		t.Fatalf("write: %v", err)
	}

	m, err := NewManager(rbacFile)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	// Second load should be a no-op (same hash)
	hashBefore := m.lastHash
	if err := m.load(); err != nil {
		t.Fatalf("load: %v", err)
	}
	if m.lastHash != hashBefore {
		t.Error("hash changed on reload of unchanged file")
	}
}

func TestLoad_DetectsChange(t *testing.T) {
	dir := t.TempDir()
	rbacFile := filepath.Join(dir, "_rbac.yaml")
	content1 := `groups:
  - name: admins
    tenants: ["*"]
    permissions: [admin]
`
	if err := os.WriteFile(rbacFile, []byte(content1), 0644); err != nil {
		t.Fatalf("write: %v", err)
	}

	m, err := NewManager(rbacFile)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	if len(m.Get().Groups) != 1 {
		t.Fatalf("expected 1 group initially, got %d", len(m.Get().Groups))
	}

	// Modify file
	content2 := `groups:
  - name: admins
    tenants: ["*"]
    permissions: [admin]
  - name: viewers
    tenants: ["*"]
    permissions: [read]
`
	if err := os.WriteFile(rbacFile, []byte(content2), 0644); err != nil {
		t.Fatalf("write: %v", err)
	}

	if err := m.load(); err != nil {
		t.Fatalf("load after change: %v", err)
	}

	if len(m.Get().Groups) != 2 {
		t.Errorf("expected 2 groups after reload, got %d", len(m.Get().Groups))
	}
}

func TestLoad_DeletedFile(t *testing.T) {
	dir := t.TempDir()
	rbacFile := filepath.Join(dir, "_rbac.yaml")
	content := `groups:
  - name: admins
    tenants: ["*"]
    permissions: [admin]
`
	if err := os.WriteFile(rbacFile, []byte(content), 0644); err != nil {
		t.Fatalf("write: %v", err)
	}

	m, err := NewManager(rbacFile)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	// Delete file
	if err := os.Remove(rbacFile); err != nil {
		t.Fatalf("remove: %v", err)
	}

	// Reload should fall back to open mode
	if err := m.load(); err != nil {
		t.Fatalf("load after delete: %v", err)
	}
	if len(m.Get().Groups) != 0 {
		t.Errorf("expected 0 groups after file deletion, got %d", len(m.Get().Groups))
	}
}

// --- WatchLoop tests ---

func TestWatchLoop_EmptyPath(t *testing.T) {
	m := &Manager{path: ""}
	m.value.Store(&RBACConfig{})

	stopCh := make(chan struct{})
	done := make(chan struct{})
	go func() {
		m.WatchLoop(10*time.Millisecond, stopCh)
		close(done)
	}()
	// WatchLoop should return immediately for empty path
	close(stopCh)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Error("WatchLoop didn't exit")
	}
}

func TestWatchLoop_StopsOnClose(t *testing.T) {
	dir := t.TempDir()
	rbacFile := filepath.Join(dir, "_rbac.yaml")
	if err := os.WriteFile(rbacFile, []byte("groups: []\n"), 0644); err != nil {
		t.Fatalf("write: %v", err)
	}

	m, err := NewManager(rbacFile)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	stopCh := make(chan struct{})
	done := make(chan struct{})
	go func() {
		m.WatchLoop(10*time.Millisecond, stopCh)
		close(done)
	}()

	// Let it tick a couple times
	time.Sleep(30 * time.Millisecond)
	close(stopCh)

	select {
	case <-done:
	case <-time.After(time.Second):
		t.Error("WatchLoop didn't stop")
	}
}

// --- Get tests ---

func TestGet_NilValue(t *testing.T) {
	m := &Manager{}
	// value is never set — Get should return empty config
	cfg := m.Get()
	if cfg == nil {
		t.Fatal("Get returned nil")
	}
	if len(cfg.Groups) != 0 {
		t.Errorf("expected 0 groups from nil value, got %d", len(cfg.Groups))
	}
}

// --- HasPermission extended tests ---

func TestHasPermission_MultipleGroups(t *testing.T) {
	m := &Manager{}
	m.value.Store(&RBACConfig{
		Groups: []GroupRule{
			{Name: "team-a", Tenants: []string{"db-a"}, Permissions: []Permission{PermRead}},
			{Name: "team-b", Tenants: []string{"db-b"}, Permissions: []Permission{PermWrite}},
		},
	})

	// User in both groups
	if !m.HasPermission([]string{"team-a", "team-b"}, "db-a", PermRead) {
		t.Error("expected read on db-a via team-a")
	}
	if !m.HasPermission([]string{"team-a", "team-b"}, "db-b", PermWrite) {
		t.Error("expected write on db-b via team-b")
	}
	if m.HasPermission([]string{"team-a"}, "db-b", PermRead) {
		t.Error("team-a should not have access to db-b")
	}
}

func TestHasPermission_EmptyGroups(t *testing.T) {
	m := &Manager{}
	m.value.Store(&RBACConfig{
		Groups: []GroupRule{
			{Name: "admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
		},
	})

	if m.HasPermission(nil, "db-a", PermRead) {
		t.Error("nil groups should be denied")
	}
	if m.HasPermission([]string{}, "db-a", PermRead) {
		t.Error("empty groups should be denied")
	}
}

func TestPermCovers_UnknownPermission(t *testing.T) {
	if permCovers(PermAdmin, Permission("unknown")) {
		t.Error("admin should not cover unknown permission")
	}
}

// --- Middleware tests ---

func TestMiddleware_MissingEmail(t *testing.T) {
	m := &Manager{}
	m.value.Store(&RBACConfig{})

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermRead, nil)(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	// No X-Forwarded-Email header
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("Middleware status = %d, want %d", w.Code, http.StatusUnauthorized)
	}
}

func TestMiddleware_OpenModeAllowsRead(t *testing.T) {
	m := &Manager{}
	m.value.Store(&RBACConfig{}) // empty = open mode

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		email := RequestEmail(r)
		if email != "test@example.com" {
			t.Errorf("RequestEmail = %q, want %q", email, "test@example.com")
		}
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermRead, nil)(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("Middleware open-mode read status = %d, want %d", w.Code, http.StatusOK)
	}
}

func TestMiddleware_OpenModeDeniesWrite(t *testing.T) {
	m := &Manager{}
	m.value.Store(&RBACConfig{}) // empty = open mode

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermWrite, nil)(inner)

	req := httptest.NewRequest("PUT", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Errorf("Middleware open-mode write status = %d, want %d", w.Code, http.StatusForbidden)
	}
}

func TestMiddleware_WithTenantIDFn(t *testing.T) {
	m := &Manager{}
	m.value.Store(&RBACConfig{
		Groups: []GroupRule{
			{Name: "db-ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}},
		},
	})

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	tenantFn := func(r *http.Request) string { return "db-a" }
	mw := m.Middleware(PermWrite, tenantFn)(inner)

	req := httptest.NewRequest("PUT", "/api/v1/tenants/db-a", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "db-ops")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("Middleware with tenantFn status = %d, want %d", w.Code, http.StatusOK)
	}
}

func TestMiddleware_DeniedForWrongTenant(t *testing.T) {
	m := &Manager{}
	m.value.Store(&RBACConfig{
		Groups: []GroupRule{
			{Name: "db-ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}},
		},
	})

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	tenantFn := func(r *http.Request) string { return "db-b" }
	mw := m.Middleware(PermWrite, tenantFn)(inner)

	req := httptest.NewRequest("PUT", "/api/v1/tenants/db-b", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "db-ops")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusForbidden {
		t.Errorf("Middleware wrong tenant status = %d, want %d", w.Code, http.StatusForbidden)
	}
}

func TestMiddleware_GroupsParsing(t *testing.T) {
	m := &Manager{}
	m.value.Store(&RBACConfig{
		Groups: []GroupRule{
			{Name: "team-b", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		},
	})

	var gotGroups []string
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotGroups = RequestGroups(r)
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermRead, nil)(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "team-a, team-b, team-c")
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", w.Code, http.StatusOK)
	}
	if len(gotGroups) != 3 {
		t.Fatalf("expected 3 groups, got %d: %v", len(gotGroups), gotGroups)
	}
	if gotGroups[0] != "team-a" || gotGroups[1] != "team-b" || gotGroups[2] != "team-c" {
		t.Errorf("unexpected groups: %v", gotGroups)
	}
}

func TestMiddleware_EmptyGroups(t *testing.T) {
	m := &Manager{}
	m.value.Store(&RBACConfig{}) // open mode

	var gotGroups []string
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotGroups = RequestGroups(r)
		w.WriteHeader(http.StatusOK)
	})
	mw := m.Middleware(PermRead, nil)(inner)

	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	// No X-Forwarded-Groups header
	w := httptest.NewRecorder()
	mw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", w.Code, http.StatusOK)
	}
	// Empty header should result in no groups (all empty strings filtered out)
	if len(gotGroups) != 0 {
		t.Errorf("expected 0 groups from empty header, got %d: %v", len(gotGroups), gotGroups)
	}
}

// --- Context helpers tests ---

func TestRequestEmail_NoContext(t *testing.T) {
	req := httptest.NewRequest("GET", "/", nil)
	email := RequestEmail(req)
	if email != "" {
		t.Errorf("expected empty email, got %q", email)
	}
}

func TestRequestGroups_NoContext(t *testing.T) {
	req := httptest.NewRequest("GET", "/", nil)
	groups := RequestGroups(req)
	if groups != nil {
		t.Errorf("expected nil groups, got %v", groups)
	}
}

// --- writeError tests ---

func TestWriteError(t *testing.T) {
	w := httptest.NewRecorder()
	writeError(w, http.StatusForbidden, "access denied")

	if w.Code != http.StatusForbidden {
		t.Errorf("status = %d, want %d", w.Code, http.StatusForbidden)
	}
	if ct := w.Header().Get("Content-Type"); ct != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
}
