package federation

// #1700, the reproduction from the issue: a tenant's _federation subset path
// that is a directory makes os.ReadFile fail with EISDIR, which is neither
// "not exist" nor any specially handled branch, so GET /tenants/{id}/federation
// answered 500 with conf.d's absolute path in the body — to a caller holding
// only read on that tenant.

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/handler"
)

func TestGetTenantFederation_UnreadableSubsetNamesNoServerPath(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	// A self-referencing symlink: os.ReadFile fails with ELOOP. (The issue's
	// EISDIR shape — the path made a directory — is now skipped by the
	// resolver; ELOOP is the other shape it named that still reaches the read.)
	subDir := filepath.Join(configDir, "_federation")
	if err := os.MkdirAll(subDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("db-a.yaml", filepath.Join(subDir, "db-a.yaml")); err != nil {
		t.Fatal(err)
	}
	d := &handler.Deps{
		ConfigDir:        configDir,
		FederationPolicy: fedpolicy.NewManager(configDir),
		RBAC:             newRBACManager(t, ""),
	}
	w := executeWithRBAC(t, GetTenantFederation(d), fedReq(t, "GET", "/api/v1/tenants/db-a/federation", "id", "db-a", ""))

	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500 (the fault did not fire as designed); body: %s", w.Code, w.Body.String())
	}
	body := w.Body.String()
	if strings.Contains(body, configDir) || strings.Contains(body, "is a directory") {
		t.Errorf("500 body carries the server's own error text: %s", body)
	}
	if !strings.Contains(body, `"code":"INTERNAL_ERROR"`) {
		t.Errorf("500 body lost its code: %s", body)
	}
}
