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
	// API-shaped (#3064: ti's alone), but a value the typed decode refuses:
	// invalid_config, and still shown — the file holds ti's content only.
	confErrOwnInvalid = "tenants:\n  ti:\n    mysql_connections: !!int abc\n"
)

func confErrDir(t *testing.T) string {
	t.Helper()
	return setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 50\n",
		"tb.yaml":        confErrNonUTF8,
		"tc.yaml":        confErrSyntax,
		"tv.yaml":        confErrValid,
		"ti.yaml":        confErrOwnInvalid,
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
	want := map[string]string{"tb": "invalid_config", "tc": "malformed_yaml", "tv": "", "ti": "invalid_config"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("config_error by id = %v, want %v", got, want)
	}
}

func TestGetTenant_ConfigError(t *testing.T) {
	t.Parallel()
	dir := confErrDir(t)
	// gitReason: #3064 — tb (a `!!binary` key) and tc (not YAML) are not
	// API-shaped, so git-managed: config_error, but no raw_yaml or hash.
	cases := []struct {
		id, raw, wantErr, gitReason string
	}{
		{"tb", confErrNonUTF8, "invalid_config", "yaml_indirection"},
		{"tc", confErrSyntax, "malformed_yaml", "not_tenant_document"},
		{"tv", confErrValid, "", ""},
		{"ti", confErrOwnInvalid, "invalid_config", ""},
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
			if c.gitReason != "" {
				if raw["managed_by"] != "git" || raw["git_managed_reason"] != c.gitReason {
					t.Errorf("managed_by = %v (%v), want git (%s)", raw["managed_by"], raw["git_managed_reason"], c.gitReason)
				}
				for _, k := range []string{"raw_yaml", "source_hash", "resolved_thresholds", "custom_alerts", "validation_warnings", "validation_notices"} {
					if v, ok := raw[k]; ok {
						t.Errorf("git-managed: %s present (%#v), want absent", k, v)
					}
				}
				return
			}
			if raw["managed_by"] != "api" {
				t.Errorf("managed_by = %v, want api", raw["managed_by"])
			}
			// The editor still gets the file and its hash ...
			if d.RawYAML != c.raw {
				t.Errorf("raw_yaml = %q, want %q", d.RawYAML, c.raw)
			}
			if want := cfg.ComputeSourceHash([]byte(c.raw)); d.SourceHash != want {
				t.Errorf("source_hash = %q, want %q", d.SourceHash, want)
			}
			// ... but nothing derived from a file the exporter does not serve.
			// ABSENT, not [] (#2373 review F1): an empty list reads as an
			// authoritative "this tenant has none", and a client editing from
			// it would write that back over the file's real content. Not null
			// either: the spec types both as arrays (Swagger 2.0 has no
			// nullable) and does not require them, so absent stays schema-valid.
			for _, k := range []string{"resolved_thresholds", "custom_alerts", "validation_warnings", "validation_notices"} {
				if v, ok := raw[k]; ok {
					t.Errorf("%s present (%#v), want absent", k, v)
				}
			}
		})
	}
}
