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
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
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

// rejFiles maps a case name to db-a.yaml content, the config_error GET
// reports for it ("" = healthy control), and — #3064 — the git-managed
// reason of a file that is not API-shaped ("" for db-a's own file): such a
// file is refused by every write as TENANT_GIT_MANAGED before its
// loadability is judged, and GET gives no source_hash for it.
var rejFiles = []struct {
	name, body, reason string
	gitManaged         string
}{
	{"non_utf8_sibling", "tenants:\n  !!binary dP8=:\n    mysql_connections: \"10\"\n  db-a:\n    mysql_connections: \"70\"\n" + rejExistingAlert, "invalid_config", "yaml_indirection"},
	{"malformed_yaml", "tenants:\n  db-a: [unclosed\n", "malformed_yaml", "not_tenant_document"},
	// #3063: an explicit tag its text does not satisfy, in the tenant's own
	// block. The file declares db-a alone: API-shaped, its hash shown, and
	// a partial write refused as TENANT_CONFIG_NOT_LOADABLE.
	{"mismatched_explicit_tag", "tenants:\n  db-a:\n    mysql_connections: !!int zz\n" + rejExistingAlert, "invalid_config", ""},
	{"healthy", "tenants:\n  db-a:\n    mysql_connections: \"70\"\n" + rejExistingAlert, "", ""},
}

// rejRefusal is the code, and a text in the refusal, a partial write of
// rejFiles row c gets.
func rejRefusal(name, reason, gitManaged string) (code, text string) {
	if gitManaged != "" {
		return string(errcode.TenantGitManaged), "git-managed (" + gitManaged + ")"
	}
	return wantNotLoadableCode, "config_error: " + reason
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
			deps := &Deps{ConfigDir: dir, State: mustDirState(dir), Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC)}

			// GET as the caller who then PUTs: since #1560 a caller who may
			// not write the tenant (an anonymous request included) is shown
			// no source_hash.
			gw := servePopulatingRBAC(t, GetTenant(deps),
				newRequestWithChiParam("GET", "/api/v1/tenants/db-a", "id", "db-a", nil), "alice@example.com", []string{"dba"})
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
			// #3064: the non-UTF-8 and malformed files are git-managed, so
			// GET gave no source_hash. A client that has the hash anyway (it
			// is in git) still gets the git-managed refusal.
			baseHash := got.SourceHash
			if c.gitManaged != "" {
				if baseHash != "" {
					t.Fatalf("GET source_hash = %q, want none for a git-managed file (#3064)", baseHash)
				}
				baseHash = cfg.ComputeSourceHash([]byte(c.body))
			} else if want := cfg.ComputeSourceHash([]byte(c.body)); baseHash != want {
				t.Fatalf("GET source_hash = %q, want %q", baseHash, want)
			}
			list := append(got.CustomAlerts, map[string]any{
				"recipe": "threshold", "name": "new_y", "metric": "m2", "threshold": "2", "window": "5m",
			})
			body, _ := json.Marshal(map[string]any{"base_hash": baseHash, "custom_alerts": list})

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
			if code, _ := rejRefusal(c.name, c.reason, c.gitManaged); c.gitManaged != "" {
				if env["code"] != code || env["tenant_id"] != "db-a" || env["git_managed_reason"] != c.gitManaged {
					t.Errorf("envelope = %v, want code %s, tenant_id db-a, git_managed_reason %s", env, code, c.gitManaged)
				}
			} else if env["code"] != code || env["tenant_id"] != "db-a" || env["config_error"] != c.reason {
				t.Errorf("envelope = %v, want code %s, tenant_id db-a, config_error %s", env, code, c.reason)
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
			if code, text := rejRefusal(c.name, c.reason, c.gitManaged); res.Status != "error" || string(res.Code) != code ||
				!strings.Contains(res.Message, "db-a") || !strings.Contains(res.Message, text) {
				t.Errorf("result = %+v, want status error, code %s, message naming db-a and %q", res, code, text)
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
			h := GroupBatch(&Deps{State: mustDirState(dir), Writer: newTestWriter(dir), ConfigDir: dir,
				Groups: groups.NewDirManager(dir), RBAC: rbacMgr, WriteMode: WriteModeDirect})
			req := newRequestWithChiParam("POST", "/api/v1/groups/g1/batch", "id", "g1",
				bytes.NewBufferString(`{"patch":{"_silent_mode":"warning"}}`))
			req = setRequestIdentity(req, "a@a.com")
			w := httptest.NewRecorder()
			rbacMgr.Middleware(rbac.PermRead, nil)(h).ServeHTTP(w, req)
			// #2900: a refused member rejects the batch (422).
			if want := http.StatusOK; c.reason != "" && w.Code != http.StatusUnprocessableEntity || c.reason == "" && w.Code != want {
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
			if code, _ := rejRefusal(c.name, c.reason, c.gitManaged); res.Status != "error" || string(res.Code) != code || !strings.Contains(res.Message, "db-a") {
				t.Errorf("result = %+v, want status error, code %s, message naming db-a", res, code)
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
	for _, c := range rejFiles[:3] {
		t.Run(c.name, func(t *testing.T) {
			dir := setupConfigDir(t, map[string]string{"db-a.yaml": c.body, "db-b.yaml": rejHealthyB, "_defaults.yaml": caDefaults})
			initGitRepo(t, dir)
			runGit(t, dir, "branch", "-M", "main")
			beforeA, beforeB := mustRead(t, filepath.Join(dir, "db-a.yaml")), mustRead(t, filepath.Join(dir, "db-b.yaml"))

			w, created := prBatchRun(t, dir, rejTwoOps)
			// #2900 F4: refused per op on the fresh base, the batch rejected.
			code, text := rejRefusal(c.name, c.reason, c.gitManaged)
			assertRejectedOp(t, w, 1, "db-a", code, text)
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
	for _, c := range rejFiles[:3] {
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
				code, text := rejRefusal(c.name, c.reason, c.gitManaged)
				assertRejectedOp(t, w, 0, "db-a", code, text)
				if created {
					t.Error("a PR was created for a file the exporter rejects on the base")
				}
			})
		}
	}
}

// #2405 callers: write on the URL tenant only, write via a prefix pattern that
// covers it, write via a "*" rule restricted to an environment (a scoped
// caller — in shadow mode it still reaches the URL tenant), read via an
// unrestricted "*" rule plus write on the URL tenant only (all tenants are
// reachable, but only for read), and write via an unrestricted "*" rule. Only
// the last may replace a current file that cannot be parsed.
const repairRBACYAML = "groups:\n" +
	"  - name: ops\n    tenants: [\"svc-alpha\"]\n    permissions: [read, write]\n" +
	"  - name: svc-team\n    tenants: [\"svc-*\"]\n    permissions: [read, write]\n" +
	"  - name: staging-ops\n    tenants: [\"*\"]\n    permissions: [read, write]\n    environments: [staging]\n" +
	"  - name: auditors\n    tenants: [\"*\"]\n    permissions: [read]\n" +
	"  - name: platform\n    tenants: [\"*\"]\n    permissions: [read, write]\n"

var repairCallers = []struct {
	name            string
	groups          []string
	platformWritten bool
}{
	{"ops", []string{"ops"}, false},
	{"svc-team", []string{"svc-team"}, false},
	{"staging-ops", []string{"staging-ops"}, false},
	{"read-all-write-one", []string{"auditors", "ops"}, false},
	{"platform", []string{"platform"}, true},
}

// #2405: before the fix, a file whose YAML does not parse, whose `tenants:` is
// not a mapping, or that repeats a key was refused by the end-of-life guard's
// read of the current file (400 "cannot read current custom alerts") and
// could only be fixed in git; the fix let a caller with write permission on
// ALL tenants (an unrestricted "*" rule) replace it. Since #3064 only an
// API-shaped file is written through the API at all, so the net rule is: an
// API-shaped file the end-of-life guard cannot decode is replaceable by that
// caller, every other caller refused with 400, file untouched; every other
// unloadable shape here (not YAML, not a tenant document, a tagged key) is
// git-managed — 409 TENANT_GIT_MANAGED for every caller, repaired in git.
// The body here carries no
// end-of-life recipe; that the guard still refuses one over such a file is
// pinned in package gitops (TestEolGuard_UnparseableBaseForbidsEveryEolRecipe)
// — the embedded recipe status map has no end-of-life recipe to send through
// the handler.
func TestPutTenant_RepairsFileExporterRejects(t *testing.T) {
	t.Parallel()
	const id = "svc-alpha"
	// #3064: gitManaged rows are not API-shaped — not YAML, not a tenant
	// document, a tagged key — so git-managed: every caller's PUT is the 409
	// TENANT_GIT_MANAGED, the platform writer's included, and the file is
	// repaired in git. #2405's rule still holds for the API-shaped file the
	// end-of-life guard cannot read (mismatched_explicit_tag).
	files := []struct {
		name, body, reason string
		gitManaged         bool
	}{
		{"unclosed_flow", "tenants:\n  " + id + ": [unclosed\n", "malformed_yaml", true},
		{"not_yaml", "{{not yaml\n", "malformed_yaml", true},
		{"top_level_list", "- a\n- b\n", "invalid_config", true},
		{"tenants_scalar", "tenants: oops\n", "invalid_config", true},
		{"duplicate_tenants_key", "tenants:\n  " + id + ":\n    mysql_connections: \"70\"\ntenants:\n  " + id + ":\n    mysql_connections: \"71\"\n", "invalid_config", true},
		{"tenants_list", "tenants:\n  - " + id + "\n", "invalid_config", true},
		{"non_utf8_sibling", "tenants:\n  !!binary dP8=:\n    mysql_connections: \"10\"\n  " + id + ":\n    mysql_connections: \"70\"\n", "invalid_config", true},
		// #3063: an explicit tag its own text does not satisfy. The generic
		// decode the end-of-life guard reads the current file with refuses
		// it, so it is replaceable only with write on all tenants.
		{"mismatched_explicit_tag", "tenants:\n  " + id + ":\n    mysql_connections: !!int zz\n", "invalid_config", false},
	}
	const repaired = "tenants:\n  " + id + ":\n    mysql_connections: \"75\"\n"
	for _, c := range files {
		for _, caller := range repairCallers {
			t.Run(c.name+"/"+caller.name, func(t *testing.T) {
				t.Parallel()
				dir := setupConfigDir(t, map[string]string{id + ".yaml": c.body, "_defaults.yaml": caDefaults})
				initGitRepo(t, dir)
				deps := &Deps{ConfigDir: dir, State: mustDirState(dir), Writer: newTestWriter(dir), RBAC: newRBACManager(t, repairRBACYAML), WriteMode: WriteModeDirect}
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
				w := servePopulatingRBAC(t, PutTenant(deps), req, "alice@example.com", caller.groups)
				after := mustRead(t, filepath.Join(dir, id+".yaml"))
				if c.gitManaged {
					if w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), string(errcode.TenantGitManaged)) {
						t.Fatalf("PUT status = %d, want 409 %s; body: %s", w.Code, errcode.TenantGitManaged, w.Body.String())
					}
					if after != c.body {
						t.Errorf("file changed on a refused PUT:\n%s", after)
					}
					return
				}
				if !caller.platformWritten {
					if w.Code != http.StatusBadRequest {
						t.Fatalf("PUT status = %d, want 400; body: %s", w.Code, w.Body.String())
					}
					if !strings.Contains(w.Body.String(), "write permission on all tenants") {
						t.Errorf("refusal does not name the permission it needs: %s", w.Body.String())
					}
					if after != c.body {
						t.Errorf("file changed on a refused PUT:\n%s", after)
					}
					return
				}
				if w.Code != http.StatusOK {
					t.Fatalf("PUT status = %d, want 200; body: %s", w.Code, w.Body.String())
				}
				want, wantConfigError := repaired, ""
				if after != want {
					t.Errorf("file after PUT:\n%s\nwant:\n%s", after, want)
				}
				if got := getConfigError(); got != wantConfigError {
					t.Errorf("after: GET config_error = %q, want %q", got, wantConfigError)
				}
			})
		}
	}
}

// unreadableForEol is an API-shaped tenant file (#3064) the end-of-life guard
// cannot read (#3063: an explicit tag its text does not satisfy), so #2405's
// rule decides who may replace it.
func unreadableForEol(id string) string {
	return "tenants:\n  " + id + ":\n    mysql_connections: !!int zz\n"
}

// #2405: in direct write mode the dry-run (POST /validate) answers what this
// caller's PUT would: over a file that cannot be parsed, invalid for a caller
// scoped to the tenant, valid for a caller with write on all tenants. (In PR
// mode the dry-run checks only the body and cannot answer this.)
func TestValidateTenant_UnparseableBaseFollowsCaller(t *testing.T) {
	t.Parallel()
	const id = "svc-alpha"
	const repaired = "tenants:\n  " + id + ":\n    mysql_connections: \"75\"\n"
	for _, caller := range repairCallers {
		t.Run(caller.name, func(t *testing.T) {
			t.Parallel()
			// #3064: a file that is not YAML is git-managed; #2405's rule is
			// about an API-shaped file the end-of-life guard cannot read.
			dir := setupConfigDir(t, map[string]string{id + ".yaml": unreadableForEol(id), "_defaults.yaml": caDefaults})
			initGitRepo(t, dir)
			deps := &Deps{ConfigDir: dir, State: mustDirState(dir), Writer: newTestWriter(dir), RBAC: newRBACManager(t, repairRBACYAML), WriteMode: WriteModeDirect}
			req := newRequestWithChiParam("POST", "/api/v1/tenants/"+id+"/validate", "id", id, bytes.NewBufferString(repaired))
			w := servePopulatingRBAC(t, ValidateTenant(deps), req, "alice@example.com", caller.groups)
			var got ValidateResponse
			if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil || w.Code != http.StatusOK {
				t.Fatalf("status = %d, err = %v; body: %s", w.Code, err, w.Body.String())
			}
			if got.Valid != caller.platformWritten {
				t.Errorf("valid = %v, want %v; warnings: %q", got.Valid, caller.platformWritten, got.Warnings)
			}
		})
	}
}

// #2405: the same rule in PR write mode — the caller's bit must reach
// WritePR (local tree and origin hold the same file here, so this pins the
// bit's path, not which of the two WritePR reads): over a
// current file that cannot be parsed, a caller scoped to the tenant is refused
// (400, no PR opened, no branch left behind); a caller with write on all
// tenants gets the PR.
func TestPutTenant_PRMode_UnparseableBaseFollowsCaller(t *testing.T) {
	const id = "svc-alpha"
	const repaired = "tenants:\n  " + id + ":\n    mysql_connections: \"75\"\n"
	for _, caller := range repairCallers {
		t.Run(caller.name, func(t *testing.T) {
			dir := setupConfigDir(t, map[string]string{id + ".yaml": unreadableForEol(id), "_defaults.yaml": caDefaults})
			initGitRepo(t, dir)
			runGit(t, dir, "branch", "-M", "main")
			bare := t.TempDir()
			runGit(t, bare, "init", "--bare", "-b", "main")
			runGit(t, dir, "remote", "add", "origin", bare)
			runGit(t, dir, "push", "origin", "main")
			created := false
			mc := &mockPlatformClient{providerName: "github", createPRFunc: func(title, body, head string, labels []string) (*platform.PRInfo, error) {
				created = true
				return &platform.PRInfo{Number: 7, WebURL: "https://example.invalid/pr/7", State: "open"}, nil
			}}
			deps := &Deps{ConfigDir: dir, State: mustDirState(dir), Writer: newTestWriter(dir), RBAC: newRBACManager(t, repairRBACYAML),
				WriteMode: WriteModePR, PRClient: mc, PRTracker: &mockPlatformTracker{}}
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+id, "id", id, bytes.NewBufferString(repaired))
			w := servePopulatingRBAC(t, PutTenant(deps), req, "alice@example.com", caller.groups)
			if caller.platformWritten {
				if w.Code != http.StatusOK || !created {
					t.Fatalf("status = %d, PR created = %v; want 200 and a PR; body: %s", w.Code, created, w.Body.String())
				}
				return
			}
			if w.Code != http.StatusBadRequest || created {
				t.Fatalf("status = %d, PR created = %v; want 400 and no PR; body: %s", w.Code, created, w.Body.String())
			}
			if !strings.Contains(w.Body.String(), "write permission on all tenants") {
				t.Errorf("refusal does not name the permission it needs: %s", w.Body.String())
			}
			branches, _ := exec.Command("git", "-C", dir, "branch", "--format=%(refname:short)").Output()
			if strings.TrimSpace(string(branches)) != "main" {
				t.Errorf("branch left behind:\n%s", branches)
			}
		})
	}
}

// #3064: a broken file shared with another tenant is git-managed, whoever
// asks. A body declaring {id} alone is the 409 TENANT_GIT_MANAGED — for the
// caller scoped to {id} and for the platform writer alike (#2405's bit no
// longer reaches such a file); a body that also declares the co-resident
// section is not API-shaped, the 400 body refusal, before the file is read.
// The file is repaired in git. (Until #3064 the platform writer could
// replace such a file whole, and a caller scoped to {id} could write its own
// section of the non-UTF-8 one.)
func TestPutTenant_BrokenSharedFileCoResident(t *testing.T) {
	t.Parallel()
	const id, other = "svc-alpha", "svc-beta"
	const sections = "  " + id + ":\n    mysql_connections: \"70\"\n  " + other + ":\n    mysql_connections: \"60\"\n"
	const unparseable = "tenants:\n" + sections + "  oops: [unclosed\n"
	const nonUTF8 = "tenants:\n  !!binary dP8=:\n    mysql_connections: \"10\"\n" + sections
	const ownOnly = "tenants:\n  " + id + ":\n    mysql_connections: \"75\"\n"
	const withOther = ownOnly + "  " + other + ":\n    mysql_connections: \"60\"\n"
	cases := []struct {
		name, base, body, group string
		wantConfigError         string
		wantCode                int
		wantRefusal             string
	}{
		{"unparseable_own_section_only_scoped_is_refused", unparseable, ownOnly, "ops", "malformed_yaml", http.StatusConflict, string(errcode.TenantGitManaged)},
		{"unparseable_own_section_only_platform_writer_is_refused", unparseable, ownOnly, "platform", "malformed_yaml", http.StatusConflict, string(errcode.TenantGitManaged)},
		{"unparseable_with_co_resident_is_refused", unparseable, withOther, "ops", "malformed_yaml", http.StatusBadRequest, "and no other tenant"},
		{"unparseable_with_co_resident_platform_writer_is_refused", unparseable, withOther, "platform", "malformed_yaml", http.StatusBadRequest, "and no other tenant"},
		{"non_utf8_with_co_resident_is_refused", nonUTF8, ownOnly, "ops", "invalid_config", http.StatusConflict, string(errcode.TenantGitManaged)},
		{"non_utf8_with_co_resident_platform_writer_is_refused", nonUTF8, ownOnly, "platform", "invalid_config", http.StatusConflict, string(errcode.TenantGitManaged)},
		{"non_utf8_declaring_co_resident_scoped_is_refused", nonUTF8, withOther, "ops", "invalid_config", http.StatusBadRequest, "and no other tenant"},
	}
	kept := map[string]string{}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			broken := c.base
			dir := setupConfigDir(t, map[string]string{id + ".yaml": broken, "_defaults.yaml": caDefaults})
			initGitRepo(t, dir)
			deps := &Deps{ConfigDir: dir, State: mustDirState(dir), Writer: newTestWriter(dir), RBAC: newRBACManager(t, repairRBACYAML), WriteMode: WriteModeDirect}
			// The base really is one GET flags (and with the reason this case
			// is about), so a 200 below is a repair, not a healthy write.
			gw := httptest.NewRecorder()
			GetTenant(deps)(gw, newRequestWithChiParam("GET", "/api/v1/tenants/"+id, "id", id, nil))
			var got struct {
				ConfigError string `json:"config_error"`
			}
			if err := json.Unmarshal(gw.Body.Bytes(), &got); err != nil || gw.Code != http.StatusOK {
				t.Fatalf("GET status = %d, err = %v; body: %s", gw.Code, err, gw.Body.String())
			}
			if got.ConfigError != c.wantConfigError {
				t.Fatalf("before: GET config_error = %q, want %q", got.ConfigError, c.wantConfigError)
			}
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+id, "id", id, bytes.NewBufferString(c.body))
			w := servePopulatingRBAC(t, PutTenant(deps), req, "alice@example.com", []string{c.group})
			if w.Code != c.wantCode {
				t.Fatalf("PUT status = %d, want %d; body: %s", w.Code, c.wantCode, w.Body.String())
			}
			after := mustRead(t, filepath.Join(dir, id+".yaml"))
			if c.wantCode == http.StatusOK {
				want, ok := kept[c.name]
				if !ok {
					want = c.body
				}
				if after != want {
					t.Errorf("file after PUT = %q, want %q", after, want)
				}
				return
			}
			if !strings.Contains(w.Body.String(), c.wantRefusal) {
				t.Errorf("refusal does not contain %q: %s", c.wantRefusal, w.Body.String())
			}
			if after != broken {
				t.Errorf("file changed on a refused PUT:\n%s", after)
			}
		})
	}
}

// #2903 (ADR-036 §4): a file threshold-exporter loads (GET reports no
// config_error) whose re-encode would change content a partial write does
// not edit (verifyPartialWrite) is refused by every partial write,
// config_error not_round_trippable, and its bytes stay.
// batch_patch_diff_test.go holds the invariant to the generator itself.
//
// #3064: every shape here is API-shaped — db-a's file holds db-a alone. The
// shared shapes that stood here until then (a second document for db-z, a
// section aliased by db-z, a block scalar in db-z's section) are git-managed
// now, refused before the round-trip check (tenant_git_managed_test.go).
var roundTripRejFiles = []struct {
	name, body string
}{
	{"block_scalar_leading_blank", "tenants:\n  db-a:\n    mysql_connections: \"70\"\n" + rejExistingAlert +
		"    _metadata:\n      owner: |-\n\n        team\n"},
}

func TestPartialWrites_RefuseBaseThatCannotRoundTrip(t *testing.T) {
	t.Parallel()
	for _, c := range roundTripRejFiles {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir := rejDir(t, c.body)
			path := filepath.Join(dir, "db-a.yaml")
			before := mustRead(t, path)
			list := []map[string]any{{
				"recipe": "threshold", "name": "new_y", "metric": "m2", "threshold": "2", "window": "5m",
			}}

			// A caller who may read db-z too is shown the file and reaches
			// the round-trip check.
			deps := &Deps{ConfigDir: dir, State: mustDirState(dir), Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBACWithZ)}

			// GET still serves it: the exporter loads the file.
			gw := servePopulatingRBAC(t, GetTenant(deps),
				newRequestWithChiParam("GET", "/api/v1/tenants/db-a", "id", "db-a", nil), "alice@example.com", []string{"dba"})
			var got struct {
				SourceHash   string           `json:"source_hash"`
				CustomAlerts []map[string]any `json:"custom_alerts"`
				ConfigError  string           `json:"config_error"`
			}
			if err := json.Unmarshal(gw.Body.Bytes(), &got); err != nil || gw.Code != http.StatusOK || got.ConfigError != "" {
				t.Fatalf("GET = %d %s (%v); want 200 without config_error", gw.Code, gw.Body.String(), err)
			}

			// PUT .../custom-alerts: 409 with the generator's reason.
			body, _ := json.Marshal(map[string]any{"base_hash": got.SourceHash, "custom_alerts": append(got.CustomAlerts, list...)})
			resp := putCustomAlerts(t, deps, "db-a", string(body), "alice@example.com", []string{"dba"})
			rb, _ := readBody(resp)
			var env map[string]any
			if err := json.Unmarshal([]byte(rb), &env); err != nil {
				t.Fatalf("unmarshal %s: %v", rb, err)
			}
			if resp.StatusCode != http.StatusConflict || env["code"] != wantNotLoadableCode ||
				env["tenant_id"] != "db-a" || env["config_error"] != "not_round_trippable" {
				t.Errorf("custom-alerts: %d %v; want 409 %s, config_error not_round_trippable", resp.StatusCode, env, wantNotLoadableCode)
			}
			if msg, _ := env["error"].(string); !strings.Contains(msg, "change content the update does not edit") {
				t.Errorf("custom-alerts message does not say the file would not round-trip: %q", msg)
			}

			// The batch patch (POST /tenants/batch direct mode, the group batch).
			res := applyPatch(context.Background(), newTestWriter(dir), dir,
				BatchOperation{TenantID: "db-a", Patch: map[string]string{"_silent_mode": "warning"}}, "op@example.com")
			if res.Status != "error" || res.Code != wantNotLoadableCode || !strings.Contains(res.Message, "config_error: not_round_trippable") {
				t.Errorf("batch: result = %+v, want %s naming config_error not_round_trippable", res, wantNotLoadableCode)
			}
			if after := mustRead(t, path); after != before {
				t.Errorf("file changed:\nbefore:\n%s\nafter:\n%s", before, after)
			}
		})
	}
}

// PR mode judges the base the PR branches from: the same refusal, no PR.
func TestBatchTenants_PRMode_RefusesBaseThatCannotRoundTrip(t *testing.T) {
	for _, c := range roundTripRejFiles {
		t.Run(c.name, func(t *testing.T) {
			dir := setupConfigDir(t, map[string]string{"db-a.yaml": c.body, "db-b.yaml": rejHealthyB, "_defaults.yaml": caDefaults})
			initGitRepo(t, dir)
			runGit(t, dir, "branch", "-M", "main")
			w, created := prBatchRun(t, dir, rejTwoOps)
			assertRejectedOp(t, w, 1, "db-a", wantNotLoadableCode, "config_error: not_round_trippable")
			if created {
				t.Error("a PR was created over a base that cannot round-trip")
			}
		})
	}
}

// #2903: no refusal of a partial write quotes another tenant's value. The
// file db-a shares with db-z holds, in db-z's block, values a decoder or the
// round-trip check chokes on; the custom-alerts PUT and the batch (direct
// and PR mode) for db-a must answer without any of them.
func TestPartialWrites_NeverQuoteAnotherTenantsValue(t *testing.T) {
	for i, other := range []string{
		"    secret_x: !!int SECRETINT\n",
		"    _routing:\n      receiver: {type: webhook, url: !!timestamp \"SECRETTS\"}\n",
		"    k: !!binary \"@@SECRETBIN@@\"\n",
		"    b: |\n\n      SECRETMULTI\n",
		"    k: [1:20, SECRETFLOW]\n",
		"    secret_y: SECRETDUP\n    secret_y: SECRETDUP2\n",
		"    _metadata: {owner: SECRETMETA, owner: x}\n",
	} {
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			file := "tenants:\n  db-a:\n    mysql_connections: \"70\"\n" + rejExistingAlert + "  db-z:\n" + other
			dir := rejDir(t, file)
			deps := &Deps{ConfigDir: dir, State: mustDirState(dir), Writer: newTestWriter(dir), RBAC: newRBACManager(t, caWriteRBAC)}
			body, _ := json.Marshal(map[string]any{"base_hash": cfg.ComputeSourceHash([]byte(file)),
				"custom_alerts": []map[string]any{{"recipe": "threshold", "name": "n", "metric": "m", "threshold": "2", "window": "5m"}}})
			resp := putCustomAlerts(t, deps, "db-a", string(body), "alice@example.com", []string{"dba"})
			ca, _ := readBody(resp)
			res := applyPatch(context.Background(), newTestWriter(dir), dir,
				BatchOperation{TenantID: "db-a", Patch: map[string]string{"_silent_mode": "warning"}}, "op@example.com")
			pdir := setupConfigDir(t, map[string]string{"db-a.yaml": file, "db-b.yaml": rejHealthyB, "_defaults.yaml": caDefaults})
			initGitRepo(t, pdir)
			runGit(t, pdir, "branch", "-M", "main")
			w, _ := prBatchRun(t, pdir, rejTwoOps)
			for name, txt := range map[string]string{"custom-alerts": ca, "batch": res.Message, "pr batch": w.Body.String()} {
				if strings.Contains(txt, "SECRET") {
					t.Errorf("%s quotes db-z's value: %s", name, txt)
				}
			}
		})
	}
}
