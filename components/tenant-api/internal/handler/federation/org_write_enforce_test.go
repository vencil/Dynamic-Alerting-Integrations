package federation

// ADR-027 / LD-6 P4b §5c — enforce-mode 403 harness for the federation
// write-plane org-scope gates (sites #6-#9) plus the platform-"*" invariant
// pins (I6). Mirrors internal/handler/org_write_enforce_test.go: an
// org-scoped rule loaded through the PRODUCTION rbac.NewManager path (claim
// key declared → validateConfig passes → middleware resolves the caller org
// claim), EnableOrgScopeEnforce, a real tenantorg manager, and side-effect
// spies so a denied request provably wrote nothing.

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
	"github.com/vencil/tenant-api/internal/testutil"
)

const (
	fedOrgClaimHeader  = "X-Auth-Request-Org"
	fedOrgGroup        = "fed-org-admins"
	fedOrgReadAllGroup = "fed-read-all"
	fedOrgTenant       = "tenant-org-fed"
	fedOrgMember       = "ORG-ALPHA"
	fedOrgOutsider     = "ORG-BETA"
)

const fedOrgRBACYAML = `groups:
  - name: ` + fedOrgGroup + `
    tenants: ["*"]
    permissions: [read, write, admin]
    org-scope: org
`

// newFedOrgEnforceRBAC builds the enforce-mode org-scoped RBAC manager via
// the production constructor (validateConfig + claim-header declaration).
func newFedOrgEnforceRBAC(t *testing.T) *rbac.Manager {
	t.Helper()
	return newFedOrgEnforceRBACFrom(t, fedOrgRBACYAML)
}

// newFedOrgEnforceRBACFrom is newFedOrgEnforceRBAC over an arbitrary rule set,
// for cases whose read-org-set must differ from their admin-org-set.
func newFedOrgEnforceRBACFrom(t *testing.T, rbacYAML string) *rbac.Manager {
	t.Helper()
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", rbacYAML)
	mgr, err := rbac.NewManager(rbacFile, map[string]string{"org": fedOrgClaimHeader})
	if err != nil {
		t.Fatalf("rbac.NewManager: %v", err)
	}
	mgr.EnableOrgScopeEnforce()
	return mgr
}

func newFedOrgTenantOrg() *tenantorg.Manager {
	return tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{
		fedOrgTenant: {fedOrgMember},
	}})
}

func fedOrgIdentity(req *http.Request, callerOrg string) *http.Request {
	req.Header.Set("X-Forwarded-Email", "fed-org-caller@example.com")
	req.Header.Set("X-Forwarded-Groups", fedOrgGroup)
	req.Header.Set(fedOrgClaimHeader, callerOrg)
	return req
}

// ── Site #6: CreateFederationToken ─────────────────────────────────────────

func TestOrgWriteEnforce_CreateFederationToken(t *testing.T) {
	t.Parallel()
	run := func(t *testing.T, callerOrg string) (*httptest.ResponseRecorder, *handler.Deps) {
		t.Helper()
		rbacMgr := newFedOrgEnforceRBAC(t)
		d := &handler.Deps{RBAC: rbacMgr, TenantOrg: newFedOrgTenantOrg(), Federation: newTestFederation(t)}
		req := httptest.NewRequest("POST", "/api/v1/federation/tokens",
			bytes.NewBufferString(`{"tenant_id":"`+fedOrgTenant+`","description":"harness"}`))
		req.Header.Set("Content-Type", "application/json")
		req = fedOrgIdentity(req, callerOrg)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(CreateFederationToken(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)
		return w, d
	}

	t.Run("outsider_denied_403_no_issue", func(t *testing.T) {
		t.Parallel()
		w, d := run(t, fedOrgOutsider)
		if w.Code != http.StatusForbidden {
			t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
		}
		recs, err := d.Federation.List(fedOrgTenant)
		if err != nil {
			t.Fatalf("List: %v", err)
		}
		if len(recs) != 0 {
			t.Errorf("denied issuance still stored %d record(s)", len(recs))
		}
	})
	t.Run("member_allowed_201", func(t *testing.T) {
		t.Parallel()
		w, _ := run(t, fedOrgMember)
		if w.Code != http.StatusCreated {
			t.Fatalf("status = %d, want 201; body=%s", w.Code, w.Body.String())
		}
	})
}

// ── Site #7: ListFederationTokens (read, but gated — enumeration oracle) ──

func TestOrgWriteEnforce_ListFederationTokens(t *testing.T) {
	t.Parallel()
	run := func(t *testing.T, callerOrg string) *httptest.ResponseRecorder {
		t.Helper()
		rbacMgr := newFedOrgEnforceRBAC(t)
		d := &handler.Deps{RBAC: rbacMgr, TenantOrg: newFedOrgTenantOrg(), Federation: newTestFederation(t)}
		req := httptest.NewRequest("GET", "/api/v1/federation/tokens?tenant_id="+fedOrgTenant, nil)
		req = fedOrgIdentity(req, callerOrg)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(ListFederationTokens(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)
		return w
	}

	t.Run("outsider_denied_403", func(t *testing.T) {
		t.Parallel()
		if w := run(t, fedOrgOutsider); w.Code != http.StatusForbidden {
			t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
		}
	})
	t.Run("member_allowed_200", func(t *testing.T) {
		t.Parallel()
		if w := run(t, fedOrgMember); w.Code != http.StatusOK {
			t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
		}
	})
}

// ── Site #8: DeleteFederationToken ─────────────────────────────────────────

func TestOrgWriteEnforce_DeleteFederationToken(t *testing.T) {
	t.Parallel()
	run := func(t *testing.T, callerOrg string) (*httptest.ResponseRecorder, *handler.Deps) {
		t.Helper()
		rbacMgr := newFedOrgEnforceRBAC(t)
		fed := newTestFederation(t)
		_, rec, err := fed.Issue(fedOrgTenant, "seed@example.com", "seed")
		if err != nil {
			t.Fatalf("Issue: %v", err)
		}
		d := &handler.Deps{RBAC: rbacMgr, TenantOrg: newFedOrgTenantOrg(), Federation: fed}
		req := newRequestWithChiParam("DELETE", "/api/v1/federation/tokens/"+rec.TokenID, "id", rec.TokenID, nil)
		req = fedOrgIdentity(req, callerOrg)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(DeleteFederationToken(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)
		return w, d
	}

	t.Run("outsider_denied_403_record_kept", func(t *testing.T) {
		t.Parallel()
		w, d := run(t, fedOrgOutsider)
		if w.Code != http.StatusForbidden {
			t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
		}
		recs, err := d.Federation.List(fedOrgTenant)
		if err != nil {
			t.Fatalf("List: %v", err)
		}
		if len(recs) != 1 {
			t.Errorf("denied revocation removed the record: %d record(s) remain, want 1", len(recs))
		}
	})
	t.Run("member_allowed_200", func(t *testing.T) {
		t.Parallel()
		w, _ := run(t, fedOrgMember)
		if w.Code != http.StatusOK {
			t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
		}
	})
}

// ── Site #9: PutTenantFederation ───────────────────────────────────────────

func TestOrgWriteEnforce_PutTenantFederation(t *testing.T) {
	t.Parallel()
	run := func(t *testing.T, rbacYAML, groups, callerOrg string) (*httptest.ResponseRecorder, *atomic.Int32) {
		t.Helper()
		rbacMgr := newFedOrgEnforceRBACFrom(t, rbacYAML)
		configDir := setupConfigDir(t, nil)
		initGitRepo(t, configDir)
		writer := newTestWriter(configDir)
		var writes atomic.Int32
		writer.SetOnWrite(func(string) { writes.Add(1) })
		tenantOrg := newFedOrgTenantOrg()
		// PUT /tenants/{id}/federation mounts PermRead middleware (the handler does
		// its own PermAdmin check), so the P4c read-by-id middleware gate applies.
		// Mirror main.go: wire the same tenantorg the handler uses into the manager
		// so the middleware read gate resolves the tenant's orgs (an unwired
		// resolver would treat the labeled tenant as unlabeled → enforce-deny).
		rbacMgr.SetOrgResolver(func(tid string) []string { orgs, _ := tenantOrg.OrgsForTenant(tid); return orgs })
		d := &handler.Deps{
			RBAC:             rbacMgr,
			TenantOrg:        tenantOrg,
			ConfigDir:        configDir,
			Writer:           writer,
			FederationPolicy: fedpolicy.NewManager(configDir),
		}
		req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+fedOrgTenant+"/federation",
			"id", fedOrgTenant, bytes.NewBufferString(`{"metrics":[]}`))
		req.Header.Set("Content-Type", "application/json")
		req = fedOrgIdentity(req, callerOrg)
		req.Header.Set("X-Forwarded-Groups", groups)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(PutTenantFederation(d), rbacMgr, rbac.PermRead, handler.TenantIDFromPath).ServeHTTP(w, req)
		return w, &writes
	}

	t.Run("outsider_denied_403_no_write", func(t *testing.T) {
		t.Parallel()
		w, writes := run(t, fedOrgRBACYAML, fedOrgGroup, fedOrgOutsider)
		if w.Code != http.StatusForbidden {
			t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
		}
		if n := writes.Load(); n != 0 {
			t.Errorf("denied subset edit committed %d time(s), want 0", n)
		}
	})
	t.Run("member_allowed_200", func(t *testing.T) {
		t.Parallel()
		w, writes := run(t, fedOrgRBACYAML, fedOrgGroup, fedOrgMember)
		if w.Code != http.StatusOK {
			t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
		}
		if n := writes.Load(); n != 1 {
			t.Errorf("writer commits = %d, want 1", n)
		}
	})

	// #1754: with the single-group fixture above the read-org-set equals the
	// admin-org-set, so the outsider is already rejected by the PermRead
	// middleware and the handler's own PermAdmin org gate is never reached.
	// Split the grants so the two sets differ: an org-blind read grant on every
	// tenant (passes the middleware read-by-id gate for any org) plus an
	// org-scoped admin grant. Rules OR together, so an outsider holding both
	// groups passes read but has no rule granting admin in its org — the
	// handler's OrgAllowed(PermAdmin) is the ONLY thing that can deny it.
	splitYAML := `groups:
  - name: ` + fedOrgReadAllGroup + `
    tenants: ["*"]
    permissions: [read]
  - name: ` + fedOrgGroup + `
    tenants: ["*"]
    permissions: [read, write, admin]
    org-scope: org
`
	splitGroups := fedOrgReadAllGroup + "," + fedOrgGroup

	t.Run("read_passes_admin_org_gate_denies_403_no_write", func(t *testing.T) {
		t.Parallel()
		w, writes := run(t, splitYAML, splitGroups, fedOrgOutsider)
		if w.Code != http.StatusForbidden {
			t.Fatalf("status = %d, want 403 from the handler's PermAdmin org gate; body=%s", w.Code, w.Body.String())
		}
		// Pin that the 403 came from the handler, not the middleware: only the
		// handler's gate names the federation subset in its message.
		if !strings.Contains(w.Body.String(), "federation subset") {
			t.Fatalf("403 did not come from PutTenantFederation's admin org gate "+
				"(middleware rejected first?); body=%s", w.Body.String())
		}
		if n := writes.Load(); n != 0 {
			t.Errorf("denied subset edit committed %d time(s), want 0", n)
		}
	})
	// Contrast: same split rule set, member org → the org-scoped admin grant
	// applies, proving the denial above is the org axis and not a missing
	// permission in the fixture.
	t.Run("split_rules_member_allowed_200", func(t *testing.T) {
		t.Parallel()
		w, writes := run(t, splitYAML, splitGroups, fedOrgMember)
		if w.Code != http.StatusOK {
			t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
		}
		if n := writes.Load(); n != 1 {
			t.Errorf("writer commits = %d, want 1", n)
		}
	})
}

// ── Invariant I6: platform "*" sites are org-blind and UNCHANGED ──────────
//
// PutFederationPolicy and BackfillAccounts gate on Allowed(p, "*", admin).
// Org-scope deliberately does not apply to platform scope: flipping enforce
// must not change these gates' outcome for an org-scoped-only caller (the
// tripwire auto-exempts the "*" literal for the same reason).

func TestOrgWriteEnforce_PlatformStarSitesUnchanged(t *testing.T) {
	t.Parallel()

	t.Run("put_federation_policy_not_org_denied", func(t *testing.T) {
		t.Parallel()
		rbacMgr := newFedOrgEnforceRBAC(t)
		configDir := setupConfigDir(t, nil)
		initGitRepo(t, configDir)
		d := &handler.Deps{
			RBAC:             rbacMgr,
			TenantOrg:        newFedOrgTenantOrg(),
			ConfigDir:        configDir,
			Writer:           newTestWriter(configDir),
			FederationPolicy: fedpolicy.NewManager(configDir),
		}
		req := httptest.NewRequest("PUT", "/api/v1/federation/policy",
			bytes.NewBufferString(`{"whitelist":[]}`))
		req.Header.Set("Content-Type", "application/json")
		req = fedOrgIdentity(req, fedOrgOutsider) // org value irrelevant at "*"
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(PutFederationPolicy(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

		if w.Code == http.StatusForbidden {
			t.Fatalf("platform \"*\" whitelist gate denied an org-scoped platform-rule caller under enforce "+
				"— org axis leaked into platform scope (I6): %s", w.Body.String())
		}
	})

	t.Run("backfill_accounts_passes_star_gate", func(t *testing.T) {
		t.Parallel()
		rbacMgr := newFedOrgEnforceRBAC(t)
		d := &handler.Deps{RBAC: rbacMgr, TenantOrg: newFedOrgTenantOrg()}
		req := httptest.NewRequest("POST", "/api/v1/federation/accounts/backfill", nil)
		req = fedOrgIdentity(req, fedOrgOutsider)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(BackfillAccounts(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

		// Accounts is deliberately unwired: passing the "*" gate lands on the
		// 503 "not configured" branch — deterministically NOT 403.
		if w.Code == http.StatusForbidden {
			t.Fatalf("platform \"*\" backfill gate denied an org-scoped platform-rule caller under enforce (I6): %s",
				w.Body.String())
		}
		if w.Code != http.StatusServiceUnavailable {
			t.Fatalf("status = %d, want 503 (past the \"*\" gate, Accounts unwired); body=%s", w.Code, w.Body.String())
		}
	})

	// Contrast pin: a caller whose groups do NOT match the org-scoped rule is
	// still denied at "*" — proving the pass above comes from the rule's
	// tenants:["*"] grant, not from any org-axis leniency.
	t.Run("unmatched_caller_still_403", func(t *testing.T) {
		t.Parallel()
		rbacMgr := newFedOrgEnforceRBAC(t)
		d := &handler.Deps{RBAC: rbacMgr, TenantOrg: newFedOrgTenantOrg()}
		req := httptest.NewRequest("POST", "/api/v1/federation/accounts/backfill", nil)
		req.Header.Set("X-Forwarded-Email", "stranger@example.com")
		req.Header.Set("X-Forwarded-Groups", "unrelated-group")
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(BackfillAccounts(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)
		if w.Code != http.StatusForbidden {
			t.Fatalf("status = %d, want 403 for a non-matching caller; body=%s", w.Code, w.Body.String())
		}
	})
}
