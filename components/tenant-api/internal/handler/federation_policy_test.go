package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/federation"
)

// fakePrometheus mocks the Prometheus Series API for handler-level
// admission tests. It distinguishes the validator's two probes by the
// `match[]` selector: the tenant-labelled probe carries `tenant!=""`.
//   - labelled: series returned for the `{tenant!=""}` probe (a
//     non-empty value yields Pass).
//   - all:      series returned for the bare-metric existence probe.
func fakePrometheus(t *testing.T, labelled, all []map[string]string) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data := all
		if strings.Contains(r.URL.Query().Get("match[]"), `tenant!=""`) {
			data = labelled
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "success", "data": data})
	}))
	t.Cleanup(srv.Close)
	return srv.URL
}

// fakePromPerMetric mocks the Series API keyed on the metric NAME:
// metrics listed in hardBlock have no tenant-labelled series (so they
// hard-block), every other metric has one (so it passes). Used to
// verify the concurrent admission fan-out maps each verdict back to the
// correct metric.
func fakePromPerMetric(t *testing.T, hardBlock ...string) string {
	t.Helper()
	blocked := make(map[string]bool, len(hardBlock))
	for _, m := range hardBlock {
		blocked[m] = true
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sel := r.URL.Query().Get("match[]")
		metric := sel
		if i := strings.IndexByte(sel, '{'); i >= 0 {
			metric = sel[:i]
		}
		var data []map[string]string
		if strings.Contains(sel, `tenant!=""`) {
			// tenant-labelled probe: empty for blocked metrics.
			if !blocked[metric] {
				data = []map[string]string{{"__name__": metric, "tenant": "db-a"}}
			}
		} else {
			// bare existence probe: every metric has data.
			data = []map[string]string{{"__name__": metric}}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "success", "data": data})
	}))
	t.Cleanup(srv.Close)
	return srv.URL
}

// lastCommitMessage returns the full message of the most recent commit
// in dir — used to assert the --force bypass trailer landed in git.
func lastCommitMessage(t *testing.T, dir string) string {
	t.Helper()
	out, err := exec.Command("git", "-C", dir, "log", "-1", "--format=%B").CombinedOutput()
	if err != nil {
		t.Fatalf("git log: %v\n%s", err, out)
	}
	return string(out)
}

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

func TestPutFederationPolicy_AdmissionHardBlock(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	// No tenant-labelled series, but the metric has data — hard block:
	// not whitelistable, not forceable.
	promURL := fakePrometheus(t, nil, []map[string]string{{"__name__": "m", "instance": "x"}})
	d := &Deps{
		ConfigDir:          configDir,
		Writer:             newTestWriter(configDir),
		FederationPolicy:   federation.NewPolicyManager(configDir),
		AdmissionValidator: federation.NewAdmissionValidator(promURL),
		RBAC:               newRBACManager(t, platformAdminRBAC),
	}
	w := executeWithRBAC(t, d.PutFederationPolicy(),
		fedReq(t, "PUT", "/api/v1/federation/policy", "", "", `{"whitelist":[{"metric":"m"}]}`))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (hard block), body: %s", w.Code, w.Body.String())
	}
	if _, err := os.Stat(filepath.Join(configDir, "_federation_policy.yaml")); !os.IsNotExist(err) {
		t.Error("whitelist file should NOT be written on a hard block")
	}
}

func TestPutFederationPolicy_AdmissionWarnNeedsForce(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	// Both probes empty — no samples in the window → soft Warn.
	promURL := fakePrometheus(t, nil, nil)
	d := &Deps{
		ConfigDir:          configDir,
		Writer:             newTestWriter(configDir),
		FederationPolicy:   federation.NewPolicyManager(configDir),
		AdmissionValidator: federation.NewAdmissionValidator(promURL),
		RBAC:               newRBACManager(t, platformAdminRBAC),
	}
	// No force → rejected.
	w := executeWithRBAC(t, d.PutFederationPolicy(),
		fedReq(t, "PUT", "/api/v1/federation/policy", "", "", `{"whitelist":[{"metric":"m"}]}`))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (warn, no force)", w.Code)
	}
	// force without a reason → rejected.
	w = executeWithRBAC(t, d.PutFederationPolicy(),
		fedReq(t, "PUT", "/api/v1/federation/policy", "", "", `{"whitelist":[{"metric":"m"}],"force":true}`))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (force without reason)", w.Code)
	}
	// force + reason → accepted, and the bypass is recorded in git.
	w = executeWithRBAC(t, d.PutFederationPolicy(),
		fedReq(t, "PUT", "/api/v1/federation/policy", "", "", `{"whitelist":[{"metric":"m"}],"force":true,"reason":"cold-start: new cluster"}`))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (force + reason), body: %s", w.Code, w.Body.String())
	}
	msg := lastCommitMessage(t, configDir)
	if !strings.Contains(msg, "[Bypass-Validator]") || !strings.Contains(msg, "cold-start: new cluster") {
		t.Errorf("commit message missing the bypass trailer:\n%s", msg)
	}
}

func TestPutFederationPolicy_AdmissionPass(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	// A tenant-labelled series exists → Pass, no force needed.
	promURL := fakePrometheus(t, []map[string]string{{"__name__": "m", "tenant": "db-a"}}, nil)
	d := &Deps{
		ConfigDir:          configDir,
		Writer:             newTestWriter(configDir),
		FederationPolicy:   federation.NewPolicyManager(configDir),
		AdmissionValidator: federation.NewAdmissionValidator(promURL),
		RBAC:               newRBACManager(t, platformAdminRBAC),
	}
	w := executeWithRBAC(t, d.PutFederationPolicy(),
		fedReq(t, "PUT", "/api/v1/federation/policy", "", "", `{"whitelist":[{"metric":"m"}]}`))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (admission pass), body: %s", w.Code, w.Body.String())
	}
}

func TestPutFederationPolicy_AdmissionMultipleMetricsConcurrent(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	// Five metrics added in one PUT; m3 is the only hard block. The
	// admission checks run concurrently — this verifies the fan-out
	// maps each verdict back to the right metric (m3, not m1/m2/...).
	promURL := fakePromPerMetric(t, "m3")
	d := &Deps{
		ConfigDir:          configDir,
		Writer:             newTestWriter(configDir),
		FederationPolicy:   federation.NewPolicyManager(configDir),
		AdmissionValidator: federation.NewAdmissionValidator(promURL),
		RBAC:               newRBACManager(t, platformAdminRBAC),
	}
	body := `{"whitelist":[{"metric":"m1"},{"metric":"m2"},{"metric":"m3"},{"metric":"m4"},{"metric":"m5"}]}`
	w := executeWithRBAC(t, d.PutFederationPolicy(), fedReq(t, "PUT", "/api/v1/federation/policy", "", "", body))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (m3 hard block), body: %s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"metric":"m3"`) || !strings.Contains(w.Body.String(), "hard_block") {
		t.Errorf("response should flag m3 as hard_block; body: %s", w.Body.String())
	}
}

func TestPutFederationPolicy_RejectsTooManyNewMetrics(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	d := &Deps{
		ConfigDir:          configDir,
		Writer:             newTestWriter(configDir),
		FederationPolicy:   federation.NewPolicyManager(configDir),
		AdmissionValidator: federation.NewAdmissionValidator(fakePrometheus(t, nil, nil)),
		RBAC:               newRBACManager(t, platformAdminRBAC),
	}
	// One more than the cap — rejected before any admission call.
	var sb strings.Builder
	sb.WriteString(`{"whitelist":[`)
	for i := 1; i <= maxNewMetricsPerRequest+1; i++ {
		if i > 1 {
			sb.WriteByte(',')
		}
		fmt.Fprintf(&sb, `{"metric":"m%d"}`, i)
	}
	sb.WriteString(`]}`)
	w := executeWithRBAC(t, d.PutFederationPolicy(), fedReq(t, "PUT", "/api/v1/federation/policy", "", "", sb.String()))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (too many new metrics)", w.Code)
	}
	if !strings.Contains(w.Body.String(), "too many new metrics") {
		t.Errorf("body should explain the per-request cap; got: %s", w.Body.String())
	}
}

func TestPutFederationPolicy_CancelledContextSkipsGitWrite(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	// Validator disabled so admission is skipped — isolates the
	// point-of-no-return context guard right before the git write.
	d := &Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: federation.NewPolicyManager(configDir),
		RBAC:             newRBACManager(t, platformAdminRBAC),
	}
	req := fedReq(t, "PUT", "/api/v1/federation/policy", "", "", `{"whitelist":[{"metric":"m"}]}`)
	ctx, cancel := context.WithCancel(req.Context())
	cancel() // the request is already aborted (server timeout / client gone)
	_ = executeWithRBAC(t, d.PutFederationPolicy(), req.WithContext(ctx))
	if _, err := os.Stat(filepath.Join(configDir, "_federation_policy.yaml")); !os.IsNotExist(err) {
		t.Error("a cancelled request must not write the whitelist file (zombie write)")
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

func TestGetTenantFederation_ReadRepairDropsStaleMetric(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	// Simulate a subset file that went stale: it still lists redis_up,
	// but the platform whitelist now allows only mysql_up.
	fedDir := filepath.Join(configDir, "_federation")
	if err := os.MkdirAll(fedDir, 0755); err != nil {
		t.Fatalf("mkdir _federation: %v", err)
	}
	if err := os.WriteFile(filepath.Join(fedDir, "db-a.yaml"),
		[]byte("metrics:\n  - mysql_up\n  - redis_up\n"), 0644); err != nil {
		t.Fatalf("write stale subset: %v", err)
	}
	mgr := federation.NewPolicyManagerForTest(&federation.FederationPolicyConfig{
		Whitelist: []federation.WhitelistEntry{{Metric: "mysql_up"}},
	})
	d := &Deps{ConfigDir: configDir, FederationPolicy: mgr, RBAC: newRBACManager(t, "")}

	w := executeWithRBAC(t, d.GetTenantFederation(), fedReq(t, "GET", "/api/v1/tenants/db-a/federation", "id", "db-a", ""))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	var got federation.FederationSubset
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	// redis_up is no longer whitelisted — read-repair drops it.
	if len(got.Metrics) != 1 || got.Metrics[0] != "mysql_up" {
		t.Errorf("effective metrics = %v, want [mysql_up]", got.Metrics)
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
