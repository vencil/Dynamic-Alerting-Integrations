package rbac

// ADR-027 / LD-6 P4 — the org-scope authorization axis, both planes.
//
// List plane (ScopeAllowed): the org axis is folded into the SAME per-rule
// loop as the metadata axis (never a top-level AND, which would leak
// cross-rule access), the four (metadata,org) visibility booleans select the
// effective decision + per-axis would-deny signals, and a config with no
// org-scoped rule degenerates BYTE-IDENTICALLY to the pre-P4 metadata filter.
//
// Write plane (AllowedInOrg, P4b): the org axis is folded into the SAME
// per-rule loop as the permission check (never a top-level AND — T4), Allowed
// stays the byte-identical org-blind degeneration (T3, plus the full
// legacy_equiv_test matrix), the org axis only ever narrows a grant (T2
// monotonicity), and would-deny observations land on the org_write axis from
// AllowedInOrg ONLY (T5 — Allowed and the list-plane ScopeAllowed must never
// pollute the write-plane soak signal). T7's rbac half proves the gate
// consumes orgs resolved AT DECISION TIME (a swapped tenantorg snapshot flips
// the decision), backing the "resolve inside the execution loop, never at
// submit time" rule for batch handlers.
//
// fakeScopeRecorder / newFakeScopeRecorder and equivConfig / equivGroupSets /
// equivTenants live in testhelpers_test.go. The org-scope claim-key LOAD
// validation lives in config_load_test.go (TestNewManager_OrgScopeValidation)
// and config_reload_test.go (the "undeclared org-scope key" reload row).

import (
	"testing"

	"github.com/vencil/tenant-api/internal/tenantorg"
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

// ── write plane (ADR-027 / LD-6 P4b) ─────────────────────────────────────────

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

// The full byte-identity pin is the legacy_equiv_test matrix (Allowed vs the
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
