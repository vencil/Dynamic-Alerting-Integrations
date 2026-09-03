package federation

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/handler"
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
	d := &handler.Deps{
		FederationPolicy: fedpolicy.NewManager(configDir),
		RBAC:             newRBACManager(t, ""),
	}
	w := executeWithRBAC(t, GetFederationPolicy(d), fedReq(t, "GET", "/api/v1/federation/policy", "", "", ""))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	var got fedpolicy.Config
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
	d := &handler.Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: fedpolicy.NewManager(configDir),
		// Caller is admin on db-a only — not a "*"-scoped platform admin.
		RBAC: newRBACManager(t, scopedAdminRBAC),
	}
	body := `{"whitelist":[{"metric":"mysql_up"}]}`
	w := executeWithRBAC(t, PutFederationPolicy(d), fedReq(t, "PUT", "/api/v1/federation/policy", "", "", body))
	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403, body: %s", w.Code, w.Body.String())
	}
}

func TestPutFederationPolicy_Success(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	d := &handler.Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: fedpolicy.NewManager(configDir),
		RBAC:             newRBACManager(t, platformAdminRBAC),
	}
	body := `{"whitelist":[{"metric":"mysql_up"},{"metric":"tenant:cpu:rate5m"}]}`
	w := executeWithRBAC(t, PutFederationPolicy(d), fedReq(t, "PUT", "/api/v1/federation/policy", "", "", body))
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
	d := &handler.Deps{
		ConfigDir:          configDir,
		Writer:             newTestWriter(configDir),
		FederationPolicy:   fedpolicy.NewManager(configDir),
		AdmissionValidator: fedpolicy.NewAdmissionValidator(promURL),
		RBAC:               newRBACManager(t, platformAdminRBAC),
	}
	w := executeWithRBAC(t, PutFederationPolicy(d),
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
	d := &handler.Deps{
		ConfigDir:          configDir,
		Writer:             newTestWriter(configDir),
		FederationPolicy:   fedpolicy.NewManager(configDir),
		AdmissionValidator: fedpolicy.NewAdmissionValidator(promURL),
		RBAC:               newRBACManager(t, platformAdminRBAC),
	}
	// No force → rejected.
	w := executeWithRBAC(t, PutFederationPolicy(d),
		fedReq(t, "PUT", "/api/v1/federation/policy", "", "", `{"whitelist":[{"metric":"m"}]}`))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (warn, no force)", w.Code)
	}
	// force without a reason → rejected.
	w = executeWithRBAC(t, PutFederationPolicy(d),
		fedReq(t, "PUT", "/api/v1/federation/policy", "", "", `{"whitelist":[{"metric":"m"}],"force":true}`))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (force without reason)", w.Code)
	}
	// force + reason → accepted, and the bypass is recorded in git.
	w = executeWithRBAC(t, PutFederationPolicy(d),
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
	d := &handler.Deps{
		ConfigDir:          configDir,
		Writer:             newTestWriter(configDir),
		FederationPolicy:   fedpolicy.NewManager(configDir),
		AdmissionValidator: fedpolicy.NewAdmissionValidator(promURL),
		RBAC:               newRBACManager(t, platformAdminRBAC),
	}
	w := executeWithRBAC(t, PutFederationPolicy(d),
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
	d := &handler.Deps{
		ConfigDir:          configDir,
		Writer:             newTestWriter(configDir),
		FederationPolicy:   fedpolicy.NewManager(configDir),
		AdmissionValidator: fedpolicy.NewAdmissionValidator(promURL),
		RBAC:               newRBACManager(t, platformAdminRBAC),
	}
	body := `{"whitelist":[{"metric":"m1"},{"metric":"m2"},{"metric":"m3"},{"metric":"m4"},{"metric":"m5"}]}`
	w := executeWithRBAC(t, PutFederationPolicy(d), fedReq(t, "PUT", "/api/v1/federation/policy", "", "", body))
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
	d := &handler.Deps{
		ConfigDir:          configDir,
		Writer:             newTestWriter(configDir),
		FederationPolicy:   fedpolicy.NewManager(configDir),
		AdmissionValidator: fedpolicy.NewAdmissionValidator(fakePrometheus(t, nil, nil)),
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
	w := executeWithRBAC(t, PutFederationPolicy(d), fedReq(t, "PUT", "/api/v1/federation/policy", "", "", sb.String()))
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
	d := &handler.Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: fedpolicy.NewManager(configDir),
		RBAC:             newRBACManager(t, platformAdminRBAC),
	}
	req := fedReq(t, "PUT", "/api/v1/federation/policy", "", "", `{"whitelist":[{"metric":"m"}]}`)
	ctx, cancel := context.WithCancel(req.Context())
	cancel() // the request is already aborted (server timeout / client gone)
	_ = executeWithRBAC(t, PutFederationPolicy(d), req.WithContext(ctx))
	if _, err := os.Stat(filepath.Join(configDir, "_federation_policy.yaml")); !os.IsNotExist(err) {
		t.Error("a cancelled request must not write the whitelist file (zombie write)")
	}
}

func TestPutFederationPolicy_RejectsInvalidMetricName(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	d := &handler.Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: fedpolicy.NewManager(configDir),
		RBAC:             newRBACManager(t, platformAdminRBAC),
	}
	body := `{"whitelist":[{"metric":"bad-name"}]}`
	w := executeWithRBAC(t, PutFederationPolicy(d), fedReq(t, "PUT", "/api/v1/federation/policy", "", "", body))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400, body: %s", w.Code, w.Body.String())
	}
}

func TestPutTenantFederation_ForbiddenWithoutTenantAdmin(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	d := &handler.Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: fedpolicy.NewManager(configDir),
		// Caller has admin on db-a only — editing db-b's subset is denied.
		RBAC: newRBACManager(t, scopedAdminRBAC),
	}
	body := `{"metrics":["mysql_up"]}`
	w := executeWithRBAC(t, PutTenantFederation(d), fedReq(t, "PUT", "/api/v1/tenants/db-b/federation", "id", "db-b", body))
	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403, body: %s", w.Code, w.Body.String())
	}
}

func TestPutTenantFederation_RejectsMetricOutsideWhitelist(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	// Whitelist allows mysql_up only.
	mgr := fedpolicy.NewManagerForTest(&fedpolicy.Config{
		Whitelist: []fedpolicy.WhitelistEntry{{Metric: "mysql_up"}},
	})
	d := &handler.Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: mgr,
		RBAC:             newRBACManager(t, scopedAdminRBAC), // admin on db-a
	}
	// redis_up is not in the whitelist — the 2-tier containment rule rejects it.
	body := `{"metrics":["mysql_up","redis_up"]}`
	w := executeWithRBAC(t, PutTenantFederation(d), fedReq(t, "PUT", "/api/v1/tenants/db-a/federation", "id", "db-a", body))
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
	mgr := fedpolicy.NewManagerForTest(&fedpolicy.Config{
		Whitelist: []fedpolicy.WhitelistEntry{{Metric: "mysql_up"}, {Metric: "pg_up"}},
	})
	d := &handler.Deps{
		ConfigDir:        configDir,
		Writer:           newTestWriter(configDir),
		FederationPolicy: mgr,
		RBAC:             newRBACManager(t, scopedAdminRBAC), // admin on db-a
	}
	body := `{"metrics":["mysql_up"]}`
	w := executeWithRBAC(t, PutTenantFederation(d), fedReq(t, "PUT", "/api/v1/tenants/db-a/federation", "id", "db-a", body))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	subset, err := readFederationSubset(d, "db-a")
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
	mgr := fedpolicy.NewManagerForTest(&fedpolicy.Config{
		Whitelist: []fedpolicy.WhitelistEntry{{Metric: "mysql_up"}},
	})
	d := &handler.Deps{ConfigDir: configDir, FederationPolicy: mgr, RBAC: newRBACManager(t, "")}

	w := executeWithRBAC(t, GetTenantFederation(d), fedReq(t, "GET", "/api/v1/tenants/db-a/federation", "id", "db-a", ""))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	var got fedpolicy.Subset
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
	d := &handler.Deps{
		ConfigDir:        configDir,
		FederationPolicy: fedpolicy.NewManager(configDir),
		RBAC:             newRBACManager(t, ""),
	}
	w := executeWithRBAC(t, GetTenantFederation(d), fedReq(t, "GET", "/api/v1/tenants/db-a/federation", "id", "db-a", ""))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	var got fedpolicy.Subset
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(got.Metrics) != 0 {
		t.Errorf("metrics = %d, want 0", len(got.Metrics))
	}
}

// federationJoinIDs is the id set both _federation/ join sites are
// tested against. Kept in one place so a shape added here has to be
// answered by the read site and the write site alike.
//
// The `.hidden` row earns its place: it is the only shape here whose
// rejection comes solely from confd's reserved-name rule. A hand-rolled
// read-side copy of the predicate that remembered `/`, `\`, `..` and the
// `_` prefix but forgot the dot prefix satisfies every other row — and a
// second hand-written copy is precisely what sharing the predicate is
// meant to prevent. Without this row the agreement assertion below can
// never fire.
var federationJoinIDs = []struct {
	name          string
	id            string
	unaddressable bool
}{
	{"ordinary tenant id", "tenant-alpha", false},
	{"traversal escaping _federation, landing inside ConfigDir", "foo/../../etc/passwd", true},
	{"traversal escaping ConfigDir entirely", "foo/../../../outside/secret", true},
	{"leading traversal, also escaping ConfigDir", "../../etc/passwd", true},
	{"nested path without traversal", "sub/nested", true},
	{"backslash separator", `win\path`, true},
	{"bare dot-dot", "..", true},
	{"reserved control-file name", "_defaults", true},
	{"dot-prefixed name", ".hidden", true},
	{"empty id", "", true},
}

// plantSubset writes a subset file carrying a unique marker metric at
// the exact path readFederationSubset would open for id, derived from
// federationSubsetPath rather than restating the join here. A
// hand-written fixture path drifts silently the next time that
// expression changes; this one cannot.
func plantSubset(t *testing.T, configDir, id, marker string) string {
	t.Helper()
	path := federationSubsetPath(configDir, id)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir for id %q: %v", id, err)
	}
	if err := os.WriteFile(path, []byte("metrics:\n  - "+marker+"\n"), 0o644); err != nil {
		t.Fatalf("plant for id %q at %s: %v", id, path, err)
	}
	return path
}

// TestReadFederationSubset_SinkGuard pins the read-side sink predicate.
//
// EVERY row gets a real file planted at its own join target first. That
// is what makes each rejection load-bearing: most of these ids land on
// paths that would otherwise be empty, where a missing guard returns an
// empty subset rather than a leak, and "the guard rejected" would be
// indistinguishable from "there was nothing there anyway". With a file
// planted, dropping the predicate serves it.
//
// Only two rows escape ConfigDir outright (`../../etc/passwd` and
// `foo/../../../outside/secret`); a third escapes _federation/ but stays
// under ConfigDir. The rest never escape at all — they are rejected for
// not being able to name a subset file, which is a different claim, and
// the planted files are what let this test hold both kinds to the same
// standard.
func TestReadFederationSubset_SinkGuard(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, "conf.d")
	d := &handler.Deps{ConfigDir: configDir}

	// Written before any subtest starts, read-only thereafter.
	markers := make(map[string]string, len(federationJoinIDs))
	for i, tc := range federationJoinIDs {
		markers[tc.id] = fmt.Sprintf("planted_row_%d", i)
		plantSubset(t, configDir, tc.id, markers[tc.id])
	}

	for _, tc := range federationJoinIDs {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			subset, err := readFederationSubset(d, tc.id)
			want := markers[tc.id]

			if !tc.unaddressable {
				if err != nil {
					t.Fatalf("readFederationSubset(%q): %v", tc.id, err)
				}
				if len(subset.Metrics) != 1 || subset.Metrics[0] != want {
					t.Errorf("metrics = %v, want [%s] — an addressable id must still read its own subset",
						subset.Metrics, want)
				}
				return
			}

			if !errors.Is(err, ErrUnaddressableTenantID) {
				t.Fatalf("readFederationSubset(%q) err = %v, want ErrUnaddressableTenantID; a subset file carrying %q sits at %s, so dropping the predicate serves it (got subset %+v)",
					tc.id, err, want, federationSubsetPath(configDir, tc.id), subset)
			}
			if subset != nil {
				t.Errorf("readFederationSubset(%q) returned subset %+v alongside its rejection", tc.id, subset)
			}
		})
	}

	t.Run("absent subset stays an empty subset, not an error", func(t *testing.T) {
		t.Parallel()
		subset, err := readFederationSubset(d, "tenant-with-no-subset-yet")
		if err != nil {
			t.Fatalf("readFederationSubset: %v", err)
		}
		if len(subset.Metrics) != 0 {
			t.Errorf("metrics = %v, want empty", subset.Metrics)
		}
	})
}

// TestFederationJoinSites_AgreeOnEveryID is why the read site reuses the
// write site's predicate instead of carrying a second hand-written copy:
// the two _federation/ join sites must disagree about no id.
//
// The comparison is by sentinel identity, so a write failing for an
// unrelated reason (git, disk) cannot make the two look asymmetric. The
// flip side is that the agreement assertion fires only when one side
// stops consulting the shared predicate — the exact regression it exists
// for, and the reason the `.hidden` row above has to be in the table.
//
// The addressable row also asserts the write produced its file: without
// that, a write plane broken end to end still satisfies "not rejected by
// the predicate" on every row, and the git setup below would carry no
// weight.
//
// Subtests here deliberately do NOT call t.Parallel(): each addressable
// row drives a real commit against one shared git repo. With a single
// such row today that is precaution rather than necessity — it becomes
// necessary the moment a second one is added to the table.
func TestFederationJoinSites_AgreeOnEveryID(t *testing.T) {
	t.Parallel()
	configDir := t.TempDir()
	initGitRepo(t, configDir)
	w := newTestWriter(configDir)
	d := &handler.Deps{ConfigDir: configDir}

	for _, tc := range federationJoinIDs {
		t.Run(tc.name, func(t *testing.T) {
			_, readErr := readFederationSubset(d, tc.id)
			writeErr := w.WriteFederationSubsetFile(context.Background(), tc.id, "test@test.com", "metrics: []\n")

			readRejected := errors.Is(readErr, ErrUnaddressableTenantID)
			writeRejected := errors.Is(writeErr, gitops.ErrReservedTenantID)
			if readRejected != writeRejected {
				t.Errorf("id %q: read-side rejected = %v (err %v), write-side rejected = %v (err %v); the two join sites must agree",
					tc.id, readRejected, readErr, writeRejected, writeErr)
			}
			if readRejected != tc.unaddressable {
				t.Errorf("id %q: read-side rejected = %v, want %v (err %v)", tc.id, readRejected, tc.unaddressable, readErr)
			}
			if tc.unaddressable {
				return
			}
			if writeErr != nil {
				t.Fatalf("write of addressable id %q failed: %v", tc.id, writeErr)
			}
			if _, err := os.Stat(federationSubsetPath(configDir, tc.id)); err != nil {
				t.Errorf("write of %q reported success but left no file at %s: %v — the two sites no longer agree on where the subset lives",
					tc.id, federationSubsetPath(configDir, tc.id), err)
			}
		})
	}
}
