package handler

// ADR-027 §9 item 7: `config check --kind scope` predicts what the server
// does. For every rule × tenant, the offline listing reports an environment,
// domain or organization gap exactly when the server's scope axes deny that
// rule's subject — on the list plane (filterTenantsByRBAC: ScopeAllowed, or
// ScopeAllowedUnknownMetadata for a tenant whose metadata cannot be read;
// the derived plane uses the latter too) and on the write plane (OrgAllowed
// with WriteScopeMeta, which reads an unusable file as unlabeled). A change
// to one side that the other does not follow turns this red.

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

// adr-check: ADR-027/9.7b
func TestScopeGapsMatchEveryPlane(t *testing.T) {
	t.Parallel()
	// Every rule's allow-lists include the labeled tenant's labels, so a
	// denial here is a missing or unknown label, never a label outside the
	// allow-list (which the gap listing deliberately does not judge).
	const rules = `groups:
  - name: g-none
    tenants: ["*"]
    permissions: [read, write]
  - name: g-env
    tenants: ["*"]
    permissions: [read, write]
    environments: [production]
  - name: g-dom
    tenants: ["*"]
    permissions: [read, write]
    domains: [finance]
  - name: g-both
    tenants: ["t-*"]
    permissions: [read, write]
    environments: [production]
    domains: [finance]
  - name: g-elsewhere
    tenants: ["x-*"]
    permissions: [read, write]
    environments: [production]
  - name: g-org
    tenants: ["*"]
    permissions: [read, write]
    org-scope: org
`
	// The org axis: g-org's subject carries org=acme, so a tenant mapped to
	// acme passes and one with no organization is the gap. t-broken is mapped
	// too, so "metadata unknown" and "no organization" stay apart (the org
	// axis does not read the tenant file).
	const orgClaim = "org"
	tenantOrgs := map[string][]string{"t-labeled": {"acme"}, "t-broken": {"acme"}}
	dir := t.TempDir()
	for name, body := range map[string]string{
		"t-labeled.yaml":   "tenants:\n  t-labeled:\n    _metadata:\n      environment: production\n      domain: finance\n",
		"t-unlabeled.yaml": "tenants:\n  t-unlabeled: {}\n",
		"t-broken.yaml":    "tenants:\n  t-broken: [unclosed\n",
		"t-idmis.yaml":     "tenants:\n  other-id:\n    _metadata:\n      environment: production\n",
	} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	mgr := newRBACManagerWithClaims(t, rules, map[string]string{orgClaim: "X-Auth-Request-Org"})
	orgs := tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: tenantOrgs})

	rows, err := LoadTenantScopeLabels(dir)
	if err != nil {
		t.Fatal(err)
	}
	labels := make([]rbac.TenantLabels, 0, len(rows))
	for _, r := range rows {
		labels = append(labels, rbac.TenantLabels{ID: r.ID, Environment: r.Environment, Domain: r.Domain,
			MetadataUnknown: r.MetadataUnknown(), Orgs: tenantOrgs[r.ID]})
	}
	cfg := mgr.Get()
	type key struct{ rule, tenant string }
	gap := map[key]bool{}
	reasons := map[string]int{}
	axes := map[rbac.ScopeAxis]int{}
	for _, g := range cfg.UnlabeledScopeGaps(labels) {
		gap[key{g.Rule, g.Tenant}] = true
		reasons[g.Reason]++
		axes[g.Axis]++
	}
	// Vacuity guards: both reasons and all three axes occur, the org rule
	// also passes somewhere, and the fixture has every kind of row.
	if reasons[rbac.GapReasonUnlabeled] == 0 || reasons[rbac.GapReasonMetadataUnknown] == 0 {
		t.Fatalf("gap reasons %v: the fixture must produce both", reasons)
	}
	for _, axis := range []rbac.ScopeAxis{rbac.ScopeAxisEnvironment, rbac.ScopeAxisDomain, rbac.ScopeAxisOrg} {
		if axes[axis] == 0 {
			t.Fatalf("gap axes %v: the fixture must produce a gap on %s", axes, axis)
		}
	}
	if gap[key{"g-org", "t-labeled"}] || gap[key{"g-org", "t-broken"}] {
		t.Fatalf("g-org has a gap on a tenant mapped to acme: the fixture lost its passing org rows")
	}
	unknown := 0
	for _, r := range rows {
		if r.MetadataUnknown() {
			unknown++
		}
	}
	if len(rows) != 4 || unknown != 2 {
		t.Fatalf("rows %d (unknown %d), want 4 (2)", len(rows), unknown)
	}

	for _, rule := range cfg.Groups {
		p := &rbac.VerifiedPrincipal{Email: "u@example.com", Subject: "u@example.com", Groups: []string{rule.Name},
			Claims: map[string]string{orgClaim: "acme"}}
		for _, r := range rows {
			// g-elsewhere's x-* matches no fixture tenant; every other rule
			// matches every one (named here rather than re-implementing the
			// rbac package's pattern matcher).
			if rule.Name == "g-elsewhere" {
				if gap[key{rule.Name, r.ID}] {
					t.Errorf("%s × %s: a gap for a rule that does not apply", rule.Name, r.ID)
				}
				continue
			}
			// As tenant_list.go does: the org axis from the tenant's org list.
			rowOrgs, _ := orgs.OrgsForTenant(r.ID)
			var listAllowed bool
			if r.MetadataUnknown() {
				listAllowed = mgr.ScopeAllowedUnknownMetadata(p, r.ID, rowOrgs)
			} else {
				listAllowed = mgr.ScopeAllowed(p, r.ID, r.Environment, r.Domain, rowOrgs)
			}
			writeAllowed := OrgAllowed(mgr, orgs, p, r.ID, rbac.PermWrite, WriteScopeMeta(dir))
			g := gap[key{rule.Name, r.ID}]
			if g == listAllowed {
				t.Errorf("%s × %s: offline gap %v, list plane allowed %v", rule.Name, r.ID, g, listAllowed)
			}
			if g == writeAllowed {
				t.Errorf("%s × %s: offline gap %v, write plane allowed %v", rule.Name, r.ID, g, writeAllowed)
			}
		}
	}
}

// The one case the planes differ on, pinned so a change to either side is
// seen: when only the root platform layer cannot be read, the list marks a
// healthy tenant's metadata unknown (and the offline listing follows it with
// a metadata_unknown gap), while the write plane answers from the tenant
// file's own `_metadata` (platformMetadataOrNone).
func TestScopeGapsRootPlatformUnreadableFollowsTheList(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "t-own.yaml"),
		[]byte("tenants:\n  t-own:\n    _metadata:\n      environment: production\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// A root `_` config file whose read fails: the root layer is unreadable.
	if err := os.Symlink("nowhere", filepath.Join(dir, "_defaults.yaml")); err != nil {
		t.Fatal(err)
	}
	mgr := newRBACManagerWithClaims(t, "groups:\n  - name: g-env\n    tenants: [\"*\"]\n    permissions: [read, write]\n    environments: [production]\n", nil)
	orgs := tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{}})
	rows, err := LoadTenantScopeLabels(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || !rows[0].MetadataUnknown() {
		t.Fatalf("rows %+v: want t-own with its metadata unknown", rows)
	}
	gaps := mgr.Get().UnlabeledScopeGaps([]rbac.TenantLabels{{ID: "t-own", MetadataUnknown: true}})
	if len(gaps) != 1 || gaps[0].Reason != rbac.GapReasonMetadataUnknown {
		t.Fatalf("gaps %+v: want one metadata_unknown gap", gaps)
	}
	p := &rbac.VerifiedPrincipal{Email: "u@example.com", Subject: "u@example.com", Groups: []string{"g-env"}}
	if mgr.ScopeAllowedUnknownMetadata(p, "t-own", nil) {
		t.Error("list plane shows t-own to g-env; the offline gap says it does not")
	}
	if !OrgAllowed(mgr, orgs, p, "t-own", rbac.PermWrite, WriteScopeMeta(dir)) {
		t.Error("write plane refuses t-own: it no longer reads the tenant file's own _metadata " +
			"when the root layer is unreadable — update the gap listing's file comment and this test")
	}
}
