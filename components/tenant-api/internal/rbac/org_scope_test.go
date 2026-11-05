package rbac

// ADR-027 / LD-6 P4 — the org-scope authorization axis, both planes, under
// ADR-027 §7 (every axis enforced; no shadow mode).
//
// List plane (ScopeAllowed): the org axis is folded into the SAME per-rule
// loop as the metadata axis (never a top-level AND, which would leak
// cross-rule access), and a config with no org-scoped rule degenerates to the
// metadata filter.
//
// Write plane (AllowedInOrg, P4b): the org axis is folded into the SAME
// per-rule loop as the permission check (never a top-level AND — T4), Allowed
// stays the scope-blind entry point (T3, plus the full legacy_equiv_test
// matrix), the scope axes only ever narrow a grant (T2 monotonicity), and
// scope-denied observations land on the org_write axis from AllowedInOrg ONLY
// (T5 — Allowed and the list-plane ScopeAllowed never record on the
// write-plane series). T7's rbac half proves the gate consumes orgs resolved
// AT DECISION TIME (a swapped tenantorg snapshot flips the decision), backing
// the "resolve inside the execution loop, never at submit time" rule for batch
// handlers.
//
// fakeScopeRecorder / newFakeScopeRecorder and equivConfig / equivGroupSets /
// equivTenants live in testhelpers_test.go. The org-scope claim-key LOAD
// validation lives in config_load_test.go (TestNewManager_OrgScopeValidation)
// and config_reload_test.go (the "undeclared org-scope key" reload row).

import (
	"testing"

	"github.com/vencil/tenant-api/internal/tenantorg"
)

// ── pure scopeSetPasses (the org analogue of scopeFieldPasses) ──────────────

func TestScopeSetPasses(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name       string
		userOrgVal string
		tenantOrgs []string
		want       bool
	}{
		{"unlabeled (nil) fails", "ORG-A", nil, false},
		{"unlabeled (empty slice) fails", "ORG-A", []string{}, false},
		{"unlabeled + no caller claim fails", "", nil, false},
		{"labeled member passes", "ORG-A", []string{"ORG-A", "ORG-B"}, true},
		{"labeled non-member fails", "ORG-C", []string{"ORG-A", "ORG-B"}, false},
		{"labeled + no caller claim fails", "", []string{"ORG-A"}, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := scopeSetPasses(c.userOrgVal, c.tenantOrgs); got != c.want {
				t.Errorf("scopeSetPasses(%q, %v) = %v, want %v", c.userOrgVal, c.tenantOrgs, got, c.want)
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
	// rule grants both → MUST be invisible, and each axis that cut a rule is
	// counted once.
	rec := newFakeScopeRecorder()
	m.SetScopeAuditor(rec)
	if m.ScopeAllowed(p, "db-x", "staging", "", []string{"ORG-OTHER"}) {
		t.Error("cross-rule leak: showed a tenant no single rule grants")
	}
	if rec.counts[scopeAxisMetadata] != 1 || rec.counts[scopeAxisOrg] != 1 {
		t.Errorf("leak case: want metadata=1 org=1 (rule B cut by metadata, rule A by org), got %v", rec.counts)
	}
}

// ── #3 org-scope evaluation matrix ──────────────────────────────────────────

func TestScopeAllowed_OrgEvaluationMatrix(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "ops", Tenants: []string{"db-*"}, Permissions: []Permission{PermRead}, OrgScope: "org"},
	}}
	cases := []struct {
		name       string
		claimOrg   string   // caller's org claim ("" = none)
		tenantOrgs []string // tenant org list (nil/empty = unlabeled)
		want       bool
	}{
		{"hit: caller org in tenant list", "ORG-A", []string{"ORG-A"}, true},
		{"miss: caller org not in list", "ORG-C", []string{"ORG-A"}, false},
		{"no caller org claim, labeled tenant", "", []string{"ORG-A"}, false},
		{"unlabeled tenant (empty list)", "ORG-A", []string{}, false},
		{"unlabeled tenant (nil list)", "ORG-A", nil, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var claims map[string]string
			if c.claimOrg != "" {
				claims = map[string]string{"org": c.claimOrg}
			}
			p := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: claims}

			m := NewForTest(cfg)
			rec := newFakeScopeRecorder()
			m.SetScopeAuditor(rec)
			if got := m.ScopeAllowed(p, "db-org1", "", "", c.tenantOrgs); got != c.want {
				t.Errorf("ScopeAllowed = %v, want %v", got, c.want)
			}
			// Every denial here is the org axis cutting the one matching rule.
			wantOrg := 0
			if !c.want {
				wantOrg = 1
			}
			if rec.counts[scopeAxisOrg] != wantOrg || rec.counts[scopeAxisMetadata] != 0 {
				t.Errorf("counts = %v, want org=%d metadata=0", rec.counts, wantOrg)
			}
		})
	}
}

// ── #4 per-axis scope-denied independence, no double count ──────────────────

func TestScopeAllowed_PerAxisScopeDeniedIndependent(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "ops", Tenants: []string{"db-org"}, Permissions: []Permission{PermRead}, OrgScope: "org"},                                        // org-scoped only
		{Name: "ops", Tenants: []string{"db-meta"}, Permissions: []Permission{PermRead}, Environments: []string{"production"}},                  // metadata-scoped only
		{Name: "ops", Tenants: []string{"db-both"}, Permissions: []Permission{PermRead}, Environments: []string{"production"}, OrgScope: "org"}, // both axes
	}}
	p := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-A"}}

	t.Run("unlabeled org denies and records org only", func(t *testing.T) {
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		// unlabeled org (nil), env irrelevant (this rule's env is wildcard).
		if m.ScopeAllowed(p, "db-org", "staging", "", nil) {
			t.Error("unlabeled-org tenant must be hidden")
		}
		if rec.counts[scopeAxisOrg] != 1 || rec.counts[scopeAxisMetadata] != 0 {
			t.Errorf("org denial: want org=1 metadata=0, got org=%d metadata=%d", rec.counts[scopeAxisOrg], rec.counts[scopeAxisMetadata])
		}
	})

	t.Run("unlabeled env denies and records metadata only", func(t *testing.T) {
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		// unlabeled env (""), org axis absent on this rule.
		if m.ScopeAllowed(p, "db-meta", "", "", []string{"ORG-A"}) {
			t.Error("unlabeled-env tenant must be hidden")
		}
		if rec.counts[scopeAxisMetadata] != 1 || rec.counts[scopeAxisOrg] != 0 {
			t.Errorf("metadata denial: want metadata=1 org=0, got metadata=%d org=%d", rec.counts[scopeAxisMetadata], rec.counts[scopeAxisOrg])
		}
	})

	t.Run("both axes deny: each recorded exactly once (no double count)", func(t *testing.T) {
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.ScopeAllowed(p, "db-both", "", "", nil) { // unlabeled env AND unlabeled org
			t.Error("both-unlabeled tenant must be hidden")
		}
		if rec.counts[scopeAxisOrg] != 1 || rec.counts[scopeAxisMetadata] != 1 {
			t.Errorf("both denial: want org=1 metadata=1 (one each), got org=%d metadata=%d", rec.counts[scopeAxisOrg], rec.counts[scopeAxisMetadata])
		}
	})

	t.Run("labeled non-match denies and records org", func(t *testing.T) {
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		// labeled tenant, caller not a member → denied by the org axis.
		if m.ScopeAllowed(p, "db-org", "staging", "", []string{"ORG-OTHER"}) {
			t.Error("labeled non-match must be invisible")
		}
		if rec.counts[scopeAxisOrg] != 1 || rec.counts[scopeAxisMetadata] != 0 {
			t.Errorf("labeled non-match: want org=1 metadata=0, got org=%d metadata=%d", rec.counts[scopeAxisOrg], rec.counts[scopeAxisMetadata])
		}
	})

	t.Run("labeled in-scope tenant visible, records nothing", func(t *testing.T) {
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if !m.ScopeAllowed(p, "db-both", "production", "", []string{"ORG-A"}) {
			t.Error("tenant in scope on both axes must be visible")
		}
		if len(rec.counts) != 0 {
			t.Errorf("an allowed decision must record nothing, got %v", rec.counts)
		}
	})

	t.Run("tenant no rule names records nothing", func(t *testing.T) {
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.ScopeAllowed(p, "db-unnamed", "", "", nil) {
			t.Error("tenant outside every pattern must be hidden")
		}
		if len(rec.counts) != 0 {
			t.Errorf("a denial no rule would have granted must record nothing, got %v", rec.counts)
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

	m := NewForTest(cfg)
	rec := newFakeScopeRecorder()
	m.SetScopeAuditor(rec)
	// Tenant belongs to some OTHER org, or none; admin still sees it (its rule
	// does not opt into org-scope).
	for _, orgs := range [][]string{{"ORG-X"}, nil} {
		if !m.ScopeAllowed(admin, "db-x", "", "", orgs) {
			t.Errorf("orgs=%v: admin (no org-scope rule) must see any tenant", orgs)
		}
		if !m.AllowedInOrg(admin, "db-x", PermAdmin, orgs, "", "") {
			t.Errorf("orgs=%v: admin (no org-scope rule) must administer any tenant", orgs)
		}
	}
	if len(rec.counts) != 0 {
		t.Errorf("admin grants are not scope denials, got %v", rec.counts)
	}
}

// ── #1 + degeneration: ScopeAllowed with no org-scoped rule == MetadataAllowed ─

// TestScopeAllowed_DegeneratesToMetadata pins the degeneration: with no
// org-scoped rule the org axis restricts no rule, so the decision equals
// MetadataAllowed across the full matrix, AND the org scope-denied counter
// never increments. tenantOrgs is deliberately non-nil to prove the org list is
// IGNORED when no rule opts in.
func TestScopeAllowed_DegeneratesToMetadata(t *testing.T) {
	t.Parallel()
	metaCases := []struct{ env, domain string }{
		{"", ""}, {"production", "finance"}, {"production", ""}, {"staging", "finance"}, {"production", "ecommerce"},
	}
	oracle := NewForTest(equivConfig()) // no recorder → MetadataAllowed as truth
	scoped := NewForTest(equivConfig())
	rec := newFakeScopeRecorder()
	scoped.SetScopeAuditor(rec)

	for gName, groups := range equivGroupSets() {
		p := &VerifiedPrincipal{Groups: groups}
		for _, tenant := range equivTenants {
			for _, mc := range metaCases {
				want := oracle.MetadataAllowed(p, tenant, mc.env, mc.domain)
				got := scoped.ScopeAllowed(p, tenant, mc.env, mc.domain, []string{"ORG-Z"})
				if got != want {
					t.Errorf("[groups=%s tenant=%q env=%q dom=%q] ScopeAllowed=%v, MetadataAllowed=%v",
						gName, tenant, mc.env, mc.domain, got, want)
				}
			}
		}
	}
	if rec.counts[scopeAxisOrg] != 0 {
		t.Errorf("degeneration: org scope-denied must stay 0 with no org-scoped rule, got %d", rec.counts[scopeAxisOrg])
	}
}

// MetadataAllowed is the org-less thin wrapper: it must equal ScopeAllowed with
// nil orgs for every input (the two entry points cannot drift).
func TestMetadataAllowed_EqualsScopeAllowedNilOrgs(t *testing.T) {
	t.Parallel()
	m := NewForTest(equivConfig())
	for _, groups := range equivGroupSets() {
		p := &VerifiedPrincipal{Groups: groups}
		for _, tenant := range equivTenants {
			for _, mc := range []struct{ env, dom string }{{"", ""}, {"production", "finance"}, {"staging", ""}} {
				if a, b := m.MetadataAllowed(p, tenant, mc.env, mc.dom), m.ScopeAllowed(p, tenant, mc.env, mc.dom, nil); a != b {
					t.Errorf("MetadataAllowed=%v != ScopeAllowed(nil orgs)=%v (tenant=%q env=%q dom=%q)",
						a, b, tenant, mc.env, mc.dom)
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
		want       bool // expected for read AND write (the rule grants write ⊇ read)
	}{
		{"member on labeled tenant", member, []string{"ORG-A", "ORG-B"}, true},
		{"non-member on labeled tenant", nonMember, []string{"ORG-A"}, false},
		{"member claim, unlabeled tenant (nil)", member, nil, false},
		{"member claim, unlabeled tenant (empty)", member, []string{}, false},
		{"no org claim, labeled tenant", noClaim, []string{"ORG-A"}, false},
		{"no org claim, unlabeled tenant", noClaim, nil, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			for _, want := range []Permission{PermRead, PermWrite} {
				m := NewForTest(orgWriteCfg())
				rec := newFakeScopeRecorder()
				m.SetScopeAuditor(rec)
				if got := m.AllowedInOrg(c.p, "db-a", want, c.tenantOrgs, "", ""); got != c.want {
					t.Errorf("AllowedInOrg(%s) = %v, want %v", want, got, c.want)
				}
				wantOrgWrite := 0
				if !c.want {
					wantOrgWrite = 1 // the only matching rule grants the perm; org cut it
				}
				if rec.counts[scopeAxisOrgWrite] != wantOrgWrite || rec.counts[scopeAxisMetadataWrite] != 0 {
					t.Errorf("AllowedInOrg(%s) counts = %v, want org_write=%d metadata_write=0", want, rec.counts, wantOrgWrite)
				}
			}
			// Admin exceeds the rule's write grant — denied whatever the org
			// (org membership must never widen a permission grant), and the
			// denial is the permission check's, not a scope axis's.
			m := NewForTest(orgWriteCfg())
			rec := newFakeScopeRecorder()
			m.SetScopeAuditor(rec)
			if m.AllowedInOrg(c.p, "db-a", PermAdmin, c.tenantOrgs, "", "") {
				t.Error("AllowedInOrg(admin) must be false (rule grants write only)")
			}
			if len(rec.counts) != 0 {
				t.Errorf("AllowedInOrg(admin) denial attributed to a scope axis: %v", rec.counts)
			}
		})
	}
}

// ── T2: monotonicity — AllowedInOrg ⟹ Allowed ────────────────────────────────

// TestAllowedInOrg_Monotonic sweeps a mixed fixture (org-scoped,
// metadata-scoped, plain, and admin rules × principals × tenants × org lists
// × metadata × permissions, plus the empty rule set) and asserts the scope
// axes only ever NARROW a grant: AllowedInOrg ⟹ Allowed, and likewise
// AllowedInOrgRead ⟹ Allowed. A violation means a scope axis widened access
// past the scope-blind Allowed.
func TestAllowedInOrg_Monotonic(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "platform-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
		{Name: "org-ops", Tenants: []string{"db-*"}, Permissions: []Permission{PermRead, PermWrite}, OrgScope: "org"},
		{Name: "prod-ops", Tenants: []string{"db-*"}, Permissions: []Permission{PermWrite}, Environments: []string{"production"}},
		{Name: "viewers", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
	}}
	principals := map[string]*VerifiedPrincipal{
		"nil":            nil,
		"admin":          {Groups: []string{"platform-admins"}},
		"org-member":     {Groups: []string{"org-ops"}, Claims: map[string]string{"org": "ORG-A"}},
		"org-no-claim":   {Groups: []string{"org-ops"}},
		"prod-ops":       {Groups: []string{"prod-ops"}},
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
	managers := map[string]*Manager{
		"configured": NewForTest(cfg),
		"empty":      NewForTest(&RBACConfig{}),
	}
	for mode, m := range managers {
		for pName, p := range principals {
			for _, tenant := range []string{"db-mono", "redis-01", "*"} {
				for oName, orgs := range orgLists {
					for _, env := range []string{"", "production", "dev"} {
						for _, want := range []Permission{PermRead, PermWrite, PermAdmin} {
							a := m.Allowed(p, tenant, want)
							if m.AllowedInOrg(p, tenant, want, orgs, env, "") && !a {
								t.Errorf("[%s p=%s tenant=%q orgs=%s env=%q want=%s] AllowedInOrg=true but Allowed=false (scope axes must only narrow)",
									mode, pName, tenant, oName, env, want)
							}
							if m.AllowedInOrgRead(p, tenant, want, orgs) && !a {
								t.Errorf("[%s p=%s tenant=%q orgs=%s want=%s] AllowedInOrgRead=true but Allowed=false (scope axes must only narrow)",
									mode, pName, tenant, oName, want)
							}
						}
					}
				}
			}
		}
	}
}

// ── T3: Allowed stays org-blind under an org-scoped rule ─────────────────────

// The full delegate pin is the legacy_equiv_test matrix (Allowed vs the
// legacy HasPermission). This adds the P4b-explicit case: with the caller
// OUTSIDE the tenant's org, Allowed still grants — it is the scope-blind entry
// point by contract (platform "*" checks); only the AllowedInOrg* methods see
// the org axis.
func TestAllowed_OrgBlindUnderOrgScopedRule(t *testing.T) {
	t.Parallel()
	m := NewForTest(orgWriteCfg())
	rec := newFakeScopeRecorder()
	m.SetScopeAuditor(rec)
	outsider := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-C"}}

	if !m.Allowed(outsider, "db-a", PermWrite) {
		t.Error("Allowed must stay org-blind (grant) with a non-member caller")
	}
	if len(rec.counts) != 0 {
		t.Errorf("Allowed must record nothing, got %v", rec.counts)
	}
	if m.AllowedInOrg(outsider, "db-a", PermWrite, []string{"ORG-A"}, "", "") {
		t.Error("AllowedInOrg must deny the same non-member caller (labeled tenant)")
	}
	if rec.counts[scopeAxisOrgWrite] != 1 {
		t.Errorf("AllowedInOrg's org denial must record org_write once, got %d", rec.counts[scopeAxisOrgWrite])
	}
}

// ── T4: write-plane cross-rule union non-leak (correctness core) ─────────────

// TestAllowedInOrg_CrossRuleUnionNoLeak proves the org axis is folded per-rule
// on the write plane. Rule A grants write but fails org for this tenant; rule
// B passes org (no org restriction) but only grants read. A top-level
// Allowed(write, from A) && orgPasses(from B) would grant the write — but no
// SINGLE rule grants it, so the per-rule fold must deny.
func TestAllowedInOrg_CrossRuleUnionNoLeak(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		// Rule A: write, org-scoped.
		{Name: "org-writers", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, OrgScope: "org"},
		// Rule B: read only, NO org restriction.
		{Name: "viewers", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
	}}
	p := &VerifiedPrincipal{Groups: []string{"org-writers", "viewers"}, Claims: map[string]string{"org": "ORG-USER"}}

	for _, orgs := range [][]string{{"ORG-OTHER"}, nil} {
		m := NewForTest(cfg)
		// Leak case: tenant labeled with a DIFFERENT org, or unlabeled → A
		// fails org, B never grants write. MUST deny.
		if m.AllowedInOrg(p, "db-x", PermWrite, orgs, "", "") {
			t.Errorf("orgs=%v: cross-rule leak — write granted though no single rule grants it", orgs)
		}
		// Control 2: read still flows through org-less rule B regardless of org
		// (proves B is live and the leak-case denial is genuinely cross-rule).
		if !m.AllowedInOrg(p, "db-x", PermRead, orgs, "", "") {
			t.Errorf("orgs=%v: control — read via org-less rule B must be granted", orgs)
		}
	}
	// Control 1: A alone grants when the org matches (proves A is live).
	if !NewForTest(cfg).AllowedInOrg(p, "db-x", PermWrite, []string{"ORG-USER"}, "", "") {
		t.Error("control — same-org write via rule A must be granted")
	}
}

// ── T5: scope-denied lands on org_write, from AllowedInOrg ONLY ──────────────

func TestAllowedInOrg_ScopeDeniedRecording(t *testing.T) {
	t.Parallel()
	member := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-A"}}

	t.Run("unlabeled tenant denies and records org_write once", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(orgWriteCfg())
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.AllowedInOrg(member, "db-a", PermWrite, nil, "", "") {
			t.Error("unlabeled tenant must be denied")
		}
		if rec.counts[scopeAxisOrgWrite] != 1 || rec.counts[scopeAxisOrg] != 0 || rec.counts[scopeAxisMetadata] != 0 || rec.counts[scopeAxisMetadataWrite] != 0 {
			t.Errorf("want org_write=1 and nothing else, got %v", rec.counts)
		}
	})

	t.Run("labeled non-member denies and records org_write once", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(orgWriteCfg())
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.AllowedInOrg(member, "db-a", PermWrite, []string{"ORG-OTHER"}, "", "") {
			t.Error("labeled non-member must be denied")
		}
		if rec.counts[scopeAxisOrgWrite] != 1 {
			t.Errorf("labeled non-match is a scope denial: want org_write=1, got %d", rec.counts[scopeAxisOrgWrite])
		}
	})

	t.Run("member on labeled tenant allows and records nothing", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(orgWriteCfg())
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if !m.AllowedInOrg(member, "db-a", PermWrite, []string{"ORG-A"}, "", "") {
			t.Error("member must be granted")
		}
		if len(rec.counts) != 0 {
			t.Errorf("an allowed write must record nothing, got %v", rec.counts)
		}
	})

	t.Run("both axes deny: each write axis recorded once", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(&RBACConfig{Groups: []GroupRule{
			{Name: "ops", Tenants: []string{"db-*"}, Permissions: []Permission{PermWrite}, OrgScope: "org", Environments: []string{"production"}},
		}})
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.AllowedInOrg(member, "db-both1", PermWrite, nil, "", "") {
			t.Error("tenant unlabeled on both axes must be denied")
		}
		if rec.counts[scopeAxisOrgWrite] != 1 || rec.counts[scopeAxisMetadataWrite] != 1 || rec.counts[scopeAxisOrg] != 0 || rec.counts[scopeAxisMetadata] != 0 {
			t.Errorf("want org_write=1 metadata_write=1 and nothing on the read axes, got %v", rec.counts)
		}
	})

	t.Run("Allowed never records", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(orgWriteCfg())
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if !m.Allowed(member, "db-a", PermWrite) {
			t.Error("Allowed (scope-blind) must grant")
		}
		if len(rec.counts) != 0 {
			t.Errorf("Allowed must record nothing on any axis, got %v", rec.counts)
		}
	})

	t.Run("list-plane ScopeAllowed records org, never org_write", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(orgWriteCfg())
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.ScopeAllowed(member, "db-a", "", "", nil) {
			t.Error("list plane: unlabeled tenant must be hidden")
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
	p := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-A"}}

	torg := tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{
		"db-a": {"ORG-OTHER"}, // mapping A: tenant belongs to someone else's org
	}})
	orgs, _ := torg.OrgsForTenant("db-a")
	if m.AllowedInOrg(p, "db-a", PermWrite, orgs, "", "") {
		t.Fatal("mapping A: non-member write must be denied")
	}

	// Hot-swap the tenantorg snapshot (models a _tenant_orgs.yaml reload while
	// an async batch is executing) and re-resolve at decision time.
	torg.Override(&tenantorg.Config{TenantOrgs: map[string][]string{
		"db-a": {"ORG-A"},
	}})
	orgs, _ = torg.OrgsForTenant("db-a")
	if !m.AllowedInOrg(p, "db-a", PermWrite, orgs, "", "") {
		t.Error("after snapshot swap: member write must be granted (orgs resolved at decision time)")
	}
}
