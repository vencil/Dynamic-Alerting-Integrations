package handler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/vencil/tenant-api/internal/federation"
)

// platformAdminRBAC grants admin on every tenant ("*"-scoped) to the
// test caller's `platform-admins` group — i.e. a platform admin.
const platformAdminRBAC = `groups:
  - name: platform-admins
    tenants: ["*"]
    permissions: [admin]
`

// scopedAdminRBAC grants admin only on tenant `db-a` to the caller's
// `platform-admins` group — admin, but NOT a platform admin, and not
// admin on `db-b`.
const scopedAdminRBAC = `groups:
  - name: platform-admins
    tenants: ["db-a"]
    permissions: [admin]
`

func fedReq(t *testing.T, method, path, paramKey, paramVal, body string) *http.Request {
	t.Helper()
	var req *http.Request
	if paramKey != "" {
		req = newRequestWithChiParam(method, path, paramKey, paramVal, bytes.NewBufferString(body))
	} else {
		req = httptest.NewRequest(method, path, bytes.NewBufferString(body))
	}
	req.Header.Set("Content-Type", "application/json")
	setRequestIdentity(req, "test@example.com")
	return req
}

func TestGetFederationPolicy_Empty(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	d := &Deps{
		FederationPolicy: federation.NewPolicyManager(configDir),
		RBAC:             newRBACManager(t, ""),
	}
	w := executeWithRBAC(t, d.GetFederationPolicy(), fedReq(t, "GET", "/api/v1/federation/policy", "", "", ""))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	var got federation.FederationPolicyConfig
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(got.Whitelist) != 0 {
		t.Errorf("whitelist = %d entries, want 0", len(got.Whitelist))
	}
}

func TestPutFederationPolicy_ForbiddenForNonPlatformAdmin(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	d := &Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: federation.NewPolicyManager(configDir),
		// Caller is admin on db-a only — not a "*"-scoped platform admin.
		RBAC: newRBACManager(t, scopedAdminRBAC),
	}
	body := `{"whitelist":[{"metric":"mysql_up"}]}`
	w := executeWithRBAC(t, d.PutFederationPolicy(), fedReq(t, "PUT", "/api/v1/federation/policy", "", "", body))
	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403, body: %s", w.Code, w.Body.String())
	}
}

func TestPutFederationPolicy_Success(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	d := &Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: federation.NewPolicyManager(configDir),
		RBAC:             newRBACManager(t, platformAdminRBAC),
	}
	body := `{"whitelist":[{"metric":"mysql_up"},{"metric":"tenant:cpu:rate5m"}]}`
	w := executeWithRBAC(t, d.PutFederationPolicy(), fedReq(t, "PUT", "/api/v1/federation/policy", "", "", body))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	if _, err := os.Stat(filepath.Join(configDir, "_federation_policy.yaml")); err != nil {
		t.Fatalf("_federation_policy.yaml not written: %v", err)
	}
	// The handler reloads the manager — the new whitelist is live.
	if !d.FederationPolicy.IsWhitelisted("mysql_up") {
		t.Error("IsWhitelisted(mysql_up) = false after PUT, want true")
	}
}

func TestPutFederationPolicy_RejectsInvalidMetricName(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	d := &Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: federation.NewPolicyManager(configDir),
		RBAC:             newRBACManager(t, platformAdminRBAC),
	}
	body := `{"whitelist":[{"metric":"bad-name"}]}`
	w := executeWithRBAC(t, d.PutFederationPolicy(), fedReq(t, "PUT", "/api/v1/federation/policy", "", "", body))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400, body: %s", w.Code, w.Body.String())
	}
}

func TestPutTenantFederation_ForbiddenWithoutTenantAdmin(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	d := &Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: federation.NewPolicyManager(configDir),
		// Caller has admin on db-a only — editing db-b's subset is denied.
		RBAC: newRBACManager(t, scopedAdminRBAC),
	}
	body := `{"metrics":["mysql_up"]}`
	w := executeWithRBAC(t, d.PutTenantFederation(), fedReq(t, "PUT", "/api/v1/tenants/db-b/federation", "id", "db-b", body))
	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403, body: %s", w.Code, w.Body.String())
	}
}

func TestPutTenantFederation_RejectsMetricOutsideWhitelist(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	// Whitelist allows mysql_up only.
	mgr := federation.NewPolicyManagerForTest(&federation.FederationPolicyConfig{
		Whitelist: []federation.WhitelistEntry{{Metric: "mysql_up"}},
	})
	d := &Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: mgr,
		RBAC:             newRBACManager(t, scopedAdminRBAC), // admin on db-a
	}
	// redis_up is not in the whitelist — the 2-tier containment rule rejects it.
	body := `{"metrics":["mysql_up","redis_up"]}`
	w := executeWithRBAC(t, d.PutTenantFederation(), fedReq(t, "PUT", "/api/v1/tenants/db-a/federation", "id", "db-a", body))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400, body: %s", w.Code, w.Body.String())
	}
	if _, err := os.Stat(filepath.Join(configDir, "_federation", "db-a.yaml")); !os.IsNotExist(err) {
		t.Error("subset file should NOT be written when validation fails")
	}
}

func TestPutTenantFederation_Success(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	mgr := federation.NewPolicyManagerForTest(&federation.FederationPolicyConfig{
		Whitelist: []federation.WhitelistEntry{{Metric: "mysql_up"}, {Metric: "pg_up"}},
	})
	d := &Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: mgr,
		RBAC:             newRBACManager(t, scopedAdminRBAC), // admin on db-a
	}
	body := `{"metrics":["mysql_up"]}`
	w := executeWithRBAC(t, d.PutTenantFederation(), fedReq(t, "PUT", "/api/v1/tenants/db-a/federation", "id", "db-a", body))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	subset, err := d.readFederationSubset("db-a")
	if err != nil {
		t.Fatalf("readFederationSubset: %v", err)
	}
	if len(subset.Metrics) != 1 || subset.Metrics[0] != "mysql_up" {
		t.Errorf("subset = %+v, want [mysql_up]", subset.Metrics)
	}
}

func TestGetTenantFederation_NoFileYieldsEmptySubset(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	d := &Deps{
		ConfigDir:        configDir,
		FederationPolicy: federation.NewPolicyManager(configDir),
		RBAC:             newRBACManager(t, ""),
	}
	w := executeWithRBAC(t, d.GetTenantFederation(), fedReq(t, "GET", "/api/v1/tenants/db-a/federation", "id", "db-a", ""))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	var got federation.FederationSubset
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(got.Metrics) != 0 {
		t.Errorf("metrics = %d, want 0", len(got.Metrics))
	}
}
