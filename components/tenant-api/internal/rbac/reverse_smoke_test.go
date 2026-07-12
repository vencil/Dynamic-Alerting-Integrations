package rbac

// Smoke tests for the reverse access report (ADR-027 / LD-6 P6) — basic happy
// path + LOCKED bar semantics. The full dogfood drift-invariant suite
// (witness-positive rule-isolated replay, completeness property, adversarial
// case table, redact golden) lives in reverse_dogfood_test.go.

import (
	"reflect"
	"testing"
)

// smokeReverseConfig: one legacy platform-wide admin rule, one org-scoped
// match-block rule whose claims pin the org value domain, and one org-scoped
// rule whose pinned domain cannot intersect the fixture tenant's orgs
// (unsatisfiable). Fixture ids are synthetic (dev-rule #2), mirroring the
// existing rbac test fixtures.
func smokeReverseConfig() *RBACConfig {
	return &RBACConfig{Groups: []GroupRule{
		{Name: "platform-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
		{
			Name: "org-ops",
			Match: &MatchBlock{
				Groups: []string{"ops"},
				Claims: map[string][]string{"org": {"ORG-1", "ORG-2"}},
			},
			Tenants:     []string{"db-*"},
			Permissions: []Permission{PermRead, PermWrite},
			OrgScope:    "org",
		},
		{
			Name:        "org-pinned-elsewhere",
			Match:       &MatchBlock{Claims: map[string][]string{"org": {"ORG-X"}}},
			Tenants:     []string{"db-*"},
			Permissions: []Permission{PermRead},
			OrgScope:    "org",
		},
	}}
}

func TestReverseAccessReport_SmokeHappyPath(t *testing.T) {
	m := NewForTest(smokeReverseConfig())
	tenantOrgs := []string{"ORG-1", "ORG-9"}

	rep := m.ReverseAccessReport("db-team-1", tenantOrgs, true, "torgs-hash-1",
		ReverseReportOptions{IncludeOrgValues: true})

	if rep.Mode != ReverseModeRules {
		t.Fatalf("mode = %q, want %q", rep.Mode, ReverseModeRules)
	}
	if rep.Verdict != ReverseVerdictGrantsFound {
		t.Fatalf("verdict = %q, want %q", rep.Verdict, ReverseVerdictGrantsFound)
	}
	if len(rep.Grants) != 3 {
		t.Fatalf("len(grants) = %d, want 3", len(rep.Grants))
	}

	// Config anchors: NewForTest has no file hash → unanchored, never "".
	if got := rep.ConfigAnchor.RBACSHA256.Value; got != AnchorUnanchored {
		t.Errorf("rbac anchor = %q, want %q", got, AnchorUnanchored)
	}
	if got := rep.ConfigAnchor.TenantOrgsSHA256.Value; got != "torgs-hash-1" {
		t.Errorf("tenant_orgs anchor = %q, want caller-injected hash", got)
	}

	// Tenant block: labeled, opt-in org values present and sorted.
	if rep.Tenant.OrgStatus != OrgStatusLabeled {
		t.Errorf("org_status = %q, want %q", rep.Tenant.OrgStatus, OrgStatusLabeled)
	}
	if want := []string{"ORG-1", "ORG-9"}; !reflect.DeepEqual(rep.Tenant.Orgs, want) {
		t.Errorf("tenant.orgs = %v, want %v", rep.Tenant.Orgs, want)
	}

	// Grant 0: legacy platform-wide admin.
	g0 := rep.Grants[0]
	if g0.Index != 0 || !g0.PlatformWide || g0.TenantPattern != "*" {
		t.Errorf("grant0 = index %d platform_wide %v pattern %q, want 0/true/*",
			g0.Index, g0.PlatformWide, g0.TenantPattern)
	}
	if !g0.Effective.Admin || !g0.Effective.Write || !g0.Effective.Read {
		t.Errorf("grant0 effective = %+v, want all true (admin ⊇ write ⊇ read)", g0.Effective)
	}
	if g0.Who.Kind != WhoKindLegacyGroup || g0.Who.LegacyGroup != "platform-admins" {
		t.Errorf("grant0 who = %+v, want legacy_group/platform-admins", g0.Who)
	}
	if g0.OrgGate.Required || g0.OrgGate.OutcomeShadow != OrgOutcomeNotRequired {
		t.Errorf("grant0 org_gate = %+v, want not required", g0.OrgGate)
	}
	if want := []string{SurfaceList, SurfaceReadByID, SurfaceWrite, SurfaceAdmin}; !reflect.DeepEqual(g0.Surfaces, want) {
		t.Errorf("grant0 surfaces = %v, want %v", g0.Surfaces, want)
	}

	// Grant 1: org-scoped match rule — passing values = orgs(X) ∩ claims pin,
	// derived by enumeration (ORG-1 in both; ORG-9 not pinned; ORG-2 not an
	// org of X).
	g1 := rep.Grants[1]
	if g1.TenantPattern != "db-*" || g1.PlatformWide {
		t.Errorf("grant1 pattern = %q platform_wide %v, want db-*/false", g1.TenantPattern, g1.PlatformWide)
	}
	if !g1.OrgGate.Required || g1.OrgGate.ClaimKey != "org" {
		t.Errorf("grant1 org_gate = %+v, want required on key org", g1.OrgGate)
	}
	if g1.OrgGate.OutcomeShadow != OrgOutcomeConditional || g1.OrgGate.OutcomeEnforce != OrgOutcomeConditional {
		t.Errorf("grant1 outcomes = %q/%q, want conditional/conditional",
			g1.OrgGate.OutcomeShadow, g1.OrgGate.OutcomeEnforce)
	}
	if want := []string{"ORG-1"}; !reflect.DeepEqual(g1.OrgGate.PassingOrgValues, want) {
		t.Errorf("grant1 passing_org_values = %v, want %v (intersection by enumeration)",
			g1.OrgGate.PassingOrgValues, want)
	}
	if g1.OrgGate.Unsatisfiable {
		t.Errorf("grant1 unsatisfiable = true, want false")
	}
	if g1.Who.Kind != WhoKindMatchBlock ||
		!reflect.DeepEqual(g1.Who.ClaimsAllOf, map[string][]string{"org": {"ORG-1", "ORG-2"}}) {
		t.Errorf("grant1 who = %+v, want match_block with verbatim claims map", g1.Who)
	}

	// Grant 2: OrgScope × pinned claims with empty intersection → unsatisfiable.
	g2 := rep.Grants[2]
	if !g2.OrgGate.Unsatisfiable {
		t.Errorf("grant2 unsatisfiable = false, want true (ORG-X ∩ {ORG-1,ORG-9} = ∅)")
	}
	if len(g2.OrgGate.PassingOrgValues) != 0 {
		t.Errorf("grant2 passing_org_values = %v, want empty", g2.OrgGate.PassingOrgValues)
	}

	// Forward cross-check (mini witness): a principal satisfying grant1's WHO
	// with a passing org value must be granted by the REAL forward gate — the
	// reverse claim and the forward decision must agree.
	p := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-1"}}
	if !m.AllowedInOrg(p, "db-team-1", PermWrite, tenantOrgs) {
		t.Errorf("forward AllowedInOrg denies the witness the reverse report claims grant1 admits")
	}

	// Default view (no opt-in): org values must be ABSENT.
	def := m.ReverseAccessReport("db-team-1", tenantOrgs, true, "torgs-hash-1", ReverseReportOptions{})
	if def.Tenant.Orgs != nil {
		t.Errorf("default view tenant.orgs = %v, want absent", def.Tenant.Orgs)
	}
	if pv := def.Grants[1].OrgGate.PassingOrgValues; pv != nil {
		t.Errorf("default view passing_org_values = %v, want absent", pv)
	}
	if !def.Grants[2].OrgGate.Unsatisfiable {
		t.Errorf("default view must still carry unsatisfiable=true (boolean is not opt-in)")
	}
}

func TestReverseAccessReport_ModesAndRedact(t *testing.T) {
	// open_read: path-less empty config — grants empty, but the report must
	// say any authenticated caller can read (mode + verdict), not "no one".
	open := NewForTest(&RBACConfig{})
	rep := open.ReverseAccessReport("db-team-1", nil, false, "", ReverseReportOptions{DevBypassActive: true})
	if rep.Mode != ReverseModeOpenRead || rep.Verdict != ReverseVerdictOpenRead {
		t.Errorf("open mode = %q/%q, want open_read/open_read", rep.Mode, rep.Verdict)
	}
	if rep.Tenant.OrgStatus != OrgStatusNotOnboarded {
		t.Errorf("org_status = %q, want %q for known=false", rep.Tenant.OrgStatus, OrgStatusNotOnboarded)
	}
	if got := rep.ConfigAnchor.TenantOrgsSHA256.Value; got != AnchorUnanchored {
		t.Errorf("empty tenant_orgs hash anchor = %q, want %q", got, AnchorUnanchored)
	}
	// Dynamic dev-bypass status (round-1 C7): caller-injected runtime value.
	if got := rep.Completeness.NotCovered[0].Status; got != "active" {
		t.Errorf("dev_bypass status = %q, want active", got)
	}

	// fail_closed_empty: configured-but-empty policy (MED-8).
	closed := NewForTest(&RBACConfig{})
	closed.failClosedOnEmpty = true
	rep = closed.ReverseAccessReport("db-team-1", nil, false, "", ReverseReportOptions{})
	if rep.Mode != ReverseModeFailClosedEmpty || rep.Verdict != ReverseVerdictFailClosedEmpty {
		t.Errorf("fail-closed mode = %q/%q, want fail_closed_empty×2", rep.Mode, rep.Verdict)
	}
	if got := rep.Completeness.NotCovered[0].Status; got != "inactive" {
		t.Errorf("dev_bypass status = %q, want inactive", got)
	}

	// Redacted projection: allowlist rebuild strips the three identifier
	// classes, keeps skeleton + counts + outcomes.
	m := NewForTest(smokeReverseConfig())
	full := m.ReverseAccessReport("db-team-1", []string{"ORG-1", "ORG-9"}, true, "h",
		ReverseReportOptions{IncludeOrgValues: true})
	red := RedactReverseReport(full)

	if red.Tenant.Orgs != nil {
		t.Errorf("redacted tenant.orgs = %v, want stripped", red.Tenant.Orgs)
	}
	for i, g := range red.Grants {
		if g.Rule != "" || g.TenantPattern != "" {
			t.Errorf("redacted grant %d keeps rule name %q / pattern %q", i, g.Rule, g.TenantPattern)
		}
		if g.Who.LegacyGroup != "" || g.Who.GroupsAnyOf != nil || g.Who.ClaimsAllOf != nil {
			t.Errorf("redacted grant %d keeps who identifiers: %+v", i, g.Who)
		}
		if g.Who.GroupsCount == nil || g.Who.ClaimsCount == nil {
			t.Errorf("redacted grant %d missing who counts", i)
		}
		if g.OrgGate.ClaimKey != "" || g.OrgGate.PassingOrgValues != nil {
			t.Errorf("redacted grant %d keeps org identifiers: %+v", i, g.OrgGate)
		}
	}
	if k := red.Grants[0].PatternKind; k != PatternKindWildcard {
		t.Errorf("grant0 pattern_kind = %q, want wildcard", k)
	}
	if k := red.Grants[1].PatternKind; k != PatternKindPrefix {
		t.Errorf("grant1 pattern_kind = %q, want prefix", k)
	}
	if gc := *red.Grants[0].Who.GroupsCount; gc != 1 {
		t.Errorf("legacy who groups_count = %d, want 1", gc)
	}
	if cc := *red.Grants[1].Who.ClaimsCount; cc != 1 {
		t.Errorf("match who claims_count = %d, want 1", cc)
	}
	// Booleans/outcomes survive redaction.
	if !red.Grants[2].OrgGate.Unsatisfiable || red.Grants[1].OrgGate.OutcomeShadow != OrgOutcomeConditional {
		t.Errorf("redacted view lost outcome/unsatisfiable booleans")
	}
	// Idempotent: re-redacting changes nothing.
	if again := RedactReverseReport(red); !reflect.DeepEqual(again, red) {
		t.Errorf("RedactReverseReport not idempotent")
	}
}

func TestPlatformAdminNonOrgScoped_Bar(t *testing.T) {
	cfg := &RBACConfig{Groups: []GroupRule{
		// Org-scoped wildcard admin: passes the bare Allowed(p,"*",PermAdmin)
		// but must FAIL the tightened bar (owner decision §0.1 — the org-blind
		// seam this helper exists to close).
		{Name: "org-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}, OrgScope: "org"},
		// Legit non-org-scoped platform admin.
		{Name: "platform-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
		// Wildcard but read-only: no admin.
		{Name: "auditors", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		// Admin but not platform-wide.
		{Name: "team-admins", Tenants: []string{"db-*"}, Permissions: []Permission{PermAdmin}},
	}}
	m := NewForTest(cfg)

	cases := []struct {
		name string
		p    *VerifiedPrincipal
		want bool
	}{
		{"org-scoped wildcard admin fails the bar", &VerifiedPrincipal{Groups: []string{"org-admins"}, Claims: map[string]string{"org": "ORG-1"}}, false},
		{"non-org-scoped platform admin passes", &VerifiedPrincipal{Groups: []string{"platform-admins"}}, true},
		{"wildcard read-only fails", &VerifiedPrincipal{Groups: []string{"auditors"}}, false},
		{"non-platform-wide admin fails", &VerifiedPrincipal{Groups: []string{"team-admins"}}, false},
		{"anonymous fails", nil, false},
	}
	for _, tc := range cases {
		if got := m.PlatformAdminNonOrgScoped(tc.p); got != tc.want {
			t.Errorf("%s: got %v, want %v", tc.name, got, tc.want)
		}
	}

	// Cross-check the seam the bar closes: the bare org-blind check WOULD
	// have admitted the org-scoped wildcard admin.
	orgAdmin := &VerifiedPrincipal{Groups: []string{"org-admins"}, Claims: map[string]string{"org": "ORG-1"}}
	if !m.Allowed(orgAdmin, "*", PermAdmin) {
		t.Fatalf("premise broken: bare Allowed no longer admits the org-scoped wildcard admin")
	}

	// Both zero-group states: no rule can satisfy the bar → false.
	open := NewForTest(&RBACConfig{})
	if open.PlatformAdminNonOrgScoped(&VerifiedPrincipal{Groups: []string{"anyone"}}) {
		t.Errorf("open_read mode must not satisfy the bar")
	}
	closed := NewForTest(&RBACConfig{})
	closed.failClosedOnEmpty = true
	if closed.PlatformAdminNonOrgScoped(&VerifiedPrincipal{Groups: []string{"anyone"}}) {
		t.Errorf("fail_closed_empty mode must not satisfy the bar")
	}
}
