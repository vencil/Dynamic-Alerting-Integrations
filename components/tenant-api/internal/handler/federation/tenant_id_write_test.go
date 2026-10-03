package federation

import (
	"net/http"
	"os"
	"path/filepath"
	"testing"

	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/handler"
)

// TestPutTenantFederation_InvalidTenantID (ADR-035): the subset PUT is a
// write, so it takes the writable check — an id the tenant-id rule refuses is
// an early 400 and nothing is written. Before ADR-035 this route ran the read
// check alone (handler.ValidateTenantID) and wrote _federation/UPPER.yaml.
// GET keeps the read check, so such a tenant's subset can still be read.
func TestPutTenantFederation_InvalidTenantID(t *testing.T) {
	t.Parallel()
	for id, want := range map[string]int{
		"UPPER":  http.StatusBadRequest,
		"Team_A": http.StatusBadRequest,
		"db-a":   http.StatusOK,
	} {
		t.Run(id, func(t *testing.T) {
			t.Parallel()
			configDir := setupConfigDir(t, nil)
			initGitRepo(t, configDir)
			d := &handler.Deps{
				ConfigDir: configDir,
				Writer:    newTestWriter(configDir),
				RBAC:      newRBACManager(t, platformAdminRBAC),
				FederationPolicy: fedpolicy.NewManagerForTest(&fedpolicy.Config{
					Whitelist: []fedpolicy.WhitelistEntry{{Metric: "mysql_up"}},
				}),
			}
			w := executeWithRBAC(t, PutTenantFederation(d),
				fedReq(t, "PUT", "/api/v1/tenants/"+id+"/federation", "id", id, `{"metrics":["mysql_up"]}`))
			if w.Code != want {
				t.Fatalf("PUT status = %d, want %d; body: %s", w.Code, want, w.Body.String())
			}
			_, err := os.Stat(filepath.Join(configDir, "_federation", id+".yaml"))
			if want != http.StatusOK && err == nil {
				t.Errorf("refused PUT wrote _federation/%s.yaml", id)
			}
			g := executeWithRBAC(t, GetTenantFederation(d),
				fedReq(t, "GET", "/api/v1/tenants/"+id+"/federation", "id", id, ""))
			if g.Code != http.StatusOK {
				t.Errorf("GET status = %d, want 200 (reads are not narrowed); body: %s", g.Code, g.Body.String())
			}
		})
	}
}
