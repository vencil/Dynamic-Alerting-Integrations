package handler

// #3064: tenant-api manages a tenant only when its conf.d file is API-shaped
// (confd.TenantFileManagement) — the file is then the tenant's alone. Every
// other tenant is git-managed: GET answers its effective view only, marked
// managed_by: git, and every write path refuses it with 409
// TENANT_GIT_MANAGED, for every caller, naming no other tenant. The fixtures
// are tenant_file_withhold_test.go's (wFiles: In's file shared with Out).

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// b64 is s as a `!!binary` scalar's text.
func b64(s string) string { return base64.StdEncoding.EncodeToString([]byte(s)) }

// gmOwnFile is In's file API-shaped: the control.
var gmOwnFile = "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n" + rejExistingAlert

// assertGitManagedDetail checks a GET answer for a git-managed tenant: the
// marker and reason, and none of the fields read off the file's text.
func assertGitManagedDetail(t *testing.T, w *httptest.ResponseRecorder, reason string) map[string]any {
	t.Helper()
	if w.Code != http.StatusOK {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	d := decodeDetail(t, w)
	if d["managed_by"] != "git" || d["git_managed_reason"] != reason {
		t.Errorf("managed_by = %v, git_managed_reason = %v; want git, %s: %s", d["managed_by"], d["git_managed_reason"], reason, w.Body.String())
	}
	for _, k := range []string{"raw_yaml", "source_hash", "custom_alerts", "masked", "raw_yaml_withheld"} {
		if v, ok := d[k]; ok {
			t.Errorf("%s present (%v) on a git-managed tenant", k, v)
		}
	}
	return d
}

// GET for a git-managed tenant: the same answer for a writer, a reader and an
// unrestricted caller, before and after Out becomes readable — never the
// file, never its hash.
func TestGetTenant_GitManagedSharedFile(t *testing.T) {
	t.Parallel()
	for name, file := range wFiles {
		for _, role := range []string{"writer", "reader", "unrestricted"} {
			t.Run(name+"/"+role, func(t *testing.T) {
				t.Parallel()
				f := wFixture(t, role, file.body)
				for _, readable := range []bool{false, true} {
					if readable {
						makeOutReadable(f)
					}
					w := wServe(f, GetTenant(f.deps()), rbac.PermRead, "GET", "", nil)
					d := assertGitManagedDetail(t, w, "tenant_count")
					if got, _ := d["config_error"].(string); got != file.configError {
						t.Errorf("config_error = %q, want %q", got, file.configError)
					}
					// The effective view: thresholds for a loadable file,
					// absent for one the exporter skips.
					if _, ok := d["resolved_thresholds"]; ok != (file.configError == "") {
						t.Errorf("resolved_thresholds present = %v, want %v: %s", ok, file.configError == "", w.Body.String())
					}
					if role != "unrestricted" && !readable {
						assertNoOtherTenant(t, "GET", w.Body.Bytes(), file.body)
					}
					if bytes.Contains(w.Body.Bytes(), []byte(cfg.ComputeSourceHash([]byte(file.body)))) {
						t.Errorf("the answer carries the shared file's hash: %s", w.Body.String())
					}
				}
			})
		}
	}
}

// The other git-managed shapes GET reports, with their reasons — the
// `? !!binary` co-tenant key the decoder reads as In itself among them.
func TestGetTenant_GitManagedShapes(t *testing.T) {
	t.Parallel()
	in := orgEnfTenantIn
	for _, c := range []struct{ name, file, reason string }{
		{"binary co-tenant key decoding to own id",
			"tenants:\n  " + in + ":\n    _silent_mode: \"critical\"\n  ? !!binary " + b64(in) + "\n  :\n    _silent_mode: \"warning\"\n    x_note: " + wOutSecret + "\n",
			"yaml_indirection"},
		{"stem is not the declared id", "tenants:\n  " + orgEnfTenantOut + ":\n    x_note: " + wOutSecret + "\n", "id_mismatch"},
		{"merge key at the top level", "base: &b\n  x_note: " + wOutSecret + "\n<<: *b\ntenants:\n  " + in + ":\n    _silent_mode: \"critical\"\n", "yaml_indirection"},
		{"second document", "tenants:\n  " + in + ":\n    _silent_mode: \"critical\"\n---\ntenants:\n  " + orgEnfTenantOut + ":\n    x_note: " + wOutSecret + "\n", "several_documents"},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			f := wFixture(t, "unrestricted", c.file)
			w := wServe(f, GetTenant(f.deps()), rbac.PermRead, "GET", "", nil)
			assertGitManagedDetail(t, w, c.reason)
			if strings.Contains(w.Body.String(), wOutSecret) {
				t.Errorf("the answer carries the file's content: %s", w.Body.String())
			}
		})
	}
}

// The control: In's own file, API-shaped — anchors inside its own section
// included — is shown whole, with its hash, managed_by api.
func TestGetTenant_APIManagedFile(t *testing.T) {
	t.Parallel()
	for _, file := range []string{gmOwnFile,
		"tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: &m \"critical\"\n    _severity_dedup: *m\n"} {
		f := wFixture(t, "writer", file)
		w := wServe(f, GetTenant(f.deps()), rbac.PermRead, "GET", "", nil)
		d := decodeDetail(t, w)
		if d["managed_by"] != "api" || d["raw_yaml"] != file || d["source_hash"] != cfg.ComputeSourceHash([]byte(file)) {
			t.Errorf("own file: %s; want managed_by api, the file and its hash", w.Body.String())
		}
		if _, ok := d["git_managed_reason"]; ok {
			t.Errorf("git_managed_reason on an API-managed tenant: %s", w.Body.String())
		}
	}
}

// gmGroupBatch sends a group batch over In as the member caller.
func gmGroupBatch(t *testing.T, f *orgEnfFixture) *httptest.ResponseRecorder {
	t.Helper()
	setupGroupsFile(t, f.configDir, "groups:\n  g1:\n    label: G1\n    members:\n      - "+orgEnfTenantIn+"\n")
	d := f.deps()
	d.Groups = groups.NewDirManager(f.configDir)
	req := newRequestWithChiParam("POST", "/api/v1/groups/g1/batch", "id", "g1",
		bytes.NewBufferString(`{"patch":{"_silent_mode":"warning"}}`))
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(GroupBatch(d), f.rbacMgr, rbac.PermRead, nil).ServeHTTP(w, orgEnfIdentity(req, orgEnfMemberOrg))
	return w
}

// Every write path, and every preview of one, refuses a git-managed tenant
// with the one code — the writer of In, a caller who may write both
// tenants, the base hash of the shared file in hand — and writes nothing.
// The control rows send the same requests over In's own file.
func TestWritePaths_RefuseGitManagedTenant(t *testing.T) {
	t.Parallel()
	shared := wFiles["loadable"].body
	ownBody := "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"warning\"\n"
	ct := map[string]string{"Content-Type": "application/json"}
	caBody := func(file string) string {
		b, _ := json.Marshal(PutCustomAlertsRequest{CustomAlerts: &[]map[string]any{}, BaseHash: cfg.ComputeSourceHash([]byte(file))})
		return string(b)
	}
	batchBody := `{"operations":[{"tenant_id":"` + orgEnfTenantIn + `","patch":{"_silent_mode":"warning"}}]}`
	paths := []struct {
		name string
		call func(t *testing.T, f *orgEnfFixture, file string) *httptest.ResponseRecorder
		// status a refusal answers with, and the control's.
		refused, control int
	}{
		{"PUT", func(t *testing.T, f *orgEnfFixture, _ string) *httptest.ResponseRecorder {
			return wServe(f, PutTenant(f.deps()), rbac.PermWrite, "PUT", ownBody, nil)
		}, http.StatusConflict, http.StatusOK},
		// The shared file's real hash: the git-managed verdict comes first,
		// so CONFLICT-or-not cannot confirm a guess of it.
		{"PUT with X-DA-Base-Hash", func(t *testing.T, f *orgEnfFixture, file string) *httptest.ResponseRecorder {
			return wServe(f, PutTenant(f.deps()), rbac.PermWrite, "PUT", ownBody,
				map[string]string{BaseHashHeader: cfg.ComputeSourceHash([]byte(file))})
		}, http.StatusConflict, http.StatusOK},
		{"diff", func(t *testing.T, f *orgEnfFixture, _ string) *httptest.ResponseRecorder {
			return wServe(f, DiffTenant(f.deps()), rbac.PermRead, "POST", ownBody, nil)
		}, http.StatusConflict, http.StatusOK},
		{"custom-alerts", func(t *testing.T, f *orgEnfFixture, file string) *httptest.ResponseRecorder {
			return wServe(f, PutTenantCustomAlerts(f.deps()), rbac.PermWrite, "PUT", caBody(file), ct)
		}, http.StatusConflict, http.StatusOK},
		{"batch", func(t *testing.T, f *orgEnfFixture, _ string) *httptest.ResponseRecorder {
			req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(batchBody))
			req.Header.Set("Content-Type", "application/json")
			w := httptest.NewRecorder()
			wrapWithRBACMiddleware(BatchTenants(f.deps()), f.rbacMgr, rbac.PermRead, nil).ServeHTTP(w, orgEnfIdentity(req, orgEnfMemberOrg))
			return w
		}, http.StatusUnprocessableEntity, http.StatusOK},
		{"group batch", func(t *testing.T, f *orgEnfFixture, _ string) *httptest.ResponseRecorder {
			return gmGroupBatch(t, f)
		}, http.StatusUnprocessableEntity, http.StatusOK},
	}
	for _, p := range paths {
		for _, both := range []bool{false, true} {
			name := p.name + map[bool]string{false: "/writer_of_in", true: "/writer_of_both"}[both]
			t.Run(name, func(t *testing.T) {
				t.Parallel()
				f := wFixture(t, "writer", shared)
				if both {
					f.shareOut(t)
				}
				before := foreignFile(t, f)
				w := p.call(t, f, shared)
				if w.Code != p.refused || !strings.Contains(w.Body.String(), CodeTenantGitManaged) {
					t.Fatalf("status %d (want %d %s): %s", w.Code, p.refused, CodeTenantGitManaged, w.Body.String())
				}
				assertNoOtherTenant(t, p.name, w.Body.Bytes(), shared)
				if n := f.writes.Load(); n != 0 {
					t.Errorf("writer commits = %d, want 0", n)
				}
				if after := foreignFile(t, f); after != before {
					t.Errorf("the shared file changed:\n%s", after)
				}
				if bytes.Contains(w.Body.Bytes(), []byte("current_source_hash")) {
					t.Errorf("the refusal carries a hash: %s", w.Body.String())
				}

				// Control: In's own file, the same request.
				c := wFixture(t, "writer", gmOwnFile)
				if w := p.call(t, c, gmOwnFile); w.Code != p.control {
					t.Errorf("own file: status %d, want %d: %s", w.Code, p.control, w.Body.String())
				}
			})
		}
	}
}

// /validate answers the git-managed refusal as its verdict, with the code;
// a reader of In gets the same 409 from /diff (the masked preview's
// sequence), naming no other tenant.
func TestPreviews_GitManagedVerdict(t *testing.T) {
	t.Parallel()
	shared := wFiles["loadable"].body
	ownBody := "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"warning\"\n"

	f := wFixture(t, "writer", shared)
	w := wServe(f, ValidateTenant(f.deps()), rbac.PermRead, "POST", ownBody, nil)
	var vr ValidateResponse
	if err := json.Unmarshal(w.Body.Bytes(), &vr); err != nil || w.Code != http.StatusOK {
		t.Fatalf("/validate: %d %s (%v)", w.Code, w.Body.String(), err)
	}
	if vr.Valid || vr.Code != CodeTenantGitManaged || len(vr.Warnings) != 1 || !strings.Contains(vr.Warnings[0], "git-managed (tenant_count)") {
		t.Errorf("/validate = %+v, want valid false, code %s and the refusal", vr, CodeTenantGitManaged)
	}
	assertNoOtherTenant(t, "/validate", w.Body.Bytes(), shared)

	// Control: In's own file is valid, with no code.
	c := wFixture(t, "writer", gmOwnFile)
	w = wServe(c, ValidateTenant(c.deps()), rbac.PermRead, "POST", ownBody, nil)
	vr = ValidateResponse{}
	if err := json.Unmarshal(w.Body.Bytes(), &vr); err != nil || !vr.Valid || vr.Code != "" {
		t.Errorf("/validate over In's own file: %s (%v), want valid and no code", w.Body.String(), err)
	}

	r := wFixture(t, "reader", shared)
	w = wServe(r, DiffTenant(r.deps()), rbac.PermRead, "POST", ownBody, nil)
	if w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), CodeTenantGitManaged) {
		t.Errorf("masked /diff: %d %s, want 409 %s", w.Code, w.Body.String(), CodeTenantGitManaged)
	}
	assertNoOtherTenant(t, "masked /diff", w.Body.Bytes(), shared)
}

// An API-managed tenant's stale X-DA-Base-Hash is the CONFLICT, with the
// current file's hash — the hash GET shows for it.
func TestPutTenant_StaleBaseHashOnOwnFileCarriesHash(t *testing.T) {
	t.Parallel()
	f := wFixture(t, "writer", gmOwnFile)
	body := "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"warning\"\n"
	w := wServe(f, PutTenant(f.deps()), rbac.PermWrite, "PUT", body, map[string]string{BaseHashHeader: "0000000000000000"})
	if w.Code != http.StatusConflict || decodeDetail(t, w)["current_source_hash"] != cfg.ComputeSourceHash([]byte(gmOwnFile)) {
		t.Errorf("%d %s, want 409 CONFLICT with current_source_hash", w.Code, w.Body.String())
	}
}
