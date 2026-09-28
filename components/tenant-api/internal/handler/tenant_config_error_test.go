package handler

// #2373: a tenant file threshold-exporter rejects WHOLE — here one that
// declares a tenant id that is not valid UTF-8 (#2266, cfg.ParseTenantFile) —
// must not be served by GET /tenants/{id} and GET /tenants as a healthy
// tenant with thresholds, since /metrics, /effective and da-guard all drop it.
// GET answers 200 with the file's content (so an editor can still open and
// fix it) plus config_error — the same reason LIST reports — and nothing
// derived from the file. A file that is not YAML at all gets the same
// 200 + config_error instead of a 500.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

const (
	// Declares tenant "tb" plus a tenant id spelled as the bytes 0x74 0xff.
	confErrNonUTF8 = "tenants:\n  !!binary dP8=:\n    mysql_connections: \"10\"\n  tb:\n    mysql_connections: \"60\"\n"
	// Not YAML at all (unclosed flow sequence).
	confErrSyntax = "tenants:\n  tc: [unclosed\n"
	// Control: a usable tenant file.
	confErrValid = "tenants:\n  tv:\n    mysql_connections: \"70\"\n"
)

func confErrDir(t *testing.T) string {
	t.Helper()
	return setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 50\n",
		"tb.yaml":        confErrNonUTF8,
		"tc.yaml":        confErrSyntax,
		"tv.yaml":        confErrValid,
	})
}

func TestListTenants_ConfigErrorMatchesExporterVerdict(t *testing.T) {
	t.Parallel()
	dir := confErrDir(t)
	h := ListTenants(&Deps{ConfigDir: dir, RBAC: newRBACManager(t, "")})
	w := httptest.NewRecorder()
	h(w, httptest.NewRequest("GET", "/api/v1/tenants", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, body: %s", w.Code, w.Body.String())
	}
	var rows []TenantSummary
	if err := json.Unmarshal(w.Body.Bytes(), &rows); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	got := map[string]string{}
	for _, r := range rows {
		got[r.ID] = r.ConfigError
	}
	want := map[string]string{"tb": "invalid_config", "tc": "malformed_yaml", "tv": ""}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("config_error by id = %v, want %v", got, want)
	}
}

func TestGetTenant_ConfigError(t *testing.T) {
	t.Parallel()
	dir := confErrDir(t)
	cases := []struct {
		id, raw, wantErr string
	}{
		{"tb", confErrNonUTF8, "invalid_config"},
		{"tc", confErrSyntax, "malformed_yaml"},
		{"tv", confErrValid, ""},
	}
	for _, c := range cases {
		t.Run(c.id, func(t *testing.T) {
			t.Parallel()
			h := GetTenant(&Deps{ConfigDir: dir})
			w := httptest.NewRecorder()
			h(w, newRequestWithChiParam("GET", "/api/v1/tenants/"+c.id, "id", c.id, nil))
			if w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
			}
			var raw map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &raw); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			var d TenantDetail
			if err := json.Unmarshal(w.Body.Bytes(), &d); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			gotErr, present := raw["config_error"]
			if c.wantErr == "" {
				if present {
					t.Errorf("healthy tenant carries config_error = %v", gotErr)
				}
				if len(d.Resolved) == 0 {
					t.Errorf("healthy tenant has no resolved_thresholds")
				}
				return
			}
			if gotErr != c.wantErr {
				t.Errorf("config_error = %v, want %q", gotErr, c.wantErr)
			}
			// The editor still gets the file and its hash ...
			if d.RawYAML != c.raw {
				t.Errorf("raw_yaml = %q, want %q", d.RawYAML, c.raw)
			}
			if want := cfg.ComputeSourceHash([]byte(c.raw)); d.SourceHash != want {
				t.Errorf("source_hash = %q, want %q", d.SourceHash, want)
			}
			// ... but nothing derived from a file the exporter does not serve.
			if len(d.Resolved) != 0 {
				t.Errorf("resolved_thresholds = %+v, want empty", d.Resolved)
			}
			if len(d.CustomAlerts) != 0 {
				t.Errorf("custom_alerts = %+v, want empty", d.CustomAlerts)
			}
			// Arrays stay arrays (the spec declares them; null would break clients).
			for _, k := range []string{"resolved_thresholds", "custom_alerts"} {
				if _, ok := raw[k].([]any); !ok {
					t.Errorf("%s = %#v, want a JSON array", k, raw[k])
				}
			}
		})
	}
}
