package federation

// #1700, the reproduction from the issue: a tenant's _federation subset path
// that os.ReadFile cannot read (here ELOOP — the issue's EISDIR shape is now
// skipped by the resolver) is neither "not exist" nor any specially handled
// branch, so GET /tenants/{id}/federation answered 500 with conf.d's absolute
// path in the body — to a caller holding only read on that tenant.

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
	p := federationSubsetPath(configDir, "db-a")
	if err := os.Symlink(filepath.Base(p), p); err != nil {
		t.Fatal(err)
	}
	d := &handler.Deps{
		ConfigDir:        configDir,
		State:            newDirState(t, configDir),
		FederationPolicy: fedpolicy.NewDirManager(configDir),
		RBAC:             newRBACManager(t, ""),
	}
	w := executeWithRBAC(t, GetTenantFederation(d), fedReq(t, "GET", "/api/v1/tenants/db-a/federation", "id", "db-a", ""))

	// An unreadable record is a state-store fault (#2856): 503, retryable,
	// and the body still names no server path.
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503 (the fault did not fire as designed); body: %s", w.Code, w.Body.String())
	}
	body := w.Body.String()
	if strings.Contains(body, configDir) || strings.Contains(body, "too many levels of symbolic links") {
		t.Errorf("503 body carries the server's own error text: %s", body)
	}
	if !strings.Contains(body, `"code":"UPSTREAM_ERROR"`) {
		t.Errorf("503 body lost its code: %s", body)
	}
}

// The admission check's soft-warn Reason: when Prometheus cannot be reached,
// the error is a *url.Error naming the internal URL, host and IP. It is
// logged; the Reason the client reads says only that the query failed.
func TestPutFederationPolicy_UnreachablePrometheusNamesNoUpstream(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	d := &handler.Deps{
		ConfigDir:          configDir,
		State:              newDirState(t, configDir),
		Writer:             newTestWriter(configDir),
		FederationPolicy:   fedpolicy.NewDirManager(configDir),
		AdmissionValidator: fedpolicy.NewAdmissionValidator("http://127.0.0.1:1"),
		RBAC:               newRBACManager(t, platformAdminRBAC),
	}
	w := executeWithRBAC(t, PutFederationPolicy(d),
		fedReq(t, "PUT", "/api/v1/federation/policy", "", "", `{"whitelist":[{"metric":"m"}]}`))

	if !strings.Contains(w.Body.String(), "admission check could not be completed") {
		t.Fatalf("the fault did not fire as designed: %d %s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), "127.0.0.1") {
		t.Errorf("response names the Prometheus upstream: %s", w.Body.String())
	}
}
