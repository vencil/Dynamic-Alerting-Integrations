package handler

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

const caDefaults = "defaults:\n  mysql_connections: 80\n"

const caTenantYAML = `# owner: payments-team
tenants:
  db-a:
    mysql_connections: "70"  # baseline
`

const caWriteRBAC = `groups:
  - name: dba
    tenants: ["db-a"]
    permissions: [read, write]
`

func putCustomAlerts(t *testing.T, deps *Deps, tenantID, bodyJSON, email string, groups []string) *http.Response {
	t.Helper()
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+tenantID+"/custom-alerts", "id", tenantID,
		bytes.NewBufferString(bodyJSON))
	req.Header.Set("Content-Type", "application/json")
	w := servePopulatingRBAC(t, PutTenantCustomAlerts(deps), req, email, groups)
	return w.Result()
}

func readBody(resp *http.Response) (string, error) {
	b, err := io.ReadAll(resp.Body)
	return string(b), err
}

func TestPutCustomAlerts_AddPreservesComments(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{"db-a.yaml": caTenantYAML, "_defaults.yaml": caDefaults})
	initGitRepo(t, dir)
	deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC)}

	h := cfg.ComputeSourceHash([]byte(caTenantYAML))
	body := `{"base_hash":"` + h + `","custom_alerts":[{"recipe":"threshold","name":"queue_high","metric":"queue_depth","threshold":"1000","window":"5m"}]}`
	resp := putCustomAlerts(t, deps, "db-a", body, "alice@example.com", []string{"dba"})
	if resp.StatusCode != http.StatusOK {
		b, _ := readBody(resp)
		t.Fatalf("status = %d, want 200; body: %s", resp.StatusCode, b)
	}
	out, _ := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
	s := string(out)
	if !strings.Contains(s, "# owner: payments-team") || !strings.Contains(s, "# baseline") {
		t.Errorf("comments must survive the AST merge:\n%s", s)
	}
	if !strings.Contains(s, "_custom_alerts") || !strings.Contains(s, "queue_high") {
		t.Errorf("recipe not written:\n%s", s)
	}
	if !strings.Contains(s, `mysql_connections: "70"`) {
		t.Errorf("sibling key dropped:\n%s", s)
	}
}

func TestPutCustomAlerts_EmptyDeletesKey(t *testing.T) {
	t.Parallel()
	withAlert := `# keep me
tenants:
  db-a:
    mysql_connections: "70"
    _custom_alerts:
      - recipe: threshold
        name: x
        metric: m
        threshold: "1"
        window: 5m
`
	dir := setupConfigDir(t, map[string]string{"db-a.yaml": withAlert, "_defaults.yaml": caDefaults})
	initGitRepo(t, dir)
	deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC)}

	h := cfg.ComputeSourceHash([]byte(withAlert))
	resp := putCustomAlerts(t, deps, "db-a", `{"base_hash":"`+h+`","custom_alerts":[]}`, "alice@example.com", []string{"dba"})
	if resp.StatusCode != http.StatusOK {
		b, _ := readBody(resp)
		t.Fatalf("status = %d, want 200; body: %s", resp.StatusCode, b)
	}
	out, _ := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
	if strings.Contains(string(out), "_custom_alerts") {
		t.Errorf("empty array must delete the key:\n%s", out)
	}
	if !strings.Contains(string(out), "# keep me") {
		t.Errorf("deleting must not disturb comments:\n%s", out)
	}
}

func TestPutCustomAlerts_BaseHashMismatch409(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{"db-a.yaml": caTenantYAML, "_defaults.yaml": caDefaults})
	initGitRepo(t, dir)
	deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC)}

	body := `{"base_hash":"deadbeefdeadbeef","custom_alerts":[{"recipe":"threshold","name":"q","metric":"m","threshold":"1","window":"5m"}]}`
	resp := putCustomAlerts(t, deps, "db-a", body, "alice@example.com", []string{"dba"})
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("status = %d, want 409 on base_hash mismatch", resp.StatusCode)
	}
}

func TestPutCustomAlerts_MatchingBaseHashSucceeds(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{"db-a.yaml": caTenantYAML, "_defaults.yaml": caDefaults})
	initGitRepo(t, dir)
	deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC)}
	hash := cfg.ComputeSourceHash([]byte(caTenantYAML))

	body := `{"base_hash":"` + hash + `","custom_alerts":[{"recipe":"threshold","name":"q","metric":"m","threshold":"1","window":"5m"}]}`
	resp := putCustomAlerts(t, deps, "db-a", body, "alice@example.com", []string{"dba"})
	if resp.StatusCode != http.StatusOK {
		b, _ := readBody(resp)
		t.Fatalf("status = %d, want 200 with matching base_hash; body: %s", resp.StatusCode, b)
	}
	// G3: the response returns a fresh source_hash (the client's next base_hash),
	// which must differ from the one just consumed (the file changed).
	b, _ := readBody(resp)
	var pr PutCustomAlertsResponse
	if err := json.Unmarshal([]byte(b), &pr); err != nil {
		t.Fatalf("response not JSON: %v; body: %s", err, b)
	}
	if len(pr.SourceHash) != 16 || pr.SourceHash == hash {
		t.Errorf("response source_hash = %q, want a fresh 16-char hash != the input %q", pr.SourceHash, hash)
	}
}

// Reef 4 "pre-existing poison" — a hand-written bad recipe already in the file
// must surface (by name) when the tenant adds a perfectly valid new one, so the
// UI can point at the offending rule rather than reject opaquely.
func TestPutCustomAlerts_PreExistingPoisonLocatable(t *testing.T) {
	t.Parallel()
	poisoned := `tenants:
  db-a:
    mysql_connections: "70"
    _custom_alerts:
      - recipe: threshold
        name: legacy_bad
        metric: "a:b:c"
        threshold: "1"
        window: 5m
`
	dir := setupConfigDir(t, map[string]string{"db-a.yaml": poisoned, "_defaults.yaml": caDefaults})
	initGitRepo(t, dir)
	deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC)}
	h := cfg.ComputeSourceHash([]byte(poisoned))

	// client adds a VALID recipe but resends the array incl the bad legacy one
	body := `{"base_hash":"` + h + `","custom_alerts":[` +
		`{"recipe":"threshold","name":"legacy_bad","metric":"a:b:c","threshold":"1","window":"5m"},` +
		`{"recipe":"threshold","name":"good_new","metric":"queue_depth","threshold":"100:warning","window":"5m"}]}`
	resp := putCustomAlerts(t, deps, "db-a", body, "alice@example.com", []string{"dba"})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (the pre-existing bad recipe blocks the write)", resp.StatusCode)
	}
	b, _ := readBody(resp)
	if !strings.Contains(b, "legacy_bad") && !strings.Contains(b, "a:b:c") {
		t.Errorf("400 violations must locate the offending pre-existing recipe; body: %s", b)
	}
}

func TestPutCustomAlerts_InvalidRecipe400WithViolations(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{"db-a.yaml": caTenantYAML, "_defaults.yaml": caDefaults})
	initGitRepo(t, dir)
	deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC)}

	// bad metric (colon → recording-rule reference, rejected by the validator)
	h := cfg.ComputeSourceHash([]byte(caTenantYAML))
	body := `{"base_hash":"` + h + `","custom_alerts":[{"recipe":"threshold","name":"bad","metric":"a:b:c","threshold":"1","window":"5m"}]}`
	resp := putCustomAlerts(t, deps, "db-a", body, "alice@example.com", []string{"dba"})
	if resp.StatusCode != http.StatusBadRequest {
		b, _ := readBody(resp)
		t.Fatalf("status = %d, want 400 for invalid recipe; body: %s", resp.StatusCode, b)
	}
	b, _ := readBody(resp)
	if !strings.Contains(b, "violations") {
		t.Errorf("400 must carry structured violations (Reef 4); body: %s", b)
	}
	// the bad recipe must NOT have been written
	out, _ := os.ReadFile(filepath.Join(dir, "db-a.yaml"))
	if strings.Contains(string(out), "a:b:c") {
		t.Errorf("invalid recipe must not be committed:\n%s", out)
	}
}

func TestPutCustomAlerts_NotFound404(t *testing.T) {
	t.Parallel()
	// RBAC grants write on any tenant ("*"); the file simply doesn't exist.
	dir := setupConfigDir(t, map[string]string{})
	deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, `groups:
  - name: all
    tenants: ["*"]
    permissions: [read, write]
`)}
	body := `{"base_hash":"any","custom_alerts":[{"recipe":"threshold","name":"q","metric":"m","threshold":"1","window":"5m"}]}`
	resp := putCustomAlerts(t, deps, "ghost", body, "alice@example.com", []string{"all"})
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status = %d, want 404 for nonexistent tenant", resp.StatusCode)
	}
}

func TestPutCustomAlerts_AbsentFieldRejected(t *testing.T) {
	t.Parallel()
	// self-review F1: an absent custom_alerts must NOT be read as delete-all.
	dir := setupConfigDir(t, map[string]string{"db-a.yaml": caTenantYAML, "_defaults.yaml": caDefaults})
	initGitRepo(t, dir)
	deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC)}

	h := cfg.ComputeSourceHash([]byte(caTenantYAML))
	resp := putCustomAlerts(t, deps, "db-a", `{"base_hash":"`+h+`"}`, "alice@example.com", []string{"dba"})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 when custom_alerts is absent (must not silently delete)", resp.StatusCode)
	}
}

func TestPutCustomAlerts_MissingBaseHashRejected(t *testing.T) {
	t.Parallel()
	// self-review F2: base_hash is required (OCC is the safe default).
	dir := setupConfigDir(t, map[string]string{"db-a.yaml": caTenantYAML, "_defaults.yaml": caDefaults})
	initGitRepo(t, dir)
	deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC)}

	resp := putCustomAlerts(t, deps, "db-a", `{"custom_alerts":[]}`, "alice@example.com", []string{"dba"})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 when base_hash is missing", resp.StatusCode)
	}
}

func TestPutCustomAlerts_PRMode501(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{"db-a.yaml": caTenantYAML, "_defaults.yaml": caDefaults})
	deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC), WriteMode: WriteModePR}

	body := `{"custom_alerts":[{"recipe":"threshold","name":"q","metric":"m","threshold":"1","window":"5m"}]}`
	resp := putCustomAlerts(t, deps, "db-a", body, "alice@example.com", []string{"dba"})
	if resp.StatusCode != http.StatusNotImplemented {
		t.Fatalf("status = %d, want 501 in PR write-back mode", resp.StatusCode)
	}
}
