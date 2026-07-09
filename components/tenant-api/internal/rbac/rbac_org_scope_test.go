package rbac

// ADR-027 / LD-6 P4 — org-scope authorization axis.
//
// These tests pin the correctness core of ScopeAllowed: the org axis is folded
// into the SAME per-rule loop as the metadata axis (never a top-level AND, which
// would leak cross-rule access), the four (metadata,org) visibility booleans
// select the effective decision + per-axis would-deny signals, and a config with
// no org-scoped rule degenerates BYTE-IDENTICALLY to the pre-P4 metadata filter.
//
// fakeScopeRecorder / newFakeScopeRecorder live in rbac_scope_mode_test.go;
// equivConfig / equivGroupSets / equivTenants live in allowed_equiv_test.go.

import (
	"os"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
)

// ── pure scopeSetModes (the org analogue of scopeFieldModes) ────────────────

func TestScopeSetModes(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name        string
		userOrgVal  string
		tenantOrgs  []string
		wantShadow  bool
		wantEnforce bool
	}{
		{"unlabeled (nil): shadow yes, enforce no", "ORG-A", nil, true, false},
		{"unlabeled (empty slice): shadow yes, enforce no", "ORG-A", []string{}, true, false},
		{"unlabeled + no caller claim: shadow yes, enforce no", "", nil, true, false},
		{"labeled member: both yes", "ORG-A", []string{"ORG-A", "ORG-B"}, true, true},
		{"labeled non-member: both no", "ORG-C", []string{"ORG-A", "ORG-B"}, false, false},
		{"labeled + no caller claim: both no", "", []string{"ORG-A"}, false, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			gotShadow, gotEnforce := scopeSetModes(c.userOrgVal, c.tenantOrgs)
			if gotShadow != c.wantShadow || gotEnforce != c.wantEnforce {
				t.Errorf("scopeSetModes(%q, %v) = (%v, %v), want (%v, %v)",
					c.userOrgVal, c.tenantOrgs, gotShadow, gotEnforce, c.wantShadow, c.wantEnforce)
			}
		})
	}
}

// ── #2 cross-rule union non-leak (correctness core) ─────────────────────────

// TestScopeAllowed_CrossRuleUnionNoLeak proves org is folded per-rule, not
// AND'd across a per-axis union. Rule A grants metadata but fails org; rule B
// grants org but fails metadata; the caller hits both. A top-level
// metaVisible(from A) && orgVisible(from B) would show the tenant — but no
// SINGLE rule grants it, so the per-rule fold must hide it.
func TestScopeAllowed_CrossRuleUnionNoLeak(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		// Rule A: org-scoped, NO metadata restriction (env/domain wildcard).
		{Name: "org-team", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, OrgScope: "org"},
		// Rule B: metadata-scoped (env=production), NO org restriction.
		{Name: "prod-team", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, Environments: []string{"production"}},
	}}
	m := NewForTest(cfg)
	p := &VerifiedPrincipal{Groups: []string{"org-team", "prod-team"}, Claims: map[string]string{"org": "ORG-USER"}}

	// Controls: each rule ALONE grants when its own axis passes — proves both
	// rules are live and the leak case's invisibility is genuine cross-rule.
	// Rule A grants: tenant in the caller's org (env=staging → B fails).
	if !m.ScopeAllowed(p, "db-x", "staging", "", []string{"ORG-USER"}) {
		t.Error("control A: org-team alone must grant a same-org tenant (org matches, metadata wildcard)")
	}
	// Rule B grants: production tenant in a DIFFERENT org (A fails org).
	if !m.ScopeAllowed(p, "db-x", "production", "", []string{"ORG-OTHER"}) {
		t.Error("control B: prod-team alone must grant a production tenant (metadata matches, org wildcard)")
	}

	// Leak case: env=staging (B fails) AND org=ORG-OTHER (A fails). No single
	// rule grants both → MUST be invisible in every flag combination.
	for _, meta := range []bool{false, true} {
		for _, org := range []bool{false, true} {
			m.metadataScopeEnforce = meta
			m.orgScopeEnforce = org
			if m.ScopeAllowed(p, "db-x", "staging", "", []string{"ORG-OTHER"}) {
				t.Errorf("cross-rule leak: metaEnforce=%v orgEnforce=%v showed a tenant no single rule grants", meta, org)
			}
		}
	}
}

// ── #3 org-scope evaluation matrix ──────────────────────────────────────────

func TestScopeAllowed_OrgEvaluationMatrix(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "ops", Tenants: []string{"db-*"}, Permissions: []Permission{PermRead}, OrgScope: "org"},
	}}
	cases := []struct {
		name        string
		claimOrg    string   // caller's org claim ("" = none)
		tenantOrgs  []string // tenant org list (nil/empty = unlabeled)
		wantShadow  bool
		wantEnforce bool
	}{
		{"hit: caller org in tenant list", "ORG-A", []string{"ORG-A"}, true, true},
		{"miss: caller org not in list", "ORG-C", []string{"ORG-A"}, false, false},
		{"no caller org claim, labeled tenant", "", []string{"ORG-A"}, false, false},
		{"unlabeled tenant (empty list)", "ORG-A", []string{}, true, false},
		{"unlabeled tenant (nil list)", "ORG-A", nil, true, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var claims map[string]string
			if c.claimOrg != "" {
				claims = map[string]string{"org": c.claimOrg}
			}
			p := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: claims}

			shadowM := NewForTest(cfg)
			if got := shadowM.ScopeAllowed(p, "db-a", "", "", c.tenantOrgs); got != c.wantShadow {
				t.Errorf("shadow: ScopeAllowed = %v, want %v", got, c.wantShadow)
			}
			enforceM := NewForTest(cfg)
			enforceM.EnableOrgScopeEnforce()
			if got := enforceM.ScopeAllowed(p, "db-a", "", "", c.tenantOrgs); got != c.wantEnforce {
				t.Errorf("enforce: ScopeAllowed = %v, want %v", got, c.wantEnforce)
			}
		})
	}
}

// ── #4 per-axis would-deny independence, no double count ─────────────────────

func TestScopeAllowed_PerAxisWouldDenyIndependent(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "ops", Tenants: []string{"db-org"}, Permissions: []Permission{PermRead}, OrgScope: "org"},                                        // org-scoped only
		{Name: "ops", Tenants: []string{"db-meta"}, Permissions: []Permission{PermRead}, Environments: []string{"production"}},                  // metadata-scoped only
		{Name: "ops", Tenants: []string{"db-both"}, Permissions: []Permission{PermRead}, Environments: []string{"production"}, OrgScope: "org"}, // both axes
	}}
	p := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-A"}}

	t.Run("org gap records org only", func(t *testing.T) {
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		// unlabeled org (nil), env irrelevant (this rule's env is wildcard).
		if !m.ScopeAllowed(p, "db-org", "staging", "", nil) {
			t.Error("shadow: unlabeled-org tenant must stay visible")
		}
		if rec.counts[scopeAxisOrg] != 1 || rec.counts[scopeAxisMetadata] != 0 {
			t.Errorf("org gap: want org=1 metadata=0, got org=%d metadata=%d", rec.counts[scopeAxisOrg], rec.counts[scopeAxisMetadata])
		}
	})

	t.Run("metadata gap records metadata only", func(t *testing.T) {
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		// unlabeled env (""), org axis absent on this rule.
		if !m.ScopeAllowed(p, "db-meta", "", "", []string{"ORG-A"}) {
			t.Error("shadow: unlabeled-env tenant must stay visible")
		}
		if rec.counts[scopeAxisMetadata] != 1 || rec.counts[scopeAxisOrg] != 0 {
			t.Errorf("metadata gap: want metadata=1 org=0, got metadata=%d org=%d", rec.counts[scopeAxisMetadata], rec.counts[scopeAxisOrg])
		}
	})

	t.Run("both axes gap records each exactly once (no double count)", func(t *testing.T) {
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if !m.ScopeAllowed(p, "db-both", "", "", nil) { // unlabeled env AND unlabeled org
			t.Error("shadow: both-unlabeled tenant must stay visible")
		}
		if rec.counts[scopeAxisOrg] != 1 || rec.counts[scopeAxisMetadata] != 1 {
			t.Errorf("both gap: want org=1 metadata=1 (one each), got org=%d metadata=%d", rec.counts[scopeAxisOrg], rec.counts[scopeAxisMetadata])
		}
	})

	t.Run("labeled non-match records nothing (just denied)", func(t *testing.T) {
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		// labeled tenant, caller not a member → denied, but NOT a migration gap.
		if m.ScopeAllowed(p, "db-org", "staging", "", []string{"ORG-OTHER"}) {
			t.Error("labeled non-match must be invisible")
		}
		if rec.counts[scopeAxisOrg] != 0 || rec.counts[scopeAxisMetadata] != 0 {
			t.Errorf("labeled non-match: want no would-deny, got org=%d metadata=%d", rec.counts[scopeAxisOrg], rec.counts[scopeAxisMetadata])
		}
	})
}

// ── #5 admin / opt-out not locked out ───────────────────────────────────────

func TestScopeAllowed_AdminNotLockedOut(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "platform-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}}, // no org-scope
		{Name: "ops", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, OrgScope: "org"},
	}}
	// Cross-org admin: matches only the org-less admin rule, carries NO org claim.
	admin := &VerifiedPrincipal{Groups: []string{"platform-admins"}}

	for _, meta := range []bool{false, true} {
		for _, org := range []bool{false, true} {
			m := NewForTest(cfg)
			rec := newFakeScopeRecorder()
			m.SetScopeAuditor(rec)
			m.metadataScopeEnforce = meta
			m.orgScopeEnforce = org
			// Tenant belongs to some OTHER org; admin still sees it (its rule
			// does not opt into org-scope), even in full enforce.
			if !m.ScopeAllowed(admin, "db-x", "", "", []string{"ORG-X"}) {
				t.Errorf("metaEnforce=%v orgEnforce=%v: admin (no org-scope rule) must see any tenant", meta, org)
			}
			if rec.counts[scopeAxisOrg] != 0 {
				t.Errorf("metaEnforce=%v orgEnforce=%v: admin grant is not a would-deny, got org=%d", meta, org, rec.counts[scopeAxisOrg])
			}
		}
	}
}

// ── #1 + degeneration: ScopeAllowed with no org-scoped rule == MetadataAllowed ─

// TestScopeAllowed_DegeneratesToMetadata pins the byte-identical degeneration:
// with no org-scoped rule the org axis is (true,true) on every rule, so the
// effective decision equals the pre-P4 MetadataAllowed across the full matrix,
// AND the org would-deny counter never increments (org-would-deny ≡ false).
// tenantOrgs is deliberately non-nil to prove the org list is IGNORED when no
// rule opts in.
func TestScopeAllowed_DegeneratesToMetadata(t *testing.T) {
	t.Parallel()
	metaCases := []struct{ env, domain string }{
		{"", ""}, {"production", "finance"}, {"production", ""}, {"staging", "finance"}, {"production", "ecommerce"},
	}
	oracle := NewForTest(equivConfig()) // no recorder → MetadataAllowed as truth
	scoped := NewForTest(equivConfig())
	rec := newFakeScopeRecorder()
	scoped.SetScopeAuditor(rec)

	for _, metaEnforce := range []bool{false, true} {
		oracle.metadataScopeEnforce = metaEnforce
		scoped.metadataScopeEnforce = metaEnforce
		for _, orgEnforce := range []bool{false, true} {
			scoped.orgScopeEnforce = orgEnforce // must not matter: no org rule
			for gName, groups := range equivGroupSets() {
				p := &VerifiedPrincipal{Groups: groups}
				for _, tenant := range equivTenants {
					for _, mc := range metaCases {
						want := oracle.MetadataAllowed(p, tenant, mc.env, mc.domain)
						got := scoped.ScopeAllowed(p, tenant, mc.env, mc.domain, []string{"ORG-Z"})
						if got != want {
							t.Errorf("[groups=%s tenant=%q env=%q dom=%q metaEnforce=%v orgEnforce=%v] ScopeAllowed=%v, MetadataAllowed=%v",
								gName, tenant, mc.env, mc.domain, metaEnforce, orgEnforce, got, want)
						}
					}
				}
			}
		}
	}
	if rec.counts[scopeAxisOrg] != 0 {
		t.Errorf("degeneration: org would-deny must stay 0 with no org-scoped rule, got %d", rec.counts[scopeAxisOrg])
	}
}

// MetadataAllowed is the org-less thin wrapper: it must equal ScopeAllowed with
// nil orgs for every input (the two entry points cannot drift).
func TestMetadataAllowed_EqualsScopeAllowedNilOrgs(t *testing.T) {
	t.Parallel()
	m := NewForTest(equivConfig())
	for _, enforce := range []bool{false, true} {
		m.metadataScopeEnforce = enforce
		for _, groups := range equivGroupSets() {
			p := &VerifiedPrincipal{Groups: groups}
			for _, tenant := range equivTenants {
				for _, mc := range []struct{ env, dom string }{{"", ""}, {"production", "finance"}, {"staging", ""}} {
					if a, b := m.MetadataAllowed(p, tenant, mc.env, mc.dom), m.ScopeAllowed(p, tenant, mc.env, mc.dom, nil); a != b {
						t.Errorf("MetadataAllowed=%v != ScopeAllowed(nil orgs)=%v (tenant=%q env=%q dom=%q enforce=%v)",
							a, b, tenant, mc.env, mc.dom, enforce)
					}
				}
			}
		}
	}
}

// ── #6 validateConfig: org-scope key must be a declared claim header ─────────

func TestNewManager_OrgScopeValidation(t *testing.T) {
	t.Parallel()
	declared := map[string]string{"org": "X-Auth-Request-Org"}

	orgScopedYAML := "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n    org-scope: org\n"
	// org-scope on a LEGACY name-matched rule (no match block) — proves the
	// check runs regardless of the match block.
	orgScopedLegacyYAML := orgScopedYAML

	t.Run("undeclared org-scope key is a load error", func(t *testing.T) {
		t.Parallel()
		_, path := testutil.MkTempYAML(t, "_rbac.yaml", orgScopedLegacyYAML)
		_, err := NewManager(path, nil) // "org" NOT declared
		if err == nil || !strings.Contains(err.Error(), "not declared") {
			t.Errorf("NewManager = %v, want error containing \"not declared\"", err)
		}
	})

	t.Run("declared org-scope key loads and evaluates", func(t *testing.T) {
		t.Parallel()
		_, path := testutil.MkTempYAML(t, "_rbac.yaml", orgScopedYAML)
		m, err := NewManager(path, declared)
		if err != nil {
			t.Fatalf("NewManager: %v", err)
		}
		p := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-A"}}
		if !m.ScopeAllowed(p, "db-a", "", "", []string{"ORG-A"}) {
			t.Error("declared org-scope: same-org tenant must be visible")
		}
	})

	t.Run("empty org-scope (omitted) is not checked", func(t *testing.T) {
		t.Parallel()
		_, path := testutil.MkTempYAML(t, "_rbac.yaml", "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n")
		if _, err := NewManager(path, nil); err != nil {
			t.Errorf("rule without org-scope must load with no claim headers, got %v", err)
		}
	})

	t.Run("strict parse rejects an org-scope typo key", func(t *testing.T) {
		t.Parallel()
		_, path := testutil.MkTempYAML(t, "_rbac.yaml", "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n    org-scop: org\n")
		if _, err := NewManager(path, declared); err == nil {
			t.Error("NewManager must reject the unknown field org-scop (strict KnownFields)")
		}
	})

	t.Run("hot-reload to an undeclared org-scope keeps last-good", func(t *testing.T) {
		t.Parallel()
		dir, path := testutil.MkTempYAML(t, "_rbac.yaml", orgScopedYAML)
		_ = dir
		m, err := NewManager(path, declared)
		if err != nil {
			t.Fatalf("NewManager: %v", err)
		}
		p := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-A"}}
		if !m.ScopeAllowed(p, "db-a", "", "", []string{"ORG-A"}) {
			t.Fatal("precondition: initial config must grant the same-org tenant")
		}
		// Rewrite with an org-scope on an undeclared key → reload rejected.
		if err := os.WriteFile(path, []byte("groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n    org-scope: region\n"), 0o600); err != nil {
			t.Fatalf("write bad config: %v", err)
		}
		if err := m.Reload(); err == nil {
			t.Fatal("Reload = nil, want an error for the undeclared org-scope key")
		}
		if !m.ScopeAllowed(p, "db-a", "", "", []string{"ORG-A"}) {
			t.Error("after failed reload: last-good must still grant the tenant")
		}
	})
}
