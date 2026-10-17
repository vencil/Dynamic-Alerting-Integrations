package handler

// #3063: a tenant file holding a scalar whose explicit tag its text does not
// satisfy (`mysql_connections: !!int zzleak42`). cfg.ParseTenantFile used to
// accept it (the typed decode never resolved the tag) while the generic
// decode every other reader uses refused it, so the read endpoints split:
// GET /tenants/{id} answered 500 INTERNAL_ERROR (customalerts.Extract),
// /effective 500 CONFIG_DECODE_ERROR, and the list and search called the
// tenant healthy — and a bad value in ANOTHER tenant's block of the same file
// did all of that to an innocent tenant. ParseTenantFile now rejects such a
// file whole, so every read endpoint gives it the verdict of any other file
// the exporter rejects: config_error invalid_config without derived fields,
// /effective 404. A tag the text satisfies changes nothing.
//
// No response may carry the file's value outside raw_yaml (which is the file,
// for an editor to fix).

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const explicitTagMarker = "zzleak42"

var explicitTagCases = []struct {
	name, file string
	// wantErr is the config_error for both tx and (when declared) ty.
	wantErr string
}{
	// ① The tenant's own threshold.
	{"own_threshold_int_tag_on_text", "tenants:\n  tx:\n    mysql_connections: !!int " + explicitTagMarker + "\n", "invalid_config"},
	// ④ Another tenant's block in the same file: tx is innocent, and goes with
	// the file, as with any other whole-file rejection.
	{"sibling_tenant_threshold_int_tag_on_text", "tenants:\n  tx:\n    mysql_connections: \"70\"\n  ty:\n    mysql_connections: !!int " + explicitTagMarker + "\n", "invalid_config"},
	// ③ Inside _custom_alerts (rejected before #3063 too; the reference).
	{"custom_alert_threshold_int_tag_on_text", "tenants:\n  tx:\n    mysql_connections: \"70\"\n    _custom_alerts:\n      - recipe: threshold\n        name: a1\n        metric: m\n        op: \">\"\n        threshold: !!int " + explicitTagMarker + "\n", "invalid_config"},
	// ⑤ Inside _metadata (rejected before #3063 too; the reference).
	{"own_metadata_int_tag_on_text", "tenants:\n  tx:\n    mysql_connections: \"70\"\n    _metadata:\n      owner: !!int " + explicitTagMarker + "\n", "invalid_config"},
	// ② A tag the text satisfies is not a defect.
	{"custom_alert_string_field_int_5", "tenants:\n  tx:\n    mysql_connections: \"70\"\n    _custom_alerts:\n      - recipe: !!int 5\n        name: a1\n", ""},
	{"threshold_str_5", "tenants:\n  tx:\n    mysql_connections: !!str 5\n", ""},
	{"threshold_float_1_5", "tenants:\n  tx:\n    mysql_connections: !!float 1.5\n", ""},
	{"threshold_int_5_sibling_bool_true", "tenants:\n  tx:\n    mysql_connections: !!int 5\n  ty:\n    mysql_connections: \"70\"\n    _silent_mode: !!bool true\n", ""},
}

func TestReadEndpoints_ExplicitTagMismatchIsInvalidConfig(t *testing.T) {
	t.Parallel()
	for _, c := range explicitTagCases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir := setupConfigDir(t, map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 50\n",
				"tx.yaml":        c.file,
			})
			mgr := newRBACManager(t, "")
			noLeak := func(ep string, body []byte) {
				t.Helper()
				var m map[string]any
				if json.Unmarshal(body, &m) == nil {
					delete(m, "raw_yaml")
					body, _ = json.Marshal(m)
				}
				if strings.Contains(string(body), explicitTagMarker) {
					t.Errorf("%s: response carries the file's value outside raw_yaml: %.300s", ep, body)
				}
			}

			// GET /tenants/{id}
			w := httptest.NewRecorder()
			GetTenant(&Deps{ConfigDir: dir, RBAC: mgr})(w, newRequestWithChiParam("GET", "/api/v1/tenants/tx", "id", "tx", nil))
			if w.Code != http.StatusOK {
				t.Fatalf("GET /tenants/tx status = %d, want 200; body: %.300s", w.Code, w.Body.String())
			}
			noLeak("GET /tenants/tx", w.Body.Bytes())
			var detail map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &detail); err != nil {
				t.Fatal(err)
			}
			if got, _ := detail["config_error"].(string); got != c.wantErr {
				t.Errorf("GET /tenants/tx config_error = %q, want %q", got, c.wantErr)
			}
			// #3064: a file that also declares ty is git-managed — no
			// raw_yaml; tx's own file is shown.
			if strings.Contains(c.file, "\n  ty:\n") {
				if raw, ok := detail["raw_yaml"]; ok || detail["managed_by"] != "git" {
					t.Errorf("GET /tenants/tx (shared file): raw_yaml = %v, managed_by = %v; want absent, git", raw, detail["managed_by"])
				}
			} else if raw, _ := detail["raw_yaml"].(string); raw != c.file {
				t.Errorf("GET /tenants/tx raw_yaml = %q, want the file", raw)
			}
			for _, k := range []string{"resolved_thresholds", "custom_alerts", "validation_warnings", "validation_notices"} {
				_, present := detail[k]
				if c.wantErr != "" && present {
					t.Errorf("GET /tenants/tx: derived field %s present on a rejected file", k)
				}
			}
			if c.wantErr == "" {
				if _, ok := detail["resolved_thresholds"]; !ok {
					t.Errorf("GET /tenants/tx: healthy tenant without resolved_thresholds")
				}
			}

			// GET /tenants/{id}/effective
			w = httptest.NewRecorder()
			GetTenantEffective(&Deps{ConfigDir: dir, RBAC: mgr})(w, newRequestWithChiParam("GET", "/api/v1/tenants/tx/effective", "id", "tx", nil))
			wantEff := http.StatusOK
			if c.wantErr != "" {
				wantEff = http.StatusNotFound
			}
			if w.Code != wantEff {
				t.Errorf("GET /tenants/tx/effective status = %d, want %d; body: %.300s", w.Code, wantEff, w.Body.String())
			}
			noLeak("GET /tenants/tx/effective", w.Body.Bytes())

			// GET /tenants
			w = httptest.NewRecorder()
			ListTenants(&Deps{ConfigDir: dir, RBAC: mgr})(w, httptest.NewRequest("GET", "/api/v1/tenants", nil))
			if w.Code != http.StatusOK {
				t.Fatalf("GET /tenants status = %d; body: %.300s", w.Code, w.Body.String())
			}
			noLeak("GET /tenants", w.Body.Bytes())
			var rows []TenantSummary
			if err := json.Unmarshal(w.Body.Bytes(), &rows); err != nil {
				t.Fatal(err)
			}
			listed := 0
			for _, r := range rows {
				if r.ID != "tx" && r.ID != "ty" {
					continue
				}
				listed++
				if r.ConfigError != c.wantErr {
					t.Errorf("GET /tenants row %s config_error = %q, want %q", r.ID, r.ConfigError, c.wantErr)
				}
			}
			if listed == 0 {
				t.Errorf("GET /tenants lists neither tx nor ty")
			}

			// GET /tenants/search
			resp, code, body := runSearch(t, dir, openModeRBAC(t), nil, "")
			if code != http.StatusOK {
				t.Fatalf("search status = %d; body: %.300s", code, body)
			}
			noLeak("GET /tenants/search", body)
			found := 0
			for _, it := range resp.Items {
				if it.ID != "tx" {
					continue
				}
				found++
				if it.ConfigError != c.wantErr {
					t.Errorf("search item tx config_error = %q, want %q", it.ConfigError, c.wantErr)
				}
			}
			if found != 1 {
				t.Errorf("search returned tx %d times, want 1", found)
			}
		})
	}
}
