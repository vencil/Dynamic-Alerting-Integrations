package federation

import (
	"net/http/httptest"
	"testing"

	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/rbac"
)

// #3123 item 4: the refusal to revoke another org's token named the token's
// tenant ("admin permission required on tenant <id>"), so any authenticated
// caller could resolve a token id to its tenant. The refusal must be the
// missing-token 404, byte for byte.
func TestIssue3123_DeleteFederationToken_RefusalIsTheMissingToken404(t *testing.T) {
	t.Parallel()
	rbacMgr := newFedOrgEnforceRBAC(t)
	fed := newTestFederation(t)
	_, rec, err := fed.Issue(fedOrgTenant, "seed@example.com", "seed")
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}
	del := func(d *handler.Deps) *httptest.ResponseRecorder {
		req := newRequestWithChiParam("DELETE", "/api/v1/federation/tokens/"+rec.TokenID, "id", rec.TokenID, nil)
		req = fedOrgIdentity(req, fedOrgOutsider)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(DeleteFederationToken(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)
		return w
	}
	refused := del(&handler.Deps{RBAC: rbacMgr, TenantOrg: newFedOrgTenantOrg(), Federation: fed})
	missing := del(&handler.Deps{RBAC: rbacMgr, TenantOrg: newFedOrgTenantOrg(), Federation: newTestFederation(t)})

	if missing.Code != 404 {
		t.Fatalf("control: missing token = %d %s, want 404", missing.Code, missing.Body.String())
	}
	if refused.Code != missing.Code || refused.Body.String() != missing.Body.String() {
		t.Errorf("refusal differs from the missing-token answer:\n refused: %d %s\n missing: %d %s",
			refused.Code, refused.Body.String(), missing.Code, missing.Body.String())
	}
	if recs, _ := fed.List(fedOrgTenant); len(recs) != 1 {
		t.Errorf("refused revocation removed the record")
	}
}
