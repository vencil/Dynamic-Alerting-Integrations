//go:build unix

package handler

// #2477: the list row and the per-tenant endpoints name the same
// config_error for the same non-regular entry. Fixtures and helpers are in
// authz_nonregular_unix_test.go.
//
// FIFO is not compared here: the list also derives state through
// threshold-exporter's conf.d load, which reads every file and does not
// return on a FIFO — a separate plane, outside this check.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

func TestListTenants_NonRegularConfigErrorMatchesByID(t *testing.T) {
	t.Parallel()
	for _, kind := range nonRegularKinds {
		if kind.name == "fifo" {
			continue
		}
		t.Run(kind.name, func(t *testing.T) {
			t.Parallel()
			f := newNonRegularFixture(t, kind.make)

			w, _ := f.serve(t, GetTenant(f.d), rbac.PermRead,
				"GET", "/api/v1/tenants/"+nonRegularTenantID, nonRegularTenantID, "")
			var byID map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &byID); err != nil {
				t.Fatalf("by-id body is not JSON: %v: %s", err, w.Body.String())
			}

			list := httptest.NewRecorder()
			withinBound(t, "GET /api/v1/tenants", func() {
				ListTenants(&Deps{ConfigDir: f.dir, RBAC: newRBACManager(t, "")})(
					list, httptest.NewRequest("GET", "/api/v1/tenants", nil))
			})
			if list.Code != http.StatusOK {
				t.Fatalf("list status = %d: %s", list.Code, list.Body.String())
			}
			var rows []TenantSummary
			if err := json.Unmarshal(list.Body.Bytes(), &rows); err != nil {
				t.Fatalf("list body: %v: %s", err, list.Body.String())
			}
			var row *TenantSummary
			for i := range rows {
				if rows[i].ID == nonRegularTenantID {
					row = &rows[i]
				}
			}
			if row == nil {
				t.Fatalf("no list row for %s: %s", nonRegularTenantID, list.Body.String())
			}
			if row.ConfigError == "" || row.ConfigError != byID["config_error"] {
				t.Errorf("list config_error = %q, by-id config_error = %v; want the same non-empty value",
					row.ConfigError, byID["config_error"])
			}
		})
	}
}
