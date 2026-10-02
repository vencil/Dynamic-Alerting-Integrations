package handler

// #2341 R8: a write to a tenant id the route generator renders nothing for
// (routingpolicy.IsValidTenantID) is refused with 400 before anything is
// read or written; reads keep ValidateTenantID alone.

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

func TestValidateWritableTenantID(t *testing.T) {
	t.Parallel()
	for id, wantErr := range map[string]bool{
		"db-a": false, "UPPER": false, "tenant_123": false, "-x": false,
		"": true, "bad tenant": true, "Bad_Tenant!": true, "a.b": true, "tenänt": true,
		"_rbac": true, // ValidateTenantID's own refusal still applies
	} {
		if err := ValidateWritableTenantID(id); (err != nil) != wantErr {
			t.Errorf("ValidateWritableTenantID(%q) error = %v, wantErr %v", id, err, wantErr)
		}
	}
	// A read keeps the old rule: an existing tenant with such an id can be looked at.
	if err := ValidateTenantID("bad tenant"); err != nil {
		t.Errorf("ValidateTenantID(%q) = %v, want nil (reads are not narrowed)", "bad tenant", err)
	}
}

func TestPutTenant_InvalidTenantID(t *testing.T) {
	for id, want := range map[string]int{
		"bad tenant":  http.StatusBadRequest,
		"Bad_Tenant!": http.StatusBadRequest,
		"a.b":         http.StatusBadRequest,
		"UPPER":       http.StatusOK,
	} {
		t.Run(id, func(t *testing.T) {
			dir := seedGitTree(t, map[string]string{"_defaults.yaml": "defaults:\n  cpu_usage_percent: 80\n"})
			rb := adminRBAC(t)
			d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: rb, WriteMode: WriteModeDirect}
			body := "tenants:\n  \"" + id + "\":\n    cpu_usage_percent: '85'\n"
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/x", "id", id, bytes.NewBufferString(body))
			req.Header.Set("X-Forwarded-Email", "alice@example.com")
			req.Header.Set("X-Forwarded-Groups", "admins")
			w := httptest.NewRecorder()
			wrapWithRBACMiddleware(PutTenant(d), rb, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
			if w.Code != want {
				t.Fatalf("status = %d, want %d; body: %s", w.Code, want, w.Body.String())
			}
			_, err := os.Stat(filepath.Join(dir, id+".yaml"))
			if want != http.StatusOK && err == nil {
				t.Errorf("refused PUT wrote %s.yaml", id)
			}
		})
	}
}

func TestBatchTenants_RefusesUnroutableTenantID(t *testing.T) {
	configDir := seedGitTree(t, routingPolicyTree())
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect}
	resp := runBatch(t, configDir, d, `[{"tenant_id":"bad tenant","patch":{"cpu_usage_percent":"90"}},
		{"tenant_id":"t-ok","patch":{"cpu_usage_percent":"90"}}]`)
	if got := statuses(resp.Results); got != "error,ok" {
		t.Fatalf("statuses = %s, want error,ok: %+v", got, resp.Results)
	}
	if _, err := os.Stat(filepath.Join(configDir, "bad tenant.yaml")); err == nil {
		t.Error("refused op wrote bad tenant.yaml")
	}
}
