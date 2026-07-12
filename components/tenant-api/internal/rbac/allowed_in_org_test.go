package rbac

// ADR-027 / LD-6 P4b — org-scope on the WRITE plane (AllowedInOrg).
//
// These tests pin the write-plane authorization core: the org axis is folded
// into the SAME per-rule loop as the permission check (never a top-level AND —
// T4), Allowed stays the byte-identical org-blind degeneration (T3, plus the
// full allowed_equiv_test matrix), the org axis only ever narrows a grant
// (T2 monotonicity), and would-deny observations land on the org_write axis
// from AllowedInOrg ONLY (T5 — Allowed and the list-plane ScopeAllowed must
// never pollute the write-plane soak signal). T7's rbac half proves the gate
// consumes orgs resolved AT DECISION TIME (a swapped tenantorg snapshot flips
// the decision), backing the "resolve inside the execution loop, never at
// submit time" rule for batch handlers.
//
// fakeScopeRecorder / newFakeScopeRecorder live in rbac_scope_mode_test.go;
// equivConfig / equivGroupSets / equivTenants live in allowed_equiv_test.go.

import (
	"testing"

	"github.com/vencil/tenant-api/internal/tenantorg"
)

// orgWriteCfg is the minimal write-plane fixture: one org-scoped rule granting
// write (⊇ read) on a tenant prefix. Admin is deliberately NOT granted so the
// matrix can also pin that org membership never widens a permission grant.
func orgWriteCfg() *RBACConfig {
	return &RBACConfig{Groups: []GroupRule{
		{Name: "ops", Tenants: []string{"db-*"}, Permissions: []Permission{PermWrite}, OrgScope: "org"},
	}}
}

// ── T1: AllowedInOrg evaluation matrix ───────────────────────────────────────

func TestAllowedInOrg_Matrix(t *testing.T) {
	t.Parallel()
	member := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-A"}}
	nonMember := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-C"}}
	noClaim := &VerifiedPrincipal{Groups: []string{"ops"}}

	cases := []struct {
		name       string
		p          *VerifiedPrincipal
		tenantOrgs []string
		wantShadow bool // expected for read AND write (the rule grants write ⊇ read)
		wantWrite  bool // expected under enforce
	}{
		{"member on labeled tenant", member, []string{"ORG-A", "ORG-B"}, true, true},
		{"non-member on labeled tenant", nonMember, []string{"ORG-A"}, false, false},
		{"member claim, unlabeled tenant (nil)", member, nil, true, false},
		{"member claim, unlabeled tenant (empty)", member, []string{}, true, false},
		{"no org claim, labeled tenant", noClaim, []string{"ORG-A"}, false, false},
		{"no org claim, unlabeled tenant", noClaim, nil, true, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			for _, want := range []Permission{PermRead, PermWrite} {
				shadowM := NewForTest(orgWriteCfg())
				if got := shadowM.AllowedInOrg(c.p, "db-a", want, c.tenantOrgs); got != c.wantShadow {
					t.Errorf("shadow: AllowedInOrg(%s) = %v, want %v", want, got, c.wantShadow)
				}
				enforceM := NewForTest(orgWriteCfg())
				enforceM.EnableOrgScopeEnforce()
				if got := enforceM.AllowedInOrg(c.p, "db-a", want, c.tenantOrgs); got != c.wantWrite {
					t.Errorf("enforce: AllowedInOrg(%s) = %v, want %v", want, got, c.wantWrite)
				}
			}
			// Admin exceeds the rule's write grant — denied in EVERY mode and org
			// combination (org membership must never widen a permission grant).
			for _, enforce := range []bool{false, true} {
				m := NewForTest(orgWriteCfg())
				if enforce {
					m.EnableOrgScopeEnforce()
				}
				if m.AllowedInOrg(c.p, "db-a", PermAdmin, c.tenantOrgs) {
					t.Errorf("enforce=%v: AllowedInOrg(admin) must be false (rule grants write only)", enforce)
				}
			}
		})
	}
}

// ── T2: monotonicity — AllowedInOrg(enforce) ⟹ AllowedInOrg(shadow) ⟹ Allowed ─

// TestAllowedInOrg_Monotonic sweeps a mixed fixture (org-scoped, plain, and
// admin rules × principals × tenants × org lists × permissions × manager
// modes) and asserts the two implications hold everywhere: the org axis only
// ever NARROWS a grant. A violation means enforce granted something shadow
// denies (swapped returns) or the org axis widened access past the org-blind
// Allowed.
func TestAllowedInOrg_Monotonic(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "platform-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
		{Name: "org-ops", Tenants: []string{"db-*"}, Permissions: []Permission{PermRead, PermWrite}, OrgScope: "org"},
		{Name: "viewers", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
	}}
	principals := map[string]*VerifiedPrincipal{
		"nil":            nil,
		"admin":          {Groups: []string{"platform-admins"}},
		"org-member":     {Groups: []string{"org-ops"}, Claims: map[string]string{"org": "ORG-A"}},
		"org-no-claim":   {Groups: []string{"org-ops"}},
		"viewer":         {Groups: []string{"viewers"}},
		"viewer+org-ops": {Groups: []string{"viewers", "org-ops"}, Claims: map[string]string{"org": "ORG-A"}},
	}
	orgLists := map[string][]string{
		"nil":    nil,
		"empty":  {},
		"member": {"ORG-A"},
		"other":  {"ORG-B"},
		"multi":  {"ORG-B", "ORG-A"},
	}
	managers := func() map[string][2]*Manager { // [shadow, enforce] per mode
		build := func(cfg *RBACConfig, failClosed bool) [2]*Manager {
			s, e := NewForTest(cfg), NewForTest(cfg)
			s.failClosedOnEmpty, e.failClosedOnEmpty = failClosed, failClosed
			e.EnableOrgScopeEnforce()
			return [2]*Manager{s, e}
		}
		return map[string][2]*Manager{
			"configured":  build(cfg, false),
			"open":        build(&RBACConfig{}, false),
			"fail-closed": build(&RBACConfig{}, true),
		}
	}()
	for mode, pair := range managers {
		shadowM, enforceM := pair[0], pair[1]
		for pName, p := range principals {
			for _, tenant := range []string{"db-a", "redis-01", "*"} {
				for oName, orgs := range orgLists {
					for _, want := range []Permission{PermRead, PermWrite, PermAdmin} {
						e := enforceM.AllowedInOrg(p, tenant, want, orgs)
						s := shadowM.AllowedInOrg(p, tenant, want, orgs)
						a := shadowM.Allowed(p, tenant, want)
						if e && !s {
							t.Errorf("[%s p=%s tenant=%q orgs=%s want=%s] enforce=true but shadow=false (org axis must only narrow)",
								mode, pName, tenant, oName, want)
						}
						if s && !a {
							t.Errorf("[%s p=%s tenant=%q orgs=%s want=%s] shadow=true but Allowed=false (org axis must only narrow)",
								mode, pName, tenant, oName, want)
						}
					}
				}
			}
		}
	}
}

// ── T3: Allowed stays org-blind under an org-scoped rule ─────────────────────

// The full byte-identity pin is the allowed_equiv_test matrix (Allowed vs the
// legacy HasPermission across every mode). This adds the P4b-explicit case:
// even with enforce ON and the caller OUTSIDE the tenant's org, Allowed still
// grants — it is the org-blind entry point by contract (platform "*" checks
// and read-plane filtering); only AllowedInOrg sees the org axis.
func TestAllowed_OrgBlindUnderOrgScopedRule(t *testing.T) {
	t.Parallel()
	m := NewForTest(orgWriteCfg())
	m.EnableOrgScopeEnforce()
	rec := newFakeScopeRecorder()
	m.SetScopeAuditor(rec)
	outsider := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-C"}}

	if !m.Allowed(outsider, "db-a", PermWrite) {
		t.Error("Allowed must stay org-blind (grant) even under enforce with a non-member caller")
	}
	if m.AllowedInOrg(outsider, "db-a", PermWrite, []string{"ORG-A"}) {
		t.Error("AllowedInOrg must deny the same non-member caller (labeled tenant, enforce)")
	}
	if rec.counts[scopeAxisOrgWrite] != 0 {
		t.Errorf("org-blind Allowed + labeled non-match must record no org_write would-deny, got %d", rec.counts[scopeAxisOrgWrite])
	}
}

// ── T4: write-plane cross-rule union non-leak (correctness core) ─────────────

// TestAllowedInOrg_CrossRuleUnionNoLeak proves the org axis is folded per-rule
// on the write plane. Rule A grants write but fails org for this tenant; rule
// B passes org (no org restriction) but only grants read. A top-level
// Allowed(write, from A) && orgPasses(from B) would grant the write — but no
// SINGLE rule grants it, so the per-rule fold must deny in BOTH modes.
func TestAllowedInOrg_CrossRuleUnionNoLeak(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		// Rule A: write, org-scoped.
		{Name: "org-writers", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, OrgScope: "org"},
		// Rule B: read only, NO org restriction.
		{Name: "viewers", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
	}}
	p := &VerifiedPrincipal{Groups: []string{"org-writers", "viewers"}, Claims: map[string]string{"org": "ORG-USER"}}

	for _, enforce := range []bool{false, true} {
		m := NewForTest(cfg)
		if enforce {
			m.EnableOrgScopeEnforce()
		}
		// Leak case: tenant labeled with a DIFFERENT org → A fails org in both
		// modes (labeled non-match), B never grants write. MUST deny.
		if m.AllowedInOrg(p, "db-x", PermWrite, []string{"ORG-OTHER"}) {
			t.Errorf("enforce=%v: cross-rule leak — write granted though no single rule grants it", enforce)
		}
		// Control 1: A alone grants when the org matches (proves A is live).
		if !m.AllowedInOrg(p, "db-x", PermWrite, []string{"ORG-USER"}) {
			t.Errorf("enforce=%v: control — same-org write via rule A must be granted", enforce)
		}
		// Control 2: read still flows through org-less rule B regardless of org
		// (proves B is live and the leak-case denial is genuinely cross-rule).
		if !m.AllowedInOrg(p, "db-x", PermRead, []string{"ORG-OTHER"}) {
			t.Errorf("enforce=%v: control — read via org-less rule B must be granted", enforce)
		}
	}
}

// ── T5: would-deny lands on org_write, from AllowedInOrg ONLY ────────────────

func TestAllowedInOrg_WouldDenyRecording(t *testing.T) {
	t.Parallel()
	member := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-A"}}

	t.Run("shadow: unlabeled tenant allows and records org_write once", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(orgWriteCfg())
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if !m.AllowedInOrg(member, "db-a", PermWrite, nil) {
			t.Error("shadow: unlabeled tenant must still be granted")
		}
		if rec.counts[scopeAxisOrgWrite] != 1 || rec.counts[scopeAxisOrg] != 0 || rec.counts[scopeAxisMetadata] != 0 {
			t.Errorf("want org_write=1 org=0 metadata=0, got org_write=%d org=%d metadata=%d",
				rec.counts[scopeAxisOrgWrite], rec.counts[scopeAxisOrg], rec.counts[scopeAxisMetadata])
		}
	})

	t.Run("enforce: unlabeled tenant denies and still records org_write", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(orgWriteCfg())
		m.EnableOrgScopeEnforce()
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.AllowedInOrg(member, "db-a", PermWrite, nil) {
			t.Error("enforce: unlabeled tenant must be denied")
		}
		if rec.counts[scopeAxisOrgWrite] != 1 {
			t.Errorf("enforce keeps counting the denied-by-scope signal: want org_write=1, got %d", rec.counts[scopeAxisOrgWrite])
		}
	})

	t.Run("Allowed never records org_write (read-plane volume must not pollute the soak)", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(orgWriteCfg())
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if !m.Allowed(member, "db-a", PermWrite) {
			t.Error("Allowed (org-blind) must grant")
		}
		if len(rec.counts) != 0 {
			t.Errorf("Allowed must record nothing on any axis, got %v", rec.counts)
		}
	})

	t.Run("labeled non-member: denied, but not a migration gap", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(orgWriteCfg())
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.AllowedInOrg(member, "db-a", PermWrite, []string{"ORG-OTHER"}) {
			t.Error("labeled non-member must be denied even in shadow")
		}
		if rec.counts[scopeAxisOrgWrite] != 0 {
			t.Errorf("labeled non-match is denied in BOTH modes → no would-deny, got %d", rec.counts[scopeAxisOrgWrite])
		}
	})

	t.Run("list-plane ScopeAllowed still records org, never org_write", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(orgWriteCfg())
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if !m.ScopeAllowed(member, "db-a", "", "", nil) {
			t.Error("shadow list plane: unlabeled tenant must stay visible")
		}
		if rec.counts[scopeAxisOrg] != 1 || rec.counts[scopeAxisOrgWrite] != 0 {
			t.Errorf("list plane: want org=1 org_write=0, got org=%d org_write=%d",
				rec.counts[scopeAxisOrg], rec.counts[scopeAxisOrgWrite])
		}
	})
}

// ── T7 (rbac half): orgs are consumed as resolved AT DECISION TIME ───────────

// TestAllowedInOrg_TenantorgSnapshotSwap simulates the executeBatchOps pattern:
// the caller resolves the tenant's orgs from the live tenantorg snapshot
// immediately before each AllowedInOrg call. Swapping the snapshot between
// calls flips the decision — proving the gate carries no stale org state of
// its own and pinning the "resolve inside the execution loop, never at submit
// time" rule the P4b batch handlers rely on (the handler half of T7 drives
// executeBatchOps itself in Phase C).
func TestAllowedInOrg_TenantorgSnapshotSwap(t *testing.T) {
	t.Parallel()
	m := NewForTest(orgWriteCfg())
	m.EnableOrgScopeEnforce()
	p := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-A"}}

	torg := tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{
		"db-a": {"ORG-OTHER"}, // mapping A: tenant belongs to someone else's org
	}})
	orgs, _ := torg.OrgsForTenant("db-a")
	if m.AllowedInOrg(p, "db-a", PermWrite, orgs) {
		t.Fatal("mapping A: non-member write must be denied under enforce")
	}

	// Hot-swap the tenantorg snapshot (models a _tenant_orgs.yaml reload while
	// an async batch is executing) and re-resolve at decision time.
	torg.Override(&tenantorg.Config{TenantOrgs: map[string][]string{
		"db-a": {"ORG-A"},
	}})
	orgs, _ = torg.OrgsForTenant("db-a")
	if !m.AllowedInOrg(p, "db-a", PermWrite, orgs) {
		t.Error("after snapshot swap: member write must be granted (orgs resolved at decision time)")
	}
}
