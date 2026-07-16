package rbac

// SHA-256 hot-reload lifecycle of the rbac config: no-op reload on an
// unchanged file, change detection, file deletion, the WatchLoop goroutine
// lifecycle, the canonical empty-config shape, and — the safety core — a
// failed reload keeping the last-good snapshot for EVERY row of the shared
// invalidConfigTable (config_load_test.go).

import (
	"os"
	"strings"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/testutil"
)

// --- Load / hot-reload tests ---

func TestLoad_NoChangeSkipsUpdate(t *testing.T) {
	t.Parallel()
	content := `groups:
  - name: admins
    tenants: ["*"]
    permissions: [admin]
`
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", content)

	m, err := NewManager(rbacFile, nil)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	// Second load should be a no-op (same hash)
	hashBefore := m.LastHash()
	if err := m.Reload(); err != nil {
		t.Fatalf("load: %v", err)
	}
	if m.LastHash() != hashBefore {
		t.Error("hash changed on reload of unchanged file")
	}
}

func TestLoad_DetectsChange(t *testing.T) {
	t.Parallel()
	content1 := `groups:
  - name: admins
    tenants: ["*"]
    permissions: [admin]
`
	dir, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", content1)

	m, err := NewManager(rbacFile, nil)
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
	testutil.WriteYAML(t, dir, "_rbac.yaml", content2)

	if err := m.Reload(); err != nil {
		t.Fatalf("load after change: %v", err)
	}

	if len(m.Get().Groups) != 2 {
		t.Errorf("expected 2 groups after reload, got %d", len(m.Get().Groups))
	}
}

func TestLoad_DeletedFile(t *testing.T) {
	t.Parallel()
	content := `groups:
  - name: admins
    tenants: ["*"]
    permissions: [admin]
`
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", content)

	m, err := NewManager(rbacFile, nil)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}

	// Delete file
	if err := os.Remove(rbacFile); err != nil {
		t.Fatalf("remove: %v", err)
	}

	// Reload after delete → empty config (this asserts the config STATE only;
	// a manager built from a configured path now fails closed on empty groups
	// per ADR-027 MED-8, not open — permission behavior is covered in
	// empty_config_mode_test.go).
	if err := m.Reload(); err != nil {
		t.Fatalf("load after delete: %v", err)
	}
	if len(m.Get().Groups) != 0 {
		t.Errorf("expected 0 groups after file deletion, got %d", len(m.Get().Groups))
	}
}

// Hot-reload failures keep the last-good snapshot: Reload returns the error
// (WatchLoop logs it as a WARN) and Get still serves the previous config —
// verified for EVERY invalidConfigTable row (strict-parse typos, null/empty
// match blocks, undeclared claim and org-scope keys, malformed tenant
// patterns, unparseable YAML).
func TestReload_InvalidKeepsLastGood(t *testing.T) {
	t.Parallel()
	granted := &VerifiedPrincipal{Groups: []string{"operators"}, Claims: map[string]string{"org": "ORG-A"}}

	for _, tc := range invalidConfigTable {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", matchLoadYAML)
			m, err := NewManager(rbacFile, invalidTableDeclared)
			if err != nil {
				t.Fatalf("NewManager: %v", err)
			}
			if !m.Allowed(granted, "any-tenant", PermWrite) {
				t.Fatal("precondition failed: initial config must grant write")
			}

			if err := os.WriteFile(rbacFile, []byte(tc.yaml), 0o600); err != nil {
				t.Fatalf("write bad config: %v", err)
			}
			rerr := m.Reload()
			if rerr == nil {
				t.Fatal("Reload = nil, want an error for the invalid config")
			}
			if tc.wantErr != "" && !strings.Contains(rerr.Error(), tc.wantErr) {
				t.Errorf("Reload error = %v, want substring %q", rerr, tc.wantErr)
			}
			// Last-good is still served: same rule count, same decision.
			if got := len(m.Get().Groups); got != 1 {
				t.Errorf("Groups after failed reload = %d, want 1 (last-good)", got)
			}
			if !m.Allowed(granted, "any-tenant", PermWrite) {
				t.Error("Allowed after failed reload = false, want true (last-good must keep serving)")
			}
		})
	}
}

// TestReload_UndeclaredOrgScopeKeepsLastGoodOrgEvaluation is the org-scoped
// twin of the "undeclared org-scope key" row above: TestReload_InvalidKeepsLastGood
// starts from a match-rule baseline and probes last-good via Allowed (the
// group-match path), so it never exercises the org-scope evaluation path.
// This test keeps the original guarantee (formerly the fifth
// TestNewManager_OrgScopeValidation subtest): the baseline itself is an
// org-scoped rule, and after the failed reload the last-good snapshot must
// still grant through ScopeAllowed — the claim-key mapping + allowedOrgs
// evaluation, not just the rule count.
func TestReload_UndeclaredOrgScopeKeepsLastGoodOrgEvaluation(t *testing.T) {
	t.Parallel()
	declared := map[string]string{"org": "X-Auth-Request-Org"}
	orgScopedYAML := "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n    org-scope: org\n"

	_, path := testutil.MkTempYAML(t, "_rbac.yaml", orgScopedYAML)
	m, err := NewManager(path, declared)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	p := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-A"}}
	if !m.ScopeAllowed(p, "db-a", "", "", []string{"ORG-A"}) {
		t.Fatal("precondition: initial config must grant the same-org tenant")
	}
	// Rewrite with an org-scope on an undeclared key → reload rejected.
	if err := os.WriteFile(path, []byte("groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n    org-scope: region\n"), 0o600); err != nil {
		t.Fatalf("write bad config: %v", err)
	}
	if err := m.Reload(); err == nil {
		t.Fatal("Reload = nil, want an error for the undeclared org-scope key")
	}
	if !m.ScopeAllowed(p, "db-a", "", "", []string{"ORG-A"}) {
		t.Error("after failed reload: last-good must still grant the tenant")
	}
}

// --- WatchLoop tests ---

func TestWatchLoop_EmptyPath(t *testing.T) {
	t.Parallel()
	// NewForTest constructs a Manager with empty path → WatchLoop
	// is a no-op (returns immediately) per the configwatcher contract.
	m := NewForTest(&RBACConfig{})

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
	t.Parallel()
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", "groups: []\n")

	m, err := NewManager(rbacFile, nil)
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

func TestGet_EmptyConfig(t *testing.T) {
	t.Parallel()
	// PR-8/11: with the configwatcher embed, a Manager constructed
	// via NewForTest(&RBACConfig{}) is the canonical "open mode" /
	// empty config shape. Pre-PR-8 this test exercised the
	// uninitialized-Manager case (`&Manager{}`); that's no longer
	// constructible without panic since Get is promoted through
	// a possibly-nil Watcher pointer. The behavior under test
	// (Get returns a non-nil empty-Groups config) is preserved.
	m := NewForTest(&RBACConfig{})
	cfg := m.Get()
	if cfg == nil {
		t.Fatal("Get returned nil")
	}
	if len(cfg.Groups) != 0 {
		t.Errorf("expected 0 groups from empty config, got %d", len(cfg.Groups))
	}
}
