package rbac

import (
	"os"
	"path/filepath"
	"testing"
)

// ADR-027 MED-8: a configured-but-empty _rbac.yaml (zero groups) must fail
// closed — an authenticated identity with no group mapping gets NO access —
// instead of the legacy open-read-to-all degrade. A path-less (bare) run
// keeps open-read; the --rbac-empty-open flag (AllowOpenReadOnEmpty) restores
// the legacy behavior for backward compat.

func TestHasPermission_FailClosedOnEmpty(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{}) // zero groups
	m.failClosedOnEmpty = true     // as if a --rbac path was configured

	if m.HasPermission(nil, "*", PermRead) {
		t.Error("fail-closed: read must be DENIED on empty groups, got allowed")
	}
	if m.HasPermission([]string{"anything"}, "some-tenant", PermRead) {
		t.Error("fail-closed: read must be DENIED regardless of claimed groups")
	}
	if m.HasPermission(nil, "*", PermWrite) {
		t.Error("fail-closed: write must be DENIED on empty groups")
	}
}

func TestHasMetadataAccess_FailClosedOnEmpty(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{})
	m.failClosedOnEmpty = true

	if m.HasMetadataAccess(nil, "t", "production", "finance") {
		t.Error("fail-closed: metadata access must be DENIED on empty groups")
	}
}

// Regression pin: the path-less open-read mode is preserved (NewForTest leaves
// failClosedOnEmpty false, matching a bare run with no --rbac path).
func TestHasPermission_OpenReadPreservedWhenNotFailClosed(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{}) // failClosedOnEmpty defaults false

	if !m.HasPermission(nil, "*", PermRead) {
		t.Error("open mode: read must be ALLOWED on empty groups")
	}
	if m.HasPermission(nil, "*", PermWrite) {
		t.Error("open mode: write must still be DENIED on empty groups")
	}
}

// The --rbac-empty-open escape hatch flips a fail-closed manager back to
// open-read on empty.
func TestAllowOpenReadOnEmpty_RestoresLegacy(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{})
	m.failClosedOnEmpty = true
	if m.HasPermission(nil, "*", PermRead) {
		t.Fatal("precondition: expected fail-closed before escape hatch")
	}

	m.AllowOpenReadOnEmpty()

	if !m.HasPermission(nil, "*", PermRead) {
		t.Error("AllowOpenReadOnEmpty: read must be ALLOWED again")
	}
	if m.HasPermission(nil, "*", PermWrite) {
		t.Error("AllowOpenReadOnEmpty: write must still be DENIED")
	}
}

// End-to-end through the real NewManager wiring (exercises the `path != ""`
// decision at rbac.go, so a revert of that line would fail here — not just
// the direct-field tests above).
func TestNewManager_ConfiguredEmptyFailsClosed(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "_rbac.yaml")
	if err := os.WriteFile(path, []byte("groups: []\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	m, err := NewManager(path, nil)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if m.HasPermission(nil, "*", PermRead) {
		t.Error("NewManager(configured empty): read must be DENIED (MED-8 fail-closed)")
	}
}

func TestNewManager_NoPathStaysOpenRead(t *testing.T) {
	t.Parallel()
	m, err := NewManager("", nil) // no --rbac path → intentional open mode
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if !m.HasPermission(nil, "*", PermRead) {
		t.Error("NewManager(no path): read must be ALLOWED (open mode preserved)")
	}
	if m.HasPermission(nil, "*", PermWrite) {
		t.Error("NewManager(no path): write must still be DENIED")
	}
}
