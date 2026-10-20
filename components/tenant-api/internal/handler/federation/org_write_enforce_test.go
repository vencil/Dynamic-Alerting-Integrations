package federation

// ADR-027 / LD-6 P4b §5c — enforce-mode 403 harness for the federation
// write-plane org-scope gates (sites #6-#9), plus the platform-wide write bar
// (#3160: an org-scoped "*" admin is refused at the platform-wide writes). Mirrors internal/handler/org_write_enforce_test.go: an
// org-scoped rule loaded through the PRODUCTION rbac.NewManager path (claim
// key declared → validateConfig passes → middleware resolves the caller org
// claim), EnableOrgScopeEnforce, a real tenantorg manager, and side-effect
// spies so a denied request provably wrote nothing.

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/statestore"
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

	t.Run("outsider_denied_404_record_kept", func(t *testing.T) {
		t.Parallel()
		w, d := run(t, fedOrgOutsider)
		// #3123: refused like a missing token (TestIssue3123_DeleteFederationToken_*).
		if w.Code != http.StatusNotFound {
			t.Fatalf("status = %d, want 404; body=%s", w.Code, w.Body.String())
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
		st, err := statestore.NewDir(configDir, func(string) { writes.Add(1) })
		if err != nil {
			t.Fatal(err)
		}
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
			State:            st,
			Writer:           writer,
			FederationPolicy: fedpolicy.NewDirManager(configDir),
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

// ── Platform-wide write bar: non-org-scoped "*" admin only (#3160) ────────
//
// PutFederationPolicy and BackfillAccounts change platform-wide state (the
// fleet's federation whitelist; every tenant's AccountID). Their bar is
// rbac.PlatformAdminNonOrgScoped — the same bar as the /audit endpoints — NOT
// the bare Allowed(p, "*", admin), whose org-blind seam lets an ORG-SCOPED
// "*" admin rule through. This is a BAR, not the org-axis would-deny: an
// org-scoped "*" admin is refused whether org-scope enforce is on or off, and
// the refused request writes nothing.

const (
	fedPlatformAdminGroup = "fed-platform-admins"
	fedSeedMetric         = "seed_metric_up"
	fedPutMetric          = "replacement_metric_up"
)

// fedPlatformBarRBACYAML holds both shapes of "*" admin rule: the org-scoped
// one (fedOrgGroup) that must be refused, and a non-org-scoped one
// (fedPlatformAdminGroup) that must still pass.
const fedPlatformBarRBACYAML = fedOrgRBACYAML + `  - name: ` + fedPlatformAdminGroup + `
    tenants: ["*"]
    permissions: [read, write, admin]
`

func newFedPlatformBarRBAC(t *testing.T, enforce bool) *rbac.Manager {
	t.Helper()
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", fedPlatformBarRBACYAML)
	mgr, err := rbac.NewManager(rbacFile, map[string]string{"org": fedOrgClaimHeader})
	if err != nil {
		t.Fatalf("rbac.NewManager: %v", err)
	}
	if enforce {
		mgr.EnableOrgScopeEnforce()
	}
	return mgr
}

func TestPlatformWriteBar_OrgScopedStarAdminDenied(t *testing.T) {
	t.Parallel()

	const seedPolicy = "whitelist:\n  - metric: " + fedSeedMetric + "\n"

	putPolicy := func(t *testing.T, enforce bool, groups, callerOrg string) (*httptest.ResponseRecorder, *handler.Deps, string) {
		t.Helper()
		rbacMgr := newFedPlatformBarRBAC(t, enforce)
		configDir := setupConfigDir(t, map[string]string{statestore.KeyFederationPolicy: seedPolicy})
		initGitRepo(t, configDir)
		d := &handler.Deps{
			RBAC:             rbacMgr,
			TenantOrg:        newFedOrgTenantOrg(),
			ConfigDir:        configDir,
			State:            newDirState(t, configDir),
			Writer:           newTestWriter(configDir),
			FederationPolicy: fedpolicy.NewDirManager(configDir),
		}
		req := httptest.NewRequest("PUT", "/api/v1/federation/policy",
			bytes.NewBufferString(`{"whitelist":[{"metric":"`+fedPutMetric+`"}]}`))
		req.Header.Set("Content-Type", "application/json")
		req = fedOrgIdentity(req, callerOrg)
		req.Header.Set("X-Forwarded-Groups", groups)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(PutFederationPolicy(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)
		return w, d, configDir
	}

	backfill := func(t *testing.T, enforce bool, groups, callerOrg string) (*httptest.ResponseRecorder, string) {
		t.Helper()
		files := map[string]string{fedOrgTenant + ".yaml": "tenants:\n  " + fedOrgTenant + ": {}\n"}
		// The helper's plain manager cannot load an org-scoped rule (no claim
		// header declared); replace it with the claim-aware one under test.
		d, configDir := newLogsFederationDeps(t, platformAdminRBAC, files)
		d.RBAC = newFedPlatformBarRBAC(t, enforce)
		d.TenantOrg = newFedOrgTenantOrg()
		req := httptest.NewRequest("POST", "/api/v1/federation/accounts/backfill", nil)
		req = fedOrgIdentity(req, callerOrg)
		req.Header.Set("X-Forwarded-Groups", groups)
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(BackfillAccounts(d), d.RBAC, rbac.PermRead, nil).ServeHTTP(w, req)
		return w, configDir
	}

	for _, mode := range []struct {
		name    string
		enforce bool
	}{{"enforce", true}, {"shadow", false}} {
		t.Run(mode.name, func(t *testing.T) {
			t.Parallel()

			// The org value must not matter: a member of the only labeled org
			// is refused exactly like an outsider.
			for _, callerOrg := range []string{fedOrgMember, fedOrgOutsider} {
				t.Run("put_federation_policy_org_scoped_403_unchanged_"+callerOrg, func(t *testing.T) {
					t.Parallel()
					w, d, configDir := putPolicy(t, mode.enforce, fedOrgGroup, callerOrg)
					if w.Code != http.StatusForbidden {
						t.Fatalf("status = %d, want 403 for an org-scoped \"*\" admin editing the platform whitelist; body=%s",
							w.Code, w.Body.String())
					}
					if !strings.Contains(w.Body.String(), "non-org-scoped") {
						t.Errorf("403 body does not name the non-org-scoped bar; body=%s", w.Body.String())
					}
					got, err := os.ReadFile(filepath.Join(configDir, statestore.KeyFederationPolicy))
					if err != nil {
						t.Fatalf("read policy: %v", err)
					}
					if string(got) != seedPolicy {
						t.Errorf("denied PUT rewrote the policy:\n got %q\nwant %q", got, seedPolicy)
					}
					if d.FederationPolicy.IsWhitelisted(fedPutMetric) {
						t.Errorf("denied PUT made %s live in the whitelist", fedPutMetric)
					}
				})

				t.Run("backfill_accounts_org_scoped_403_no_registry_"+callerOrg, func(t *testing.T) {
					t.Parallel()
					w, configDir := backfill(t, mode.enforce, fedOrgGroup, callerOrg)
					if w.Code != http.StatusForbidden {
						t.Fatalf("status = %d, want 403 for an org-scoped \"*\" admin running the fleet backfill; body=%s",
							w.Code, w.Body.String())
					}
					if !strings.Contains(w.Body.String(), "non-org-scoped") {
						t.Errorf("403 body does not name the non-org-scoped bar; body=%s", w.Body.String())
					}
					if _, ok := readRegistry(t, configDir).Lookup(fedOrgTenant); ok {
						t.Errorf("denied backfill allocated an account_id for %s", fedOrgTenant)
					}
				})
			}

			// Contrast: a NON-org-scoped "*" admin still passes both bars —
			// the denials above are the org-scope of the rule, not a missing
			// permission in the fixture.
			t.Run("put_federation_policy_non_org_scoped_200_written", func(t *testing.T) {
				t.Parallel()
				w, d, configDir := putPolicy(t, mode.enforce, fedPlatformAdminGroup, fedOrgOutsider)
				if w.Code != http.StatusOK {
					t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
				}
				if !d.FederationPolicy.IsWhitelisted(fedPutMetric) {
					t.Errorf("IsWhitelisted(%s) = false after an allowed PUT", fedPutMetric)
				}
				got, err := os.ReadFile(filepath.Join(configDir, statestore.KeyFederationPolicy))
				if err != nil {
					t.Fatalf("read policy: %v", err)
				}
				if string(got) == seedPolicy {
					t.Error("allowed PUT left the seeded policy in place")
				}
			})
			t.Run("backfill_accounts_non_org_scoped_200_allocated", func(t *testing.T) {
				t.Parallel()
				w, configDir := backfill(t, mode.enforce, fedPlatformAdminGroup, fedOrgOutsider)
				if w.Code != http.StatusOK {
					t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
				}
				if _, ok := readRegistry(t, configDir).Lookup(fedOrgTenant); !ok {
					t.Errorf("allowed backfill did not allocate an account_id for %s", fedOrgTenant)
				}
			})

			// Contrast: a caller matching no rule is refused too.
			t.Run("unmatched_caller_403", func(t *testing.T) {
				t.Parallel()
				w, _ := backfill(t, mode.enforce, "unrelated-group", fedOrgMember)
				if w.Code != http.StatusForbidden {
					t.Fatalf("status = %d, want 403 for a non-matching caller; body=%s", w.Code, w.Body.String())
				}
			})
		})
	}
}
