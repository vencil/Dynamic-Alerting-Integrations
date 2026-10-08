package handler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
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

// Both spellings inside one SUBDIRECTORY are a duplicate too, but not the
// top-level shape: the body is the fixed text, so the subdirectory's name does
// not reach the caller.
func TestGetTenantEffective_SubdirSpellingPairIsFixedText(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "team", spellingTenant+".yaml"), spellingBody("FROM-YAML"))
	writeFile(t, filepath.Join(dir, "team", spellingTenant+".yml"), spellingBody("FROM-YML"))

	w := httptest.NewRecorder()
	GetTenantEffective(&Deps{ConfigDir: dir})(w, newRequestWithChiParam("GET",
		"/api/v1/tenants/"+spellingTenant+"/effective", "id", spellingTenant, nil))

	env := assertConflictWithoutDir(t, w, dir)
	if msg, _ := env["error"].(string); msg != msgTenantDeclaredElsewhere {
		t.Errorf("error = %q, want the fixed text %q", msg, msgTenantDeclaredElsewhere)
	}
}

// A top-level SHARED file whose `tenants:` also declares the id is a duplicate
// at the conf.d root, but the shared file's name is not derivable from the id:
// fixed text, not base names.
func TestGetTenantEffective_TopLevelSharedFileIsFixedText(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{
		spellingTenant + ".yaml": spellingBody("FROM-OWN"),
		"shared.yaml":            spellingBody("FROM-SHARED"),
	})

	w := httptest.NewRecorder()
	GetTenantEffective(&Deps{ConfigDir: dir})(w, newRequestWithChiParam("GET",
		"/api/v1/tenants/"+spellingTenant+"/effective", "id", spellingTenant, nil))

	env := assertConflictWithoutDir(t, w, dir)
	if msg, _ := env["error"].(string); msg != msgTenantDeclaredElsewhere {
		t.Errorf("error = %q, want the fixed text %q", msg, msgTenantDeclaredElsewhere)
	}
}

// The walker reports paths under the symlink-RESOLVED root; a conf.d reached
// through a symlink must still be recognised as top-level, or the two-spellings
// message would silently degrade to the fixed text.
func TestGetTenantEffective_AmbiguousSpellingThroughSymlinkedRoot(t *testing.T) {
	t.Parallel()
	real := setupConfigDir(t, map[string]string{
		spellingTenant + ".yaml": spellingBody("FROM-YAML"),
		spellingTenant + ".yml":  spellingBody("FROM-YML"),
	})
	link := filepath.Join(t.TempDir(), "confd-link")
	if err := os.Symlink(real, link); err != nil {
		t.Skipf("symlink unsupported: %v", err)
	}

	w := httptest.NewRecorder()
	GetTenantEffective(&Deps{ConfigDir: link})(w, newRequestWithChiParam("GET",
		"/api/v1/tenants/"+spellingTenant+"/effective", "id", spellingTenant, nil))

	env := assertConflictWithoutDir(t, w, real)
	msg, _ := env["error"].(string)
	if strings.Contains(msg, link) || !strings.Contains(msg, spellingTenant+".yml") {
		t.Errorf("error = %q, want the two base names and no path", msg)
	}
}

// #2511 review: the conflict is the walker's verdict, not a filename match. A
// sibling `<id>.yml` that declares nothing for the tenant — or cannot be read
// as a tenant file at all — is not a second declaration: the exporter serves
// the tenant from `<id>.yaml`, so /effective must stay 200. A name-only check
// (confd.ResolveTenantFile) answered 409 for every one of these.
func TestGetTenantEffective_NonDeclaringSiblingSpellingStays200(t *testing.T) {
	t.Parallel()
	shapes := []struct {
		name  string
		build func(t *testing.T, sibling string)
	}{
		{"empty tenants map", func(t *testing.T, p string) { writeFile(t, p, "tenants: {}\n") }},
		{"malformed yaml", func(t *testing.T, p string) { writeFile(t, p, "tenants: [this is not: valid yaml\n") }},
		{"declares another tenant only", func(t *testing.T, p string) {
			writeFile(t, p, "tenants:\n  "+spellingTenant+"-other:\n    mysql_connections: \"70\"\n")
		}},
		{"symlink to a directory", func(t *testing.T, p string) {
			target := filepath.Join(filepath.Dir(p), "_target_dir")
			if err := os.Mkdir(target, 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(target, p); err != nil {
				t.Skipf("symlink unsupported: %v", err)
			}
		}},
		{"dangling symlink", func(t *testing.T, p string) {
			if err := os.Symlink(filepath.Join(filepath.Dir(p), "missing.yaml"), p); err != nil {
				t.Skipf("symlink unsupported: %v", err)
			}
		}},
	}
	for _, sh := range shapes {
		t.Run(sh.name, func(t *testing.T) {
			t.Parallel()
			dir := setupConfigDir(t, map[string]string{
				spellingTenant + ".yaml": spellingBody("FROM-YAML"),
			})
			sh.build(t, filepath.Join(dir, spellingTenant+".yml"))

			w := httptest.NewRecorder()
			GetTenantEffective(&Deps{ConfigDir: dir})(w, newRequestWithChiParam("GET",
				"/api/v1/tenants/"+spellingTenant+"/effective", "id", spellingTenant, nil))
			if w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
			}
		})
	}
}

// #2511 review: an internal failure answers a fixed message; the error text
// (here the walker's stat of the missing root) carries the server path.
func TestGetTenantEffective_InternalErrorNamesNoServerPath(t *testing.T) {
	t.Parallel()
	dir := filepath.Join(t.TempDir(), "absent")

	w := httptest.NewRecorder()
	GetTenantEffective(&Deps{ConfigDir: dir})(w, newRequestWithChiParam("GET",
		"/api/v1/tenants/"+spellingTenant+"/effective", "id", spellingTenant, nil))

	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500; body=%s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), dir) {
		t.Errorf("body leaks the conf.d absolute path %q: %s", dir, w.Body.String())
	}
}

// A *cfg.DecodeError keeps its own text in the body: it is the decoder's
// message (`parse defaults[i]: …`), which names no path. Measured here so the
// pass-through cannot start leaking if that text ever changes.
//
// The fixture is a root `_defaults.yaml` the exporter reads (its typed decode
// skips the unknown top-level `x`) while the defaults-chain merge cannot
// decode it (`!!int abc`). A chain file with a plain syntax error is no
// longer one (#2296): the exporter drops it, and so does /effective — 200,
// named in chain_parse_failed (TestGetTenantEffective_ChainSyntaxErrorIs200).
func TestGetTenantEffective_DecodeErrorTextNamesNoServerPath(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  cpu: 70\nx: !!int abc\n")
	writeFile(t, filepath.Join(dir, "team", spellingTenant+".yaml"), spellingBody("FROM-TEAM"))

	w := httptest.NewRecorder()
	GetTenantEffective(&Deps{ConfigDir: dir})(w, newRequestWithChiParam("GET",
		"/api/v1/tenants/"+spellingTenant+"/effective", "id", spellingTenant, nil))

	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500; body=%s", w.Code, w.Body.String())
	}
	body := w.Body.String()
	if strings.Contains(body, dir) {
		t.Errorf("body leaks the conf.d absolute path %q: %s", dir, body)
	}
	if !strings.Contains(body, "parse defaults") {
		t.Errorf("body lost the decode error's own text: %s", body)
	}
}

// #2296: a chain `_defaults.yaml` with a syntax error is dropped by the
// exporter, which serves the tenant from the rest of the chain; /effective
// answers 200 the same way, naming the file in chain_parse_failed and no
// server path anywhere in the body. It used to be a 500.
func TestGetTenantEffective_ChainSyntaxErrorIs200(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "team", "_defaults.yaml"), "defaults: [this is not: valid yaml\n")
	writeFile(t, filepath.Join(dir, "team", spellingTenant+".yaml"), spellingBody("FROM-TEAM"))

	w := httptest.NewRecorder()
	GetTenantEffective(&Deps{ConfigDir: dir})(w, newRequestWithChiParam("GET",
		"/api/v1/tenants/"+spellingTenant+"/effective", "id", spellingTenant, nil))

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	body := w.Body.String()
	if strings.Contains(body, dir) {
		t.Errorf("body leaks the conf.d absolute path %q: %s", dir, body)
	}
	var got struct {
		TenantID         string   `json:"tenant_id"`
		ChainParseFailed []string `json:"chain_parse_failed"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("body is not JSON: %v: %s", err, body)
	}
	if got.TenantID != spellingTenant || len(got.ChainParseFailed) != 1 || got.ChainParseFailed[0] != "team/_defaults.yaml" {
		t.Errorf("tenant_id %q chain_parse_failed %v, want %q [team/_defaults.yaml]", got.TenantID, got.ChainParseFailed, spellingTenant)
	}
}
