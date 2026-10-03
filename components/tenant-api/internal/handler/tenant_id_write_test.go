package handler

// #2341 R8 / ADR-035: a write to a tenant id the tenant-id rule refuses
// (routingpolicy.IsValidTenantID: a DNS-1123 label) is refused with 400
// before anything is read or written; reads keep ValidateTenantID alone.

import (
	"bytes"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/threshold-exporter/pkg/tenantid"
)

func TestValidateWritableTenantID(t *testing.T) {
	t.Parallel()
	for id, wantErr := range map[string]bool{
		"db-a": false, "a": false, "010": false, strings.Repeat("a", 63): false,
		// ADR-035: accepted by the #2341 R8 rule, refused by DNS-1123.
		"UPPER": true, "tenant_123": true, "-x": true, "x-": true, strings.Repeat("a", 64): true,
		"": true, "bad tenant": true, "Bad_Tenant!": true, "a.b": true, "tenänt": true,
		"_rbac": true, // ValidateTenantID's own refusal still applies
	} {
		err := ValidateWritableTenantID(id)
		if (err != nil) != wantErr {
			t.Errorf("ValidateWritableTenantID(%q) error = %v, wantErr %v", id, err, wantErr)
		}
		// The message cites the rule's one description, not its own wording.
		if err != nil && id != "_rbac" && id != "" && !strings.Contains(err.Error(), tenantid.Description) {
			t.Errorf("ValidateWritableTenantID(%q) = %q, want it to cite tenantid.Description", id, err)
		}
	}
	// A read keeps the old rule: an existing tenant with such an id can be looked at.
	for _, id := range []string{"bad tenant", "UPPER", "Team_A"} {
		if err := ValidateTenantID(id); err != nil {
			t.Errorf("ValidateTenantID(%q) = %v, want nil (reads are not narrowed)", id, err)
		}
	}
}

// TestWriterTenantIDRefusalMapsTo400 (ADR-035): the writer's own copy of the
// rule (gitops.ErrInvalidTenantID) is a 400 on the PR write paths and passes
// verbatim through the dry-run route, like the reserved-id refusal.
func TestWriterTenantIDRefusalMapsTo400(t *testing.T) {
	t.Parallel()
	err := fmt.Errorf("%w %q: %s", gitops.ErrInvalidTenantID, "Team_A", tenantid.Description)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("PUT", "/api/v1/tenants/Team_A", nil)
	if !writeWriteFlowError(rec, req, err) {
		t.Fatal("writeWriteFlowError did not handle ErrInvalidTenantID")
	}
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400; body %s", rec.Code, rec.Body.String())
	}
	if got := dryRunRefusalMessage(err); got != err.Error() {
		t.Errorf("dryRunRefusalMessage = %q, want the error verbatim %q", got, err.Error())
	}
	if !errors.Is(err, gitops.ErrInvalidTenantID) {
		t.Fatal("test error does not wrap the sentinel")
	}
}

func TestPutTenant_InvalidTenantID(t *testing.T) {
	for id, want := range map[string]int{
		"bad tenant":  http.StatusBadRequest,
		"Bad_Tenant!": http.StatusBadRequest,
		"a.b":         http.StatusBadRequest,
		"UPPER":       http.StatusBadRequest, // ADR-035: 200 under the #2341 R8 rule
		"Team_A":      http.StatusBadRequest,
		"team-a":      http.StatusOK,
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
		{"tenant_id":"Team_A","patch":{"cpu_usage_percent":"90"}},
		{"tenant_id":"t-ok","patch":{"cpu_usage_percent":"90"}}]`)
	if got := statuses(resp.Results); got != "error,error,ok" {
		t.Fatalf("statuses = %s, want error,error,ok: %+v", got, resp.Results)
	}
	for _, f := range []string{"bad tenant.yaml", "Team_A.yaml"} {
		if _, err := os.Stat(filepath.Join(configDir, f)); err == nil {
			t.Errorf("refused op wrote %s", f)
		}
	}
}
