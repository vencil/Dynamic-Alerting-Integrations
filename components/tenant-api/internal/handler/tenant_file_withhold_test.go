package handler

// #3050: a conf.d file can declare several tenants, so an answer built on the
// whole file is withheld unless the caller may read every tenant the file
// declares (fileShown, on tenantNamesFor). Since #3064 GET /tenants/{id} and
// the writes do not ask: they show and write only an API-shaped file, the
// tenant's alone (tenant_git_managed_test.go). What still asks is
// /effective — its source_hash and decode-error text, about the tenant file
// and the root platform files, which legitimately hold several tenants. Each
// check below inspects the serialized response bytes, then makes the other
// tenant readable and expects the full answer back. The helpers here serve
// tenant_git_managed_test.go too.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// The other tenant's block: its name (orgEnfTenantOut) and every value in it
// are what no answer to a caller who may not read it may carry.
const (
	wOutOwner  = "OUTOWNERQX"
	wOutSecret = "OUTSECRETQX"
	wOutValue  = "777.125"
)

var wOutBlock = "  " + orgEnfTenantOut + ":\n" +
	"    mysql_connections: \"" + wOutValue + "\"\n" +
	"    _metadata:\n      owner: " + wOutOwner + "\n" +
	"    _routing:\n      receiver:\n        type: webhook\n        url: \"https://hooks.example/" + wOutSecret + "\"\n"

// wFiles are the shapes of a file declaring both tenants: one the exporter
// loads, and the config_error one (invalid_config: a duplicate key, which a
// node parse still lists the tenants of).
var wFiles = map[string]struct{ body, configError string }{
	"loadable": {"tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n" + wOutBlock, ""},
	"config_error": {"tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n" + wOutBlock +
		"    mysql_connections: \"1\"\n", "invalid_config"},
}

// wReadOnlyRBAC: read on every tenant in the caller's org, no write — the
// masked (#1560) caller.
const wReadOnlyRBAC = `groups:
  - name: ` + orgEnfGroup + `
    tenants: ["*"]
    permissions: [read]
    org-scope: org
`

// wUnrestrictedRBAC: read and write on every tenant, on every scope axis
// (rbac.PlatformUnrestricted) — write, so credentials are shown and the file
// is not withheld for want of a certain mask.
const wUnrestrictedRBAC = `groups:
  - name: ` + orgEnfGroup + `
    tenants: ["*"]
    permissions: [read, write]
`

// wFixture is newOrgEnfFixture with the caller's rule: "writer" (read, write,
// admin in its org), "reader" (wReadOnlyRBAC) or "unrestricted"
// (wUnrestrictedRBAC).
func wFixture(t *testing.T, role, file string) *orgEnfFixture {
	t.Helper()
	f := newOrgEnfFixture(t, map[string]string{
		"_defaults.yaml":         "defaults:\n  mysql_connections: 5\n",
		orgEnfTenantIn + ".yaml": file,
	})
	switch role {
	case "reader", "unrestricted":
		y := wReadOnlyRBAC
		if role == "unrestricted" {
			y = wUnrestrictedRBAC
		}
		f.rbacMgr = newRBACManagerWithClaims(t, y, map[string]string{"org": orgEnfClaimHeader})
		f.rbacMgr.EnableOrgScopeEnforce()
	}
	return f
}

// makeOutReadable puts the other tenant in the caller's org.
func makeOutReadable(f *orgEnfFixture) {
	f.tenantOrg.Override(&tenantorg.Config{TenantOrgs: map[string][]string{
		orgEnfTenantIn: {orgEnfMemberOrg}, orgEnfTenantOut: {orgEnfMemberOrg}}})
}

// wServe runs h for tenantOrgIn as the member caller, behind the read
// middleware (the handler judges the rest).
func wServe(f *orgEnfFixture, h http.HandlerFunc, perm rbac.Permission, method, body string, header map[string]string) *httptest.ResponseRecorder {
	req := newRequestWithChiParam(method, "/api/v1/tenants/"+orgEnfTenantIn, "id", orgEnfTenantIn, bytes.NewBufferString(body))
	for k, v := range header {
		req.Header.Set(k, v)
	}
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, f.rbacMgr, perm, nil).ServeHTTP(w, orgEnfIdentity(req, orgEnfMemberOrg))
	return w
}

// assertNoOtherTenant fails when the response bytes carry the other tenant's
// name, any of its values, or the hash of the whole file.
func assertNoOtherTenant(t *testing.T, what string, body []byte, file string) {
	t.Helper()
	for _, m := range []string{orgEnfTenantOut, wOutOwner, wOutSecret, wOutValue, cfg.ComputeSourceHash([]byte(file))} {
		if bytes.Contains(body, []byte(m)) {
			t.Errorf("%s: the response carries %q, which only a reader of %s may see: %s", what, m, orgEnfTenantOut, body)
		}
	}
}

func decodeDetail(t *testing.T, w *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &m); err != nil {
		t.Fatalf("unmarshal: %v: %s", err, w.Body.String())
	}
	return m
}

func TestDeclaredTenantIDs(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		name, data string
		ids        []string
		ok         bool
	}{
		{"empty", "", nil, true},
		{"comment only", "# nothing\n", nil, true},
		{"null tenants", "tenants:\n", nil, true},
		{"two", "tenants:\n  a: {}\n  b: {x: 1}\n", []string{"a", "b"}, true},
		{"second document counts", "tenants:\n  a: {}\n---\ntenants:\n  b: {}\n  a: {}\n", []string{"a", "b"}, true},
		{"other top-level keys", "defaults:\n  x: 1\ntenants:\n  a: {}\n", []string{"a"}, true},
		{"not YAML", "tenants:\n  a: {\n", nil, false},
		{"root is a list", "- tenants\n", nil, false},
		{"tenants is a list", "tenants:\n  - a\n", nil, false},
		{"tenants is an alias", "x: &t\n  b: {}\ntenants: *t\n", nil, false},
		{"merge key in tenants", "x: &t\n  b: {}\ntenants:\n  a: {}\n  <<: *t\n", nil, false},
		{"merge key at the top", "x: &t\n  tenants: {b: {}}\n<<: *t\n", nil, false},
		{"alias as a tenant key", "x: &k b\ntenants:\n  *k : {}\n", nil, false},
		{"non-scalar tenant key", "tenants:\n  ? [b]\n  : {}\n", nil, false},
		// #3050 round 2: yaml.v3 decodes a !!binary key, so the text is not
		// the id — `dGVuYW50cw==` is "tenants", `ZGItYg==` is "db-b".
		{"!!binary tenants key", "? !!binary dGVuYW50cw==\n: {a: {x: \"1\"}, b: {y: \"2\"}}\n", nil, false},
		{"!!binary tenants key, second document", "tenants:\n  a: {}\n---\n? !!binary dGVuYW50cw==\n: {b: {}}\n", nil, false},
		{"!!binary tenant id", "tenants:\n  !!binary ZGItYg==: {x: \"1\"}\n  a: {}\n", nil, false},
		{"!!binary tenant id, second document", "tenants:\n  a: {}\n---\ntenants:\n  !!binary ZGItYg==: {}\n", nil, false},
		{"!!int tenant id", "tenants:\n  !!int 123: {x: \"1\"}\n", nil, false},
		{"local tag on a tenant id", "tenants:\n  !custom b: {}\n", nil, false},
		{"!!timestamp tenant id", "tenants:\n  !!timestamp 2001-12-14: {}\n", nil, false},
		{"!!str tags are plain", "!!str tenants:\n  !!str b: {}\n", []string{"b"}, true},
		{"untagged non-string ids decode to their text", "tenants:\n  123: {}\n  true: {}\n  0x1F: {}\n", []string{"123", "true", "0x1F"}, true},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			ids, ok := declaredTenantIDs([]byte(c.data))
			if ok != c.ok || strings.Join(ids, ",") != strings.Join(c.ids, ",") {
				t.Errorf("declaredTenantIDs = %v, %v; want %v, %v", ids, ok, c.ids, c.ok)
			}
		})
	}
}

// #3050 round 2, property: on every shape this file and its neighbours
// probe, the tenants threshold-exporter decodes (cfg.ParseTenantFile, first
// document) are a subset of what declaredTenantIDs lists — or it says it
// cannot list them. A smaller list is the fail-open this guards.
func TestDeclaredTenantIDs_NeverFewerThanTheExporter(t *testing.T) {
	t.Parallel()
	shapes := []string{
		"? !!binary dGVuYW50cw==\n: {db-a: {x: \"1\"}, db-b: {secret: \"2\"}}\n",
		"tenants:\n  !!binary ZGItYg==: {x: \"1\"}\n  db-a: {x: \"1\"}\n",
		"tenants:\n  !!int 123: {x: \"1\"}\n",
		"tenants:\n  123: {x: \"1\"}\n  true: {}\n  0x1F: {}\n  1e3: {}\n  2001-12-14: {}\n",
		"tenants:\n  ~: {x: \"1\"}\n",
		"tenants:\n  !!str db-b: {}\n  !custom db-c: {}\n  !!float 1.0: {}\n",
		"!!str tenants:\n  db-b: {}\n",
		"tenants:\n  \"db-b\": {}\n  'db-c': {}\n",
		"tenants:\n  db-a: &b\n    x: \"1\"\n  db-z: *b\n",
		"tenants:\n  db-a: {}\n---\ntenants:\n  db-z: {}\n",
		"x: &t\n  b: {}\ntenants:\n  a: {}\n  <<: *t\n",
		"tenants:\n  !!binary dP8=:\n    x: \"10\"\n  db-a: {}\n",
		"tenants:\n  db-a: {}\n  db-a: {}\n",
	}
	for _, f := range wFiles {
		shapes = append(shapes, f.body)
	}
	for _, f := range rejFiles {
		shapes = append(shapes, f.body)
	}
	for _, f := range roundTripRejFiles {
		shapes = append(shapes, f.body)
	}
	for _, data := range shapes {
		c, err := cfg.ParseTenantFile([]byte(data))
		if err != nil {
			continue // the exporter declares nothing from a file it rejects
		}
		ids, ok := declaredTenantIDs([]byte(data))
		if !ok {
			continue
		}
		listed := map[string]bool{}
		for _, id := range ids {
			listed[id] = true
		}
		for id := range c.Tenants {
			if !listed[id] {
				t.Errorf("%q: the exporter declares %q, declaredTenantIDs lists only %q", data, id, ids)
			}
		}
	}
}

// A nil TenantNames shows no file — not even one declaring only the
// requested tenant, nor an empty one — and never falls back to the
// unrestricted answer.
func TestFileTenantsShown_NilNamesFailsClosed(t *testing.T) {
	t.Parallel()
	all := gitops.TenantNames(func(string) bool { return true })
	yes := func() bool { return true }
	for _, data := range []string{"", "tenants:\n  a: {}\n", "tenants:\n  a: {}\n  b: {}\n", "not: [yaml\n"} {
		if fileTenantsShown([]byte(data), "a", nil, yes) {
			t.Errorf("nil names showed %q", data)
		}
		if !fileTenantsShown([]byte(data), "a", all, yes) {
			t.Errorf("names showing every tenant did not show %q", data)
		}
	}
	only := gitops.TenantNames(func(id string) bool { return id == "a" })
	if fileTenantsShown([]byte("tenants:\n  a: {}\n  b: {}\n"), "a", only, yes) {
		t.Error("a file declaring an unreadable tenant was shown")
	}
	if fileTenantsShown([]byte("not: [yaml\n"), "a", all, func() bool { return false }) {
		t.Error("an unlistable file was shown to a caller who may not read every tenant")
	}
}

func TestGetTenantEffective_SourceHashWithheld(t *testing.T) {
	t.Parallel()
	file := wFiles["loadable"].body
	f := wFixture(t, "writer", file)
	w := wServe(f, GetTenantEffective(f.deps()), rbac.PermRead, "GET", "", nil)
	assertNoOtherTenant(t, "effective", w.Body.Bytes(), file)
	var resp TenantEffectiveResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil || w.Code != http.StatusOK ||
		resp.SourceHash != "" || !resp.SourceHashWithheld || resp.MergedHash == "" {
		t.Fatalf("%d %s (%v); want source_hash withheld, merged_hash kept", w.Code, w.Body.String(), err)
	}
	makeOutReadable(f)
	w = wServe(f, GetTenantEffective(f.deps()), rbac.PermRead, "GET", "", nil)
	resp = TenantEffectiveResponse{}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil ||
		resp.SourceHash != cfg.ComputeSourceHash([]byte(file)) || resp.SourceHashWithheld {
		t.Errorf("readable: %s (%v); want source_hash as today", w.Body.String(), err)
	}
}

// A yaml.v3 type error in the other tenant's block quotes its value; the
// writer of this tenant gets a fixed text instead.
//
// Since #3063 a TENANT file of that shape never reaches the decode:
// ParseTenantFile refuses it whole, so /effective answers 404 and carries
// nothing of the file. The root `_defaults.yaml` is the file that still gets
// there — its `tenants:` block can name the other tenant — so the withheld
// text is pinned on it.
func TestGetTenantEffective_DecodeErrorWithheld(t *testing.T) {
	t.Parallel()
	file := "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n" +
		"  " + orgEnfTenantOut + ":\n    mysql_connections: !!int " + wOutOwner + "\n"
	f := wFixture(t, "writer", file)
	w := wServe(f, GetTenantEffective(f.deps()), rbac.PermRead, "GET", "", nil)
	if w.Code != http.StatusNotFound {
		t.Fatalf("tenant file: %d %s, want 404 (the file is rejected whole, #3063)", w.Code, w.Body.String())
	}
	assertNoOtherTenant(t, "effective, rejected tenant file", w.Body.Bytes(), file)

	root := "defaults:\n  mysql_connections: 5\ntenants:\n  " + orgEnfTenantOut + ":\n    mysql_connections: !!int " + wOutOwner + "\n"
	f = newOrgEnfFixture(t, map[string]string{
		"_defaults.yaml":         root,
		orgEnfTenantIn + ".yaml": "tenants:\n  " + orgEnfTenantIn + ":\n    _silent_mode: \"critical\"\n",
	})
	w = wServe(f, GetTenantEffective(f.deps()), rbac.PermRead, "GET", "", nil)
	if w.Code != http.StatusInternalServerError || !strings.Contains(w.Body.String(), CodeConfigDecode) {
		t.Fatalf("root defaults: %d %s, want 500 %s", w.Code, w.Body.String(), CodeConfigDecode)
	}
	assertNoOtherTenant(t, "effective decode error", w.Body.Bytes(), root)
	makeOutReadable(f)
	w = wServe(f, GetTenantEffective(f.deps()), rbac.PermRead, "GET", "", nil)
	if !strings.Contains(w.Body.String(), wOutOwner) {
		t.Errorf("readable: %s, want the decoder's message as today", w.Body.String())
	}
}
