package handler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
)

// Regression tests for #1673: the enumerating plane (confd.TenantIDFromFile)
// accepts `.yaml` and `.yml` in any case, while every per-tenant site used to
// join a hardcoded `<id>.yaml`. The two planes therefore disagreed about which
// file — or whether any file — is a given tenant's.

const spellingTenant = "db-a"

func spellingBody(marker string) string {
	return "tenants:\n  " + spellingTenant + ":\n    _metadata:\n      owner: " + marker + "\n"
}

// A tenant stored as `<id>.yml` is listed by GET /tenants; before #1673 the
// per-tenant read joined `<id>.yaml` and answered 404 for the same tenant.
func TestGetTenant_YmlSpellingIsReachable(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		spellingTenant + ".yml": spellingBody("FROM-YML"),
	})

	// The listing sees it...
	got, err := loadAllTenants(dir)
	if err != nil {
		t.Fatalf("loadAllTenants: %v", err)
	}
	if len(got) != 1 || got[0].ID != spellingTenant {
		t.Fatalf("loadAllTenants = %+v, want exactly one %q", got, spellingTenant)
	}

	// ...and so must read-by-id.
	w := httptest.NewRecorder()
	GetTenant(&Deps{ConfigDir: dir})(w,
		newRequestWithChiParam("GET", "/api/v1/tenants/"+spellingTenant, "id", spellingTenant, nil))

	if w.Code != http.StatusOK {
		t.Fatalf("GetTenant status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "FROM-YML") {
		t.Errorf("GetTenant did not return the .yml file's content; body=%s", w.Body.String())
	}
}

// Two files claiming one tenant is refused, not resolved by precedence: they
// can carry different metadata and thresholds, and a whole-file PUT would drop
// whichever one lost.
func TestTenantFileSpelling_AmbiguousIsRefused(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		spellingTenant + ".yaml": spellingBody("FROM-YAML"),
		spellingTenant + ".yml":  spellingBody("FROM-YML"),
	})

	t.Run("listing fails loud instead of returning the tenant twice", func(t *testing.T) {
		got, err := loadAllTenants(dir)
		if err == nil {
			t.Fatalf("loadAllTenants returned %+v and no error; want a duplicate-tenant error", got)
		}
		if !strings.Contains(err.Error(), spellingTenant) {
			t.Errorf("error does not name the tenant: %v", err)
		}
	})

	t.Run("read-by-id answers 409 rather than picking a file", func(t *testing.T) {
		w := httptest.NewRecorder()
		GetTenant(&Deps{ConfigDir: dir})(w,
			newRequestWithChiParam("GET", "/api/v1/tenants/"+spellingTenant, "id", spellingTenant, nil))

		if w.Code != http.StatusConflict {
			t.Fatalf("GetTenant status = %d, want 409; body=%s", w.Code, w.Body.String())
		}
		var env map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
			t.Fatalf("response is not JSON: %v (%s)", err, w.Body.String())
		}
		if env["code"] == nil || env["code"] == "" {
			t.Errorf("error envelope has no machine-readable code: %s", w.Body.String())
		}
	})
}

// A whole-file PUT against an ambiguous tenant is a 409, not the 400 the
// unrecognised-write fallback would give: the request is well-formed, the
// on-disk state is not.
func TestPutTenant_AmbiguousSpellingIsConflict(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		spellingTenant + ".yaml": spellingBody("FROM-YAML"),
		spellingTenant + ".yml":  spellingBody("FROM-YML"),
	})
	d := &Deps{ConfigDir: dir, Writer: newTestWriter(dir)}

	w := httptest.NewRecorder()
	PutTenant(d)(w, newRequestWithChiParam("PUT", "/api/v1/tenants/"+spellingTenant, "id", spellingTenant,
		bytes.NewBufferString(spellingBody("NEW"))))

	if w.Code != http.StatusConflict {
		t.Fatalf("PutTenant status = %d, want 409; body=%s", w.Code, w.Body.String())
	}
}

// The uppercase-extension case #1537 fixed for the enumerator must resolve on
// the per-tenant plane too, or the two planes disagree again.
func TestGetTenant_UppercaseExtensionIsReachable(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		spellingTenant + ".YAML": spellingBody("FROM-UPPER"),
	})
	w := httptest.NewRecorder()
	GetTenant(&Deps{ConfigDir: dir})(w,
		newRequestWithChiParam("GET", "/api/v1/tenants/"+spellingTenant, "id", spellingTenant, nil))

	if w.Code != http.StatusOK {
		t.Fatalf("GetTenant status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "FROM-UPPER") {
		t.Errorf("GetTenant did not return the .YAML file's content; body=%s", w.Body.String())
	}
}

// The duplicate claim must be made on the FILENAME, before the file is read or
// parsed. A malformed `<id>.yaml` used to hit the yaml.Unmarshal `continue`
// without ever reserving its id, so the valid `<id>.yml` beside it was listed
// as a perfectly ordinary tenant — while confd.ResolveTenantFile, which matches
// names and never contents, answered 409 for that same tenant. That is exactly
// the two-planes-disagree defect #1673 exists to close, reintroduced through
// the back door.
func TestTenantFileSpelling_BrokenSiblingStillClaimsTheID(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		spellingTenant + ".yaml": "tenants: [this is not: valid yaml\n",
		spellingTenant + ".yml":  spellingBody("FROM-YML"),
	})

	t.Run("listing refuses instead of silently returning the readable one", func(t *testing.T) {
		got, err := loadAllTenants(dir)
		if err == nil {
			t.Fatalf("loadAllTenants returned %+v and no error; the broken sibling must still claim %q", got, spellingTenant)
		}
		if !strings.Contains(err.Error(), spellingTenant) {
			t.Errorf("error does not name the tenant: %v", err)
		}
	})

	t.Run("read-by-id agrees with the listing", func(t *testing.T) {
		w := httptest.NewRecorder()
		GetTenant(&Deps{ConfigDir: dir})(w,
			newRequestWithChiParam("GET", "/api/v1/tenants/"+spellingTenant, "id", spellingTenant, nil))

		if w.Code != http.StatusConflict {
			t.Fatalf("GetTenant status = %d, want 409; body=%s", w.Code, w.Body.String())
		}
	})
}

// assertConflictWithoutDir requires a 409 CONFLICT envelope whose body does not
// carry dir (the conf.d root as the server sees it).
func assertConflictWithoutDir(t *testing.T, w *httptest.ResponseRecorder, dir string) map[string]any {
	t.Helper()
	if w.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409; body=%s", w.Code, w.Body.String())
	}
	var env map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("response is not JSON: %v (%s)", err, w.Body.String())
	}
	if env["code"] != CodeConflict {
		t.Errorf("code = %v, want %s; body=%s", env["code"], CodeConflict, w.Body.String())
	}
	if strings.Contains(w.Body.String(), dir) {
		t.Errorf("body leaks the conf.d absolute path %q: %s", dir, w.Body.String())
	}
	return env
}

// #2511: /effective answers the two-spellings shape as GET /tenants/{id} does —
// 409 with the CONFLICT code — instead of a 500 whose body carried both files'
// absolute server paths. The body may name the files only relative to conf.d.
// PUT rides along: it maps the same sentinel and must not leak the path either.
func TestTenantFileSpelling_AmbiguousConflictNamesNoServerPath(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		spellingTenant + ".yaml": spellingBody("FROM-YAML"),
		spellingTenant + ".yml":  spellingBody("FROM-YML"),
	})
	d := &Deps{ConfigDir: dir, Writer: newTestWriter(dir)}
	base := "/api/v1/tenants/" + spellingTenant

	cases := []struct {
		name string
		h    http.HandlerFunc
		req  *http.Request
	}{
		{"effective", GetTenantEffective(d),
			newRequestWithChiParam("GET", base+"/effective", "id", spellingTenant, nil)},
		{"get", GetTenant(d),
			newRequestWithChiParam("GET", base, "id", spellingTenant, nil)},
		{"put", PutTenant(d),
			newRequestWithChiParam("PUT", base, "id", spellingTenant, bytes.NewBufferString(spellingBody("NEW")))},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w := httptest.NewRecorder()
			tc.h(w, tc.req)
			env := assertConflictWithoutDir(t, w, dir)
			msg, _ := env["error"].(string)
			for _, name := range []string{spellingTenant + ".yaml", spellingTenant + ".yml"} {
				if !strings.Contains(msg, name) {
					t.Errorf("error %q does not name %q", msg, name)
				}
			}
		})
	}
}

// #2511: a duplicate the top-level resolver cannot see — the same id declared
// again in a subdirectory — reaches ResolveEffective's walker as a typed
// *DuplicateTenantError. It is a 409 too, and its body names no file at all:
// the error's text carries both absolute paths, which only the log may see.
func TestGetTenantEffective_NestedDuplicateIsConflictWithoutPaths(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		spellingTenant + ".yaml": spellingBody("FROM-ROOT"),
	})
	writeFile(t, filepath.Join(dir, "team", spellingTenant+".yaml"), spellingBody("FROM-TEAM"))

	w := httptest.NewRecorder()
	GetTenantEffective(&Deps{ConfigDir: dir})(w, newRequestWithChiParam("GET",
		"/api/v1/tenants/"+spellingTenant+"/effective", "id", spellingTenant, nil))

	env := assertConflictWithoutDir(t, w, dir)
	if msg, _ := env["error"].(string); msg != msgTenantDeclaredElsewhere {
		t.Errorf("error = %q, want the fixed text %q", msg, msgTenantDeclaredElsewhere)
	}
}
