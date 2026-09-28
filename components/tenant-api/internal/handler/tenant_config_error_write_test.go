package handler

// #2373 blind review F1: GET /tenants/{id} answers a file threshold-exporter
// rejects with config_error and WITHOUT custom_alerts (it cannot vouch for
// them). A client that edits from that answer — the portal's custom-alerts
// modal reads a missing list as [] — must not be able to write a partial
// update over the file's real content: PUT .../custom-alerts would replace
// the tenant's existing recipes with the client's new list, and the batch
// patches would commit into a file no plane serves. Every endpoint that
// merges a partial update INTO the existing tenant file therefore refuses
// such a file (409 CONFLICT; per-op CONFLICT for the batch results) and
// leaves its bytes alone. The whole-file PUT /tenants/{id} is the repair
// path and is not affected.

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
)

const rejExistingAlert = `    _custom_alerts:
      - recipe: threshold
        name: existing_x
        metric: m
        threshold: "1"
        window: 5m
`

// rejFiles maps a case name to db-a.yaml content and the config_error GET
// reports for it ("" = healthy control).
var rejFiles = []struct {
	name, body, reason string
}{
	{"non_utf8_sibling", "tenants:\n  !!binary dP8=:\n    mysql_connections: \"10\"\n  db-a:\n    mysql_connections: \"70\"\n" + rejExistingAlert, "invalid_config"},
	{"malformed_yaml", "tenants:\n  db-a: [unclosed\n", "malformed_yaml"},
	{"healthy", "tenants:\n  db-a:\n    mysql_connections: \"70\"\n" + rejExistingAlert, ""},
}

func rejDir(t *testing.T, body string) string {
	t.Helper()
	dir := setupConfigDir(t, map[string]string{"db-a.yaml": body, "_defaults.yaml": caDefaults})
	initGitRepo(t, dir)
	return dir
}

func mustRead(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// The exact client flow of the portal modal: GET, take custom_alerts (a
// missing list reads as []), append a recipe, PUT with the GET's source_hash.
func TestPutCustomAlerts_RefusesFileExporterRejects(t *testing.T) {
	t.Parallel()
	for _, c := range rejFiles {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir := rejDir(t, c.body)
			deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC)}

			gw := httptest.NewRecorder()
			GetTenant(deps)(gw, newRequestWithChiParam("GET", "/api/v1/tenants/db-a", "id", "db-a", nil))
			if gw.Code != http.StatusOK {
				t.Fatalf("GET status = %d; body: %s", gw.Code, gw.Body.String())
			}
			var got struct {
				SourceHash   string           `json:"source_hash"`
				CustomAlerts []map[string]any `json:"custom_alerts"`
				ConfigError  string           `json:"config_error"`
			}
			if err := json.Unmarshal(gw.Body.Bytes(), &got); err != nil {
				t.Fatal(err)
			}
			if got.ConfigError != c.reason {
				t.Fatalf("GET config_error = %q, want %q", got.ConfigError, c.reason)
			}
			list := append(got.CustomAlerts, map[string]any{
				"recipe": "threshold", "name": "new_y", "metric": "m2", "threshold": "2", "window": "5m",
			})
			body, _ := json.Marshal(map[string]any{"base_hash": got.SourceHash, "custom_alerts": list})

			path := filepath.Join(dir, "db-a.yaml")
			before := mustRead(t, path)
			resp := putCustomAlerts(t, deps, "db-a", string(body), "alice@example.com", []string{"dba"})
			rb, _ := readBody(resp)
			after := mustRead(t, path)

			if c.reason == "" {
				if resp.StatusCode != http.StatusOK {
					t.Fatalf("healthy: status = %d, want 200; body: %s", resp.StatusCode, rb)
				}
				if !strings.Contains(after, "existing_x") || !strings.Contains(after, "new_y") {
					t.Errorf("healthy: want both recipes in the file:\n%s", after)
				}
				return
			}
			if resp.StatusCode != http.StatusConflict {
				t.Errorf("status = %d, want 409; body: %s", resp.StatusCode, rb)
			}
			if !strings.Contains(rb, `"code":"`+CodeConflict+`"`) {
				t.Errorf("body lacks code %s: %s", CodeConflict, rb)
			}
			if after != before {
				t.Errorf("file changed:\nbefore:\n%s\nafter:\n%s", before, after)
			}
		})
	}
}

// The shared per-tenant write of POST /tenants/batch (direct mode) and
// POST /groups/{id}/batch.
func TestApplyPatch_RefusesFileExporterRejects(t *testing.T) {
	t.Parallel()
	for _, c := range rejFiles {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir := rejDir(t, c.body)
			path := filepath.Join(dir, "db-a.yaml")
			before := mustRead(t, path)
			res := applyPatch(context.Background(), newTestWriter(dir), dir,
				BatchOperation{TenantID: "db-a", Patch: map[string]string{"_silent_mode": "warning"}}, "op@example.com")
			after := mustRead(t, path)
			if c.reason == "" {
				if res.Status != "ok" {
					t.Fatalf("healthy: status = %q (%s), want ok", res.Status, res.Message)
				}
				return
			}
			if res.Status != "error" || res.Code != CodeConflict {
				t.Errorf("result = %+v, want status error, code %s", res, CodeConflict)
			}
			if after != before {
				t.Errorf("file changed:\nbefore:\n%s\nafter:\n%s", before, after)
			}
		})
	}
}

func TestGroupBatch_RefusesFileExporterRejects(t *testing.T) {
	t.Parallel()
	for _, c := range rejFiles {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir := setupConfigDir(t, map[string]string{"db-a.yaml": c.body, "_defaults.yaml": caDefaults})
			setupGroupsFile(t, dir, "groups:\n  g1:\n    label: G1\n    members:\n      - db-a\n")
			initGitRepo(t, dir)
			path := filepath.Join(dir, "db-a.yaml")
			before := mustRead(t, path)

			rbacMgr := permissiveRBACManager(t)
			h := GroupBatch(&Deps{Writer: newTestWriter(dir), ConfigDir: dir,
				Groups: groups.NewManager(dir), RBAC: rbacMgr, WriteMode: WriteModeDirect})
			req := newRequestWithChiParam("POST", "/api/v1/groups/g1/batch", "id", "g1",
				bytes.NewBufferString(`{"patch":{"_silent_mode":"warning"}}`))
			req = setRequestIdentity(req, "a@a.com")
			w := httptest.NewRecorder()
			rbacMgr.Middleware(rbac.PermRead, nil)(h).ServeHTTP(w, req)
			if w.Code != http.StatusOK {
				t.Fatalf("status = %d; body: %s", w.Code, w.Body.String())
			}
			var resp GroupBatchResponse
			if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
				t.Fatal(err)
			}
			if len(resp.Results) != 1 {
				t.Fatalf("results = %+v, want one", resp.Results)
			}
			res := resp.Results[0]
			if c.reason == "" {
				if res.Status != "ok" {
					t.Fatalf("healthy: result = %+v, want ok", res)
				}
				return
			}
			if res.Status != "error" || res.Code != CodeConflict {
				t.Errorf("result = %+v, want status error, code %s", res, CodeConflict)
			}
			if after := mustRead(t, path); after != before {
				t.Errorf("file changed:\n%s", after)
			}
		})
	}
}

func TestBatchTenants_PRMode_RefusesFileExporterRejects(t *testing.T) {
	c := rejFiles[0]
	dir := setupConfigDir(t, map[string]string{"db-a.yaml": c.body, "_defaults.yaml": caDefaults})
	initGitRepo(t, dir)
	if out, err := exec.Command("git", "-C", dir, "branch", "-M", "main").CombinedOutput(); err != nil {
		t.Fatalf("git branch -M main: %v\n%s", err, out)
	}
	path := filepath.Join(dir, "db-a.yaml")
	before := mustRead(t, path)

	rbacMgr := adminRBAC(t)
	prCreated := false
	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, head string, labels []string) (*platform.PRInfo, error) {
			prCreated = true
			return &platform.PRInfo{Number: 7, WebURL: "https://example/pr/7", State: "open"}, nil
		},
	}
	h := BatchTenants(&Deps{
		Writer: newTestWriter(dir), ConfigDir: dir, RBAC: rbacMgr,
		WriteMode: WriteModePR, PRClient: mockClient, PRTracker: &mockPlatformTracker{},
	})
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch",
		bytes.NewBufferString(`{"operations":[{"tenant_id":"db-a","patch":{"_silent_mode":"warning"}}]}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	rbacMgr.Middleware(rbac.PermRead, nil)(h).ServeHTTP(w, req)

	if w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), `"code":"`+CodeConflict+`"`) {
		t.Errorf("status = %d, want 409 %s; body: %s", w.Code, CodeConflict, w.Body.String())
	}
	if prCreated {
		t.Error("a PR was created for a file the exporter rejects")
	}
	branches, _ := exec.Command("git", "-C", dir, "branch", "--format=%(refname:short)").Output()
	if strings.Contains(string(branches), "tenant-api/batch/") {
		t.Errorf("batch branch left behind:\n%s", branches)
	}
	if after := mustRead(t, path); after != before {
		t.Errorf("file changed:\n%s", after)
	}
}
