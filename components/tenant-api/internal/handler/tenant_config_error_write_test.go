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

// wantNotLoadableCode is the error code of the refusal (#2373 review round 3,
// F4): its own code, not CONFLICT — CONFLICT means "your base_hash is stale,
// refresh and retry", which is not what happened here. A literal, not the
// handler constant, so a rename of the wire value fails here.
const wantNotLoadableCode = "TENANT_CONFIG_NOT_LOADABLE"

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
			var env map[string]any
			if err := json.Unmarshal([]byte(rb), &env); err != nil {
				t.Fatalf("unmarshal %s: %v", rb, err)
			}
			if env["code"] != wantNotLoadableCode || env["tenant_id"] != "db-a" || env["config_error"] != c.reason {
				t.Errorf("envelope = %v, want code %s, tenant_id db-a, config_error %s", env, wantNotLoadableCode, c.reason)
			}
			if msg, _ := env["error"].(string); !strings.Contains(msg, "db-a") {
				t.Errorf("error message does not name the tenant: %q", msg)
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
			if res.Status != "error" || res.Code != wantNotLoadableCode || !strings.Contains(res.Message, "db-a") {
				t.Errorf("result = %+v, want status error, code %s, message naming db-a", res, wantNotLoadableCode)
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
			if res.Status != "error" || res.Code != wantNotLoadableCode || !strings.Contains(res.Message, "db-a") {
				t.Errorf("result = %+v, want status error, code %s, message naming db-a", res, wantNotLoadableCode)
			}
			if after := mustRead(t, path); after != before {
				t.Errorf("file changed:\n%s", after)
			}
		})
	}
}

// prBatchRun runs a PR-mode BatchTenants over dir; created reports whether
// the mock forge was asked to open a PR.
func prBatchRun(t *testing.T, dir, body string) (*httptest.ResponseRecorder, bool) {
	t.Helper()
	rbacMgr := adminRBAC(t)
	created := false
	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, head string, labels []string) (*platform.PRInfo, error) {
			created = true
			return &platform.PRInfo{Number: 7, WebURL: "https://example/pr/7", State: "open"}, nil
		},
	}
	h := BatchTenants(&Deps{
		Writer: newTestWriter(dir), ConfigDir: dir, RBAC: rbacMgr,
		WriteMode: WriteModePR, PRClient: mockClient, PRTracker: &mockPlatformTracker{},
	})
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	rbacMgr.Middleware(rbac.PermRead, nil)(h).ServeHTTP(w, req)
	return w, created
}

const rejHealthyB = "tenants:\n  db-b:\n    mysql_connections: \"70\"\n"

const rejTwoOps = `{"operations":[{"tenant_id":"db-b","patch":{"_silent_mode":"warning"}},{"tenant_id":"db-a","patch":{"_silent_mode":"warning"}}]}`

// A multi-op PR batch: db-b is healthy, db-a is not. The whole PR is refused
// and the answer names db-a (F3), under its own code (F4).
func TestBatchTenants_PRMode_RefusesFileExporterRejects(t *testing.T) {
	for _, c := range rejFiles[:2] {
		t.Run(c.name, func(t *testing.T) {
			dir := setupConfigDir(t, map[string]string{"db-a.yaml": c.body, "db-b.yaml": rejHealthyB, "_defaults.yaml": caDefaults})
			initGitRepo(t, dir)
			runGit(t, dir, "branch", "-M", "main")
			beforeA, beforeB := mustRead(t, filepath.Join(dir, "db-a.yaml")), mustRead(t, filepath.Join(dir, "db-b.yaml"))

			w, created := prBatchRun(t, dir, rejTwoOps)
			var env map[string]any
			_ = json.Unmarshal(w.Body.Bytes(), &env)
			if w.Code != http.StatusConflict || env["code"] != wantNotLoadableCode || env["tenant_id"] != "db-a" || env["config_error"] != c.reason {
				t.Errorf("status = %d, envelope = %v; want 409, code %s, tenant_id db-a, config_error %s",
					w.Code, env, wantNotLoadableCode, c.reason)
			}
			if created {
				t.Error("a PR was created for a file the exporter rejects")
			}
			branches, _ := exec.Command("git", "-C", dir, "branch", "--format=%(refname:short)").Output()
			if strings.Contains(string(branches), "tenant-api/batch/") {
				t.Errorf("batch branch left behind:\n%s", branches)
			}
			if mustRead(t, filepath.Join(dir, "db-a.yaml")) != beforeA || mustRead(t, filepath.Join(dir, "db-b.yaml")) != beforeB {
				t.Error("a tenant file changed")
			}
		})
	}
}

// F2: the PR batch branches from the FRESH origin base, so the pre-flight —
// which reads the local tree before checkout — must not refuse a tenant file
// on the strength of that tree. Local copy broken, origin repaired → the PR
// is opened. Origin still broken → the authoritative post-checkout pass
// refuses it.
func TestBatchTenants_PRMode_NotLoadableJudgedOnFreshBase(t *testing.T) {
	const repaired = "tenants:\n  db-a:\n    mysql_connections: \"70\"\n"
	for _, c := range rejFiles[:2] {
		for _, originRepaired := range []bool{true, false} {
			name := c.name + "/origin_broken"
			if originRepaired {
				name = c.name + "/origin_repaired"
			}
			t.Run(name, func(t *testing.T) {
				dir := setupConfigDir(t, map[string]string{"db-a.yaml": c.body, "_defaults.yaml": caDefaults})
				initGitRepo(t, dir)
				runGit(t, dir, "branch", "-M", "main")
				bare := t.TempDir()
				runGit(t, bare, "init", "--bare", "-b", "main")
				runGit(t, dir, "remote", "add", "origin", bare)
				runGit(t, dir, "push", "origin", "main")
				if originRepaired {
					other := t.TempDir()
					if out, err := exec.Command("git", "clone", bare, other).CombinedOutput(); err != nil {
						t.Fatalf("clone: %v\n%s", err, out)
					}
					if err := os.WriteFile(filepath.Join(other, "db-a.yaml"), []byte(repaired), 0o644); err != nil {
						t.Fatal(err)
					}
					runGit(t, other, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-am", "repair")
					runGit(t, other, "push", "origin", "main")
				}

				w, created := prBatchRun(t, dir, `{"operations":[{"tenant_id":"db-a","patch":{"_silent_mode":"warning"}}]}`)
				if originRepaired {
					if w.Code != http.StatusOK || !created {
						t.Errorf("status = %d, PR created = %v; want 200 and a PR; body: %s", w.Code, created, w.Body.String())
					}
					return
				}
				var env map[string]any
				_ = json.Unmarshal(w.Body.Bytes(), &env)
				if w.Code != http.StatusConflict || env["code"] != wantNotLoadableCode || created {
					t.Errorf("status = %d, PR created = %v; want 409 %s; body: %s", w.Code, created, wantNotLoadableCode, w.Body.String())
				}
			})
		}
	}
}

// #2405: the whole-file PUT is the repair path for every file GET answers with
// config_error — not only for the non-UTF-8 sibling id. Before the fix, a file
// whose YAML does not parse, whose `tenants:` is not a mapping, or that
// repeats a key was refused by the end-of-life guard's read of the current
// file (400 "cannot read current custom alerts") and could only be fixed in
// git. The body here carries no end-of-life recipe; that the guard still
// refuses one over such a file is pinned in package gitops
// (TestEolGuard_UnparseableBaseForbidsEveryEolRecipe) — the embedded recipe
// status map has no end-of-life recipe to send through the handler.
func TestPutTenant_RepairsFileExporterRejects(t *testing.T) {
	t.Parallel()
	const id = "svc-alpha"
	const rbacYAML = "groups:\n  - name: ops\n    tenants: [\"" + id + "\"]\n    permissions: [read, write]\n"
	files := []struct{ name, body, reason string }{
		{"unclosed_flow", "tenants:\n  " + id + ": [unclosed\n", "malformed_yaml"},
		{"not_yaml", "{{not yaml\n", "malformed_yaml"},
		{"top_level_list", "- a\n- b\n", "invalid_config"},
		{"tenants_scalar", "tenants: oops\n", "invalid_config"},
		{"duplicate_tenants_key", "tenants:\n  " + id + ":\n    mysql_connections: \"70\"\ntenants:\n  " + id + ":\n    mysql_connections: \"71\"\n", "invalid_config"},
		{"tenants_list", "tenants:\n  - " + id + "\n", "invalid_config"},
		{"non_utf8_sibling", "tenants:\n  !!binary dP8=:\n    mysql_connections: \"10\"\n  " + id + ":\n    mysql_connections: \"70\"\n", "invalid_config"},
	}
	const repaired = "tenants:\n  " + id + ":\n    mysql_connections: \"75\"\n"
	for _, c := range files {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir := setupConfigDir(t, map[string]string{id + ".yaml": c.body, "_defaults.yaml": caDefaults})
			initGitRepo(t, dir)
			deps := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: newRBACManager(t, rbacYAML), WriteMode: WriteModeDirect}
			getConfigError := func() string {
				gw := httptest.NewRecorder()
				GetTenant(deps)(gw, newRequestWithChiParam("GET", "/api/v1/tenants/"+id, "id", id, nil))
				if gw.Code != http.StatusOK {
					t.Fatalf("GET status = %d; body: %s", gw.Code, gw.Body.String())
				}
				var got struct {
					ConfigError string `json:"config_error"`
				}
				if err := json.Unmarshal(gw.Body.Bytes(), &got); err != nil {
					t.Fatal(err)
				}
				return got.ConfigError
			}
			if got := getConfigError(); got != c.reason {
				t.Fatalf("before: GET config_error = %q, want %q", got, c.reason)
			}

			req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+id, "id", id, bytes.NewBufferString(repaired))
			w := servePopulatingRBAC(t, PutTenant(deps), req, "alice@example.com", []string{"ops"})
			if w.Code != http.StatusOK {
				t.Fatalf("PUT status = %d, want 200; body: %s", w.Code, w.Body.String())
			}
			if after := mustRead(t, filepath.Join(dir, id+".yaml")); after != repaired {
				t.Errorf("file after PUT:\n%s\nwant:\n%s", after, repaired)
			}
			if got := getConfigError(); got != "" {
				t.Errorf("after: GET config_error = %q, want none", got)
			}
		})
	}
}
