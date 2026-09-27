package handler

// #2208: GET /api/v1/tenants/{id} shows the root platform files' per-tenant
// `tenants:` layer (what /metrics serves), and keeps its two validation
// channels honest about who wrote what: validation_warnings stays "what a
// write of this exact file would be rejected on" — the tenant's own keys —
// and a problem in a platform file's entry for the tenant is a notice that
// names that file.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func getTenantDetail(t *testing.T, d *Deps, id string) TenantDetail {
	t.Helper()
	req := newRequestWithChiParam("GET", "/api/v1/tenants/"+id, "id", id, nil)
	w := httptest.NewRecorder()
	GetTenant(d)(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("GET status = %d, body %s", w.Code, w.Body.String())
	}
	var detail TenantDetail
	if err := json.Unmarshal(w.Body.Bytes(), &detail); err != nil {
		t.Fatal(err)
	}
	return detail
}

func resolvedValue(detail TenantDetail, component, metric, severity string) (float64, bool) {
	for _, r := range detail.Resolved {
		if r.Component == component && r.Metric == metric && r.Severity == severity && len(r.CustomLabels) == 0 {
			return r.Value, true
		}
	}
	return 0, false
}

func TestGetTenantShowsThePlatformLayer(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_memory: 70\n",
		"_platform.yaml": "tenants:\n  tx:\n    redis_memory: \"66\"\n    mysql_connections: \"11\"\n  ty:\n    redis_memory: \"1\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    mysql_connections: \"90\"\n",
	})
	detail := getTenantDetail(t, &Deps{ConfigDir: configDir}, "tx")
	if v, ok := resolvedValue(detail, "redis", "memory", "warning"); !ok || v != 66 {
		t.Errorf("redis_memory = %v (present %v), want the platform file's 66", v, ok)
	}
	if v, _ := resolvedValue(detail, "mysql", "connections", "warning"); v != 90 {
		t.Errorf("mysql_connections = %v, want the tenant file's 90 (tenant wins key by key)", v)
	}
	for _, r := range detail.Resolved {
		if r.Tenant != "tx" {
			t.Errorf("resolved row for tenant %q", r.Tenant)
		}
	}
	if len(detail.Warnings) != 0 || len(detail.Notices) != 0 {
		t.Errorf("warnings %q notices %q, want none", detail.Warnings, detail.Notices)
	}
}

func TestGetTenantPlatformProblemIsANoticeNotAWarning(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"_platform.yaml": "tenants:\n  tx:\n    mysql_conections_typo: \"5\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    mysql_connections: \"90\"\n",
	})
	detail := getTenantDetail(t, &Deps{ConfigDir: configDir}, "tx")
	if len(detail.Warnings) != 0 {
		t.Errorf("validation_warnings = %q: a platform-file problem is not what a write of tx.yaml is rejected on", detail.Warnings)
	}
	found := false
	for _, n := range detail.Notices {
		if strings.Contains(n, "platform file _platform.yaml") && strings.Contains(n, "mysql_conections_typo") {
			found = true
		}
		if strings.Contains(n, configDir) {
			t.Errorf("notice leaks the server path: %q", n)
		}
	}
	if !found {
		t.Errorf("validation_notices = %q, want one naming _platform.yaml and the key", detail.Notices)
	}

	// Must-fire control: the same key in the tenant's own file is a warning.
	own := setupConfigDir(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"tx.yaml":        "tenants:\n  tx:\n    mysql_conections_typo: \"5\"\n",
	})
	if d := getTenantDetail(t, &Deps{ConfigDir: own}, "tx"); len(d.Warnings) == 0 {
		t.Errorf("control: the tenant's own unknown key produced no validation_warnings (notices %q)", d.Notices)
	}
}
