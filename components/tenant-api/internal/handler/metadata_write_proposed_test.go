package handler

import (
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
)

// Regression tests for the post-state half of the #1597 write gate.
//
// The gap: RequireOrgWrite authorizes against the tenant's metadata AS IT IS ON
// DISK, but PUT /tenants/{id} is a whole-file replace and `_metadata` lives
// inside the file being replaced. A caller scoped to environment=production
// could therefore submit a body setting environment=dev — authorized against
// production, committed into dev.
//
// The org axis is NOT exposed this way (org membership lives in the admin-only
// _tenant_orgs.yaml, a separate file this path cannot write), and neither is
// the batch path (it refuses to overwrite a structured key like `_metadata`
// with a scalar patch). Whole-file PUT is the exposed shape, so that is what
// these tests pin.

func proposedTestCfg() *rbac.RBACConfig {
	return &rbac.RBACConfig{Groups: []rbac.GroupRule{{
		Name: "prod-ops", Tenants: []string{"db-*"},
		Permissions:  []rbac.Permission{rbac.PermRead, rbac.PermWrite},
		Environments: []string{"production"},
	}}}
}

func proposedTestPrincipal() *rbac.VerifiedPrincipal {
	return &rbac.VerifiedPrincipal{Email: "ops@example.com", Groups: []string{"prod-ops"}}
}

const (
	tenantInProd = "tenants:\n  db-a:\n    _metadata:\n      environment: production\n"
	tenantInDev  = "tenants:\n  db-a:\n    _metadata:\n      environment: dev\n"
)

// proposedScopeMeta must read the BODY, not the disk — otherwise the post-state
// check is the pre-state check wearing a different name.
func TestProposedScopeMetaReadsTheBody(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{"db-a.yaml": tenantInProd})

	if onDisk, _ := WriteScopeMeta(dir)("db-a"); onDisk != "production" {
		t.Fatalf("precondition: on-disk environment = %q, want production", onDisk)
	}
	proposed, _ := proposedScopeMeta(tenantInDev)("db-a")
	if proposed != "dev" {
		t.Errorf("proposedScopeMeta = %q, want dev — it is reading something other than the body", proposed)
	}
}

// The gap itself: the pre-state check passes (the tenant IS production today)
// while the post-state check refuses (the body would make it dev). Both halves
// asserted in one place so a future edit cannot quietly drop the second.
func TestWriteThatRelabelsTenantOutOfScopeIsRefused(t *testing.T) {
	t.Parallel()
	dir := setupConfigDir(t, map[string]string{"db-a.yaml": tenantInProd})
	p := proposedTestPrincipal()

	m := rbac.NewForTest(proposedTestCfg())
	m.EnableMetadataWriteScopeEnforce()

	if !OrgAllowed(m, nil, p, "db-a", rbac.PermWrite, WriteScopeMeta(dir)) {
		t.Fatal("pre-state gate must allow: the tenant is production today and the caller is production-scoped")
	}
	if OrgAllowed(m, nil, p, "db-a", rbac.PermWrite, proposedScopeMeta(tenantInDev)) {
		t.Error("post-state gate must refuse a body that moves the tenant to an environment the caller does not administer")
	}
	// A body that keeps the tenant in scope stays writable — the check narrows
	// one shape, it does not block ordinary edits.
	if !OrgAllowed(m, nil, p, "db-a", rbac.PermWrite, proposedScopeMeta(tenantInProd)) {
		t.Error("post-state gate must still allow a body that keeps the tenant in the caller's environment")
	}
}

// Migration safety: the post-state check runs the SAME predicate as the rest of
// the axis, so it must be inert until the flag flips. If this ever fails, the
// follow-up has started tightening deployments that never opted in.
func TestPostStateCheckIsInertInShadowMode(t *testing.T) {
	t.Parallel()
	p := proposedTestPrincipal()
	m := rbac.NewForTest(proposedTestCfg()) // shadow: flag NOT enabled

	if !OrgAllowed(m, nil, p, "db-a", rbac.PermWrite, proposedScopeMeta(tenantInDev)) {
		t.Error("shadow mode must still allow — the post-state check must not tighten anyone before the flip")
	}
}
