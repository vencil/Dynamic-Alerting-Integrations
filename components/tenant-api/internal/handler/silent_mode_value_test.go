package handler

// #3227: `_silent_mode`'s value is judged by the write gate with the
// exporter's reader — the same on PUT, batch and dry-run — not by a copy of
// the reader's word list in this package. A batch op with a value the reader
// ignores fails as that op (422, op code BAD_REQUEST; async: the task
// fails); a value the file already holds is a notice (GET shows it there,
// not among the validation warnings).

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
)

const smPurple = `_silent_mode "purple" is not a value the exporter reads`

// smPurpleJSON is smPurple as it appears in a JSON response body.
const smPurpleJSON = `_silent_mode \"purple\" is not a value the exporter reads`

func newSilentModeFixture(t *testing.T, tA string) *aonFixture {
	t.Helper()
	f := &aonFixture{dir: seedGitTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"t-a.yaml":       tA,
		"t-b.yaml":       "tenants:\n  t-b:\n    mysql_connections: \"75\"\n",
	})}
	f.tasks = async.NewManager(1)
	t.Cleanup(func() { _ = f.tasks.Close() })
	f.deps = &Deps{State: mustDirState(f.dir), Writer: newTestWriter(f.dir), ConfigDir: f.dir, RBAC: adminRBAC(t),
		Groups: groups.NewDirManager(f.dir), WriteMode: WriteModeDirect, Tasks: f.tasks}
	return f
}

func TestBatchTenants_SilentModeValue_RefusedAsTheOp(t *testing.T) {
	t.Parallel()
	body := `{"operations":[{"tenant_id":"t-b","patch":{"_silent_mode":"warning"}},{"tenant_id":"t-a","patch":{"_silent_mode":"purple"}}]}`

	t.Run("sync", func(t *testing.T) {
		t.Parallel()
		f := newSilentModeFixture(t, "tenants:\n  t-a:\n    mysql_connections: \"70\"\n")
		code, results, _ := f.run(t, "", body, false)
		if code != http.StatusUnprocessableEntity {
			t.Fatalf("status = %d, want 422: %s", code, f.lastBody)
		}
		if statuses(results) != "not_applied,error" || results[1].Code != "BAD_REQUEST" ||
			!strings.Contains(results[1].Message, smPurple) {
			t.Errorf("results = %+v", results)
		}
	})
	t.Run("async", func(t *testing.T) {
		t.Parallel()
		f := newSilentModeFixture(t, "tenants:\n  t-a:\n    mysql_connections: \"70\"\n")
		_, results, task := f.run(t, "", body, true)
		if task == nil || task.Status != async.TaskFailed {
			t.Fatalf("task = %+v, want failed", task)
		}
		if statuses(results) != "not_applied,error" || results[1].Code != "BAD_REQUEST" {
			t.Errorf("results = %+v", results)
		}
	})
}

// Every value the reader takes is accepted: the disabled words and a value
// with surrounding space, which the old handler copy refused or never saw.
func TestBatchTenants_SilentModeValue_ReadableValuesPass(t *testing.T) {
	t.Parallel()
	for _, v := range []string{"off", "disabled", "false", " warning ", "Critical"} {
		t.Run(v, func(t *testing.T) {
			t.Parallel()
			f := newSilentModeFixture(t, "tenants:\n  t-a:\n    mysql_connections: \"70\"\n")
			code, results, _ := f.run(t, "", `{"operations":[{"tenant_id":"t-a","patch":{"_silent_mode":"`+v+`"}}]}`, false)
			if code != http.StatusOK || statuses(results) != "ok" {
				t.Errorf("status %d results %+v: %s", code, results, f.lastBody)
			}
		})
	}
}

// A batch op that leaves an existing bad value alone passes, and says so.
func TestBatchTenants_SilentModeValue_ExistingIsANotice(t *testing.T) {
	t.Parallel()
	f := newSilentModeFixture(t, "tenants:\n  t-a:\n    _silent_mode: purple\n")
	code, _, _ := f.run(t, "", `{"operations":[{"tenant_id":"t-a","patch":{"mysql_connections":"71"}}]}`, false)
	if code != http.StatusOK || !strings.Contains(f.lastBody, smPurpleJSON) {
		t.Fatalf("status %d: %s", code, f.lastBody)
	}
	var resp BatchResponse
	if err := json.Unmarshal([]byte(f.lastBody), &resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Results) != 1 || len(resp.Results[0].Warnings) == 0 ||
		!strings.Contains(strings.Join(resp.Results[0].Warnings, "\n"), "NOTICE: tenant=t-a: "+smPurple) {
		t.Errorf("results = %+v, want the notice on the op", resp.Results)
	}
}

// Keys a batch patch cannot set are still a 400 for the whole request.
func TestBatchTenants_PatchKeysRefusedUpFront(t *testing.T) {
	t.Parallel()
	for _, k := range []string{"_metadata", "_custom_alerts", "_namespaces", "_timeout_ms"} {
		t.Run(k, func(t *testing.T) {
			t.Parallel()
			f := newSilentModeFixture(t, "tenants:\n  t-a:\n    mysql_connections: \"70\"\n")
			code, _, _ := f.run(t, "", `{"operations":[{"tenant_id":"t-a","patch":{"`+k+`":"x"}}]}`, false)
			if code != http.StatusBadRequest || !strings.Contains(f.lastBody, `"code":"INVALID_BODY"`) {
				t.Errorf("status %d: %s", code, f.lastBody)
			}
		})
	}
}

// The unset allow-list is the registry's, and the same keys as before.
func TestValidateBatchEdit_UnsetAllowList(t *testing.T) {
	t.Parallel()
	for _, k := range []string{"_routing", "_routing_receiver_type"} {
		if v := validateBatchEdit(nil, []string{k}, "operations[0]"); len(v) != 0 {
			t.Errorf("unset %s: %+v", k, v)
		}
	}
	v := validateBatchEdit(nil, []string{"_silent_mode"}, "operations[0]")
	if len(v) != 1 || v[0].Reason != `unset can only remove "_routing", "_routing_receiver_type"; got "_silent_mode"` {
		t.Errorf("unset _silent_mode: %+v", v)
	}
}

func putTenantIn(t *testing.T, f *aonFixture, id, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+id, "id", id, bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(f.deps), f.deps.RBAC, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
	return w
}

// PUT: a new bad value is a 400; leaving an existing one alone is a 200.
func TestPutTenant_SilentModeValue(t *testing.T) {
	t.Parallel()
	f := newSilentModeFixture(t, "tenants:\n  t-a:\n    _silent_mode: purple\n")
	if w := putTenantIn(t, f, "t-b", "tenants:\n  t-b:\n    _silent_mode: purple\n"); w.Code != http.StatusBadRequest ||
		!strings.Contains(w.Body.String(), smPurpleJSON) {
		t.Errorf("new bad value: %d %s", w.Code, w.Body.String())
	}
	if w := putTenantIn(t, f, "t-a", "tenants:\n  t-a:\n    _silent_mode: purple\n    mysql_connections: \"71\"\n"); w.Code != http.StatusOK ||
		!strings.Contains(w.Body.String(), "NOTICE: tenant=t-a: "+smPurpleJSON) {
		t.Errorf("existing value left alone: %d %s", w.Code, w.Body.String())
	}
}

// GET: a bad value on disk is a validation notice, not a validation warning
// (which says what blocks a write).
func TestGetTenant_SilentModeValueIsANotice(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml":        "tenants:\n  tx:\n    _silent_mode: purple\n    _silent_mod: x\n",
	})
	detail := getTenantDetail(t, &Deps{ConfigDir: configDir}, "tx")
	if strings.Contains(strings.Join(detail.Warnings, "\n"), smPurple) ||
		!strings.Contains(strings.Join(detail.Notices, "\n"), smPurple) {
		t.Errorf("warnings %q notices %q", detail.Warnings, detail.Notices)
	}
	// Control: a key error stays a warning.
	if !strings.Contains(strings.Join(detail.Warnings, "\n"), `"_silent_mod"`) {
		t.Errorf("warnings %q, want the unknown key", detail.Warnings)
	}
}

// The same on a git-managed tenant (#3064: a flat file declaring two
// tenants), whose GET answers the effective view.
func TestGetTenant_GitManaged_SilentModeValueIsANotice(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml":        "tenants:\n  tx:\n    _silent_mode: purple\n  ty:\n    mysql_connections: \"70\"\n",
	})
	detail := getTenantDetail(t, &Deps{ConfigDir: configDir}, "tx")
	if detail.ManagedBy != "git" {
		t.Fatalf("managed_by = %q, want git (the fixture no longer exercises that path)", detail.ManagedBy)
	}
	if strings.Contains(strings.Join(detail.Warnings, "\n"), smPurple) ||
		!strings.Contains(strings.Join(detail.Notices, "\n"), smPurple) {
		t.Errorf("warnings %q notices %q", detail.Warnings, detail.Notices)
	}
}

// PR mode's dry-run is the body-only pre-flight (#1718): a value is judged on
// the write, against the fresh base, not here.
func TestValidateTenant_PRModeDoesNotJudgeValues(t *testing.T) {
	t.Parallel()
	f := newSilentModeFixture(t, "tenants:\n  t-a:\n    mysql_connections: \"70\"\n")
	f.deps.WriteMode = WriteModePR
	f.deps.PRClient = &mockPlatformClient{}
	f.deps.PRTracker = &mockPlatformTracker{}
	req := newRequestWithChiParam("POST", "/api/v1/tenants/t-a/validate", "id", "t-a",
		bytes.NewBufferString("tenants:\n  t-a:\n    _silent_mode: purple\n"))
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(ValidateTenant(f.deps), f.deps.RBAC, rbac.PermRead, TenantIDFromPath).ServeHTTP(w, req)
	var resp ValidateResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil || !resp.Valid {
		t.Errorf("status %d: %s", w.Code, w.Body.String())
	}
}

// #3269: a reserved key written with a schedule — refused on a new write,
// a notice when the file already holds it (GET, PUT and a batch op that
// leave it alone), a refusal when the write changes the schedule.
const (
	smSched       = "_silent_mode writes `overrides:`"
	smSchedValue  = "{default: disable, overrides: [{window: \"03:00-04:00\", value: all}]}"
	smSchedTenant = "tenants:\n  t-a:\n    _silent_mode: " + smSchedValue + "\n"
)

func TestScheduledReserved_WriteAndRead(t *testing.T) {
	t.Parallel()
	t.Run("PUT new is refused", func(t *testing.T) {
		t.Parallel()
		f := newSilentModeFixture(t, smSchedTenant)
		if w := putTenantIn(t, f, "t-b", "tenants:\n  t-b:\n    _silent_mode: "+smSchedValue+"\n"); w.Code != http.StatusBadRequest ||
			!strings.Contains(w.Body.String(), smSched) {
			t.Errorf("%d %s", w.Code, w.Body.String())
		}
	})
	t.Run("PUT leaving it alone is a notice", func(t *testing.T) {
		t.Parallel()
		f := newSilentModeFixture(t, smSchedTenant)
		if w := putTenantIn(t, f, "t-a", smSchedTenant+"    mysql_connections: \"71\"\n"); w.Code != http.StatusOK ||
			!strings.Contains(w.Body.String(), "NOTICE: tenant=t-a: "+smSched) {
			t.Errorf("%d %s", w.Code, w.Body.String())
		}
	})
	t.Run("PUT moving the window is refused", func(t *testing.T) {
		t.Parallel()
		f := newSilentModeFixture(t, smSchedTenant)
		moved := strings.Replace(smSchedTenant, "03:00-04:00", "05:00-06:00", 1)
		if w := putTenantIn(t, f, "t-a", moved); w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), smSched) {
			t.Errorf("%d %s", w.Code, w.Body.String())
		}
	})
	t.Run("batch op on another key passes with the notice", func(t *testing.T) {
		t.Parallel()
		f := newSilentModeFixture(t, smSchedTenant)
		code, results, _ := f.run(t, "", `{"operations":[{"tenant_id":"t-a","patch":{"mysql_connections":"71"}}]}`, false)
		if code != http.StatusOK || statuses(results) != "ok" ||
			!strings.Contains(strings.Join(results[0].Warnings, "\n"), "NOTICE: tenant=t-a: "+smSched) {
			t.Errorf("status %d results %+v: %s", code, results, f.lastBody)
		}
	})
	t.Run("GET is a notice", func(t *testing.T) {
		t.Parallel()
		configDir := setupConfigDir(t, map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"t-a.yaml":       smSchedTenant,
		})
		detail := getTenantDetail(t, &Deps{ConfigDir: configDir}, "t-a")
		if strings.Contains(strings.Join(detail.Warnings, "\n"), smSched) ||
			!strings.Contains(strings.Join(detail.Notices, "\n"), smSched) {
			t.Errorf("warnings %q notices %q", detail.Warnings, detail.Notices)
		}
	})
}
