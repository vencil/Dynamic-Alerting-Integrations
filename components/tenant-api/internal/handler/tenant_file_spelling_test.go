package handler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
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
