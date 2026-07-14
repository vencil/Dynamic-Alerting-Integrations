package rbac

// Evaluation semantics of the rbac core: the pure matchers (tenantMatches /
// permCovers), the claims-aware match block (ADR-027 / LD-6 P3), the Allowed
// truth table, and the nil-principal (anonymous) contract.
//
// This file pins the HIGH-risk surface of the P3 change (the rbac gate is
// the only enforcement layer, so a match-evaluation bug is an authorization
// hole):
//
//  1. Exhaustive match evaluation (table-driven): groups-only / claims-only
//     / groups+claims AND / multi-value OR / missing claim / claim value
//     mismatch / multi-rule union / legacy+match mix / nil principal / nil
//     claims / match-rule name is a pure label.
//  2. Fail-closed guardrails: empty match never matches (defense-in-depth
//     at evaluation, on top of the load-time validation error).
//
// Load-time behavior (validateConfig / strict parsing / NewManager / reload)
// lives in config_load_test.go and config_reload_test.go; the empty-config
// three-mode semantics (open / fail-closed / escape hatch) live in
// empty_config_mode_test.go; the legacy-delegate equivalence matrices live in
// legacy_equiv_test.go.

import (
	"strings"
	"testing"
)

func TestTenantMatches(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name     string
		patterns []string
		tenantID string
		want     bool
	}{
		{"wildcard", []string{"*"}, "any-tenant", true},
		{"exact match", []string{"db-a"}, "db-a", true},
		{"exact no match", []string{"db-a"}, "db-b", false},
		{"prefix match", []string{"db-a-*"}, "db-a-prod", true},
		{"prefix no match", []string{"db-a-*"}, "db-b-prod", false},
		{"multiple patterns", []string{"db-a-*", "db-b-*"}, "db-b-staging", true},
		{"empty patterns", []string{}, "db-a", false},
		// Malformed patterns must never match (fail-closed backstop for a rule
		// that bypassed validateConfig). NOTE: of these, only the first case —
		// ["**"] vs the platform-scope "*" gate — actually EXERCISES the guard:
		// without it "**" collapses to prefix "*" and HasPrefix("*","*")==true
		// would fail the rule OPEN (mutation-verified: removing the guard reddens
		// exactly that case). The others already return false on their own merits
		// (their derived prefix mismatches the id); they lock dead-rule behavior
		// against a future tenantMatches change. See TestTenantPatternInvariants
		// for the load-bearing, grammar-wide fail-open / matchability pins.
		{"double-star vs platform gate does not fail open", []string{"**"}, "*", false},
		{"double-star vs real tenant does not match", []string{"**"}, "db-a", false},
		{"embedded-star vs platform gate does not fail open", []string{"*a*"}, "*", false},
		{"embedded-star vs real tenant does not match", []string{"*a*"}, "db-a", false},
		{"trailing-double-star vs platform gate does not fail open", []string{"a**"}, "*", false},
		{"trailing-double-star vs real tenant does not match", []string{"a**"}, "db-a", false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if got := tenantMatches(tt.patterns, tt.tenantID); got != tt.want {
				t.Errorf("tenantMatches(%v, %q) = %v, want %v", tt.patterns, tt.tenantID, got, tt.want)
			}
		})
	}
}

// TestTenantPatternInvariants pins — across the whole small pattern grammar —
// the two properties the "**" fail-open fix depends on, so a future edit to
// validTenantPattern or tenantMatches that breaks allowlist↔matcher agreement
// fails loudly. Unlike most TenantMatches rows (which return false on their own
// merits and so never exercise the guard), EVERY case here is load-bearing:
//
//  1. NO FAIL-OPEN — only the literal "*" may pass a platform-scope "*" gate
//     query (Allowed(p, "*", …)). Any other pattern, well-formed or malformed,
//     must NOT match tenantID "*".
//  2. ALLOWLIST↔MATCHER AGREEMENT — every pattern validTenantPattern accepts
//     must be matchable by some id (a validateConfig-accepted rule is never a
//     silently dead rule the guard refuses to match).
func TestTenantPatternInvariants(t *testing.T) {
	t.Parallel()
	grammar := []string{
		"*", "db-a", "db-a-*", "a*", "-*", "x", // well-formed
		"**", "***", "*a", "*a*", "a**", "a*b", "", " ", "   ", // malformed
	}
	for _, pat := range grammar {
		pat := pat
		t.Run("pat="+pat, func(t *testing.T) {
			t.Parallel()
			// Invariant 1 — no fail-open at the platform-scope "*" gate.
			if got := tenantMatches([]string{pat}, "*"); got != (pat == "*") {
				t.Errorf("fail-open: tenantMatches([%q], \"*\") = %v, want %v (only \"*\" may pass a platform gate)", pat, got, pat == "*")
			}
			// Invariant 2 — an accepted pattern must be matchable by some id.
			if !validTenantPattern(pat) {
				return
			}
			var id string
			switch {
			case pat == "*":
				id = "any-tenant"
			case strings.HasSuffix(pat, "*"):
				id = strings.TrimSuffix(pat, "*") + "x" // literal prefix + one char
			default:
				id = pat // exact
			}
			if !tenantMatches([]string{pat}, id) {
				t.Errorf("accepted pattern %q not matchable: tenantMatches([%q], %q) = false", pat, pat, id)
			}
		})
	}
}

func TestPermCovers(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name  string
		grant Permission
		want  Permission
		ok    bool
	}{
		{"admin covers read", PermAdmin, PermRead, true},
		{"admin covers write", PermAdmin, PermWrite, true},
		{"admin covers admin", PermAdmin, PermAdmin, true},
		{"write covers read", PermWrite, PermRead, true},
		{"write covers write", PermWrite, PermWrite, true},
		{"write not covers admin", PermWrite, PermAdmin, false},
		{"read covers read", PermRead, PermRead, true},
		{"read not covers write", PermRead, PermWrite, false},
		{"read not covers admin", PermRead, PermAdmin, false},
		{"admin not covers unknown permission", PermAdmin, Permission("unknown"), false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if got := permCovers(tt.grant, tt.want); got != tt.ok {
				t.Errorf("permCovers(%s, %s) = %v, want %v", tt.grant, tt.want, got, tt.ok)
			}
		})
	}
}

// TestAllowed_ExpectedValues pins the evaluation entry point's semantics
// directly (NOT via the legacy delegates): the historical HasPermission truth
// table re-asserted against Allowed, extended with the multi-group and
// empty-groups rows that used to live in separate tests. Delegate parity is
// separately guaranteed by TestAllowed_EquivalentToLegacyHasPermission
// (legacy_equiv_test.go).
func TestAllowed_ExpectedValues(t *testing.T) {
	t.Parallel()
	configured := NewForTest(equivConfig())
	open := NewForTest(&RBACConfig{})
	failClosed := NewForTest(&RBACConfig{})
	failClosed.failClosedOnEmpty = true
	// Two disjoint single-tenant grants: pins that a caller in BOTH groups gets
	// each group's grant on its own tenant, and neither leaks across.
	twoTeams := NewForTest(&RBACConfig{Groups: []GroupRule{
		{Name: "team-a", Tenants: []string{"db-a"}, Permissions: []Permission{PermRead}},
		{Name: "team-b", Tenants: []string{"db-b"}, Permissions: []Permission{PermWrite}},
	}})

	cases := []struct {
		name     string
		m        *Manager
		groups   []string
		tenant   string
		want     Permission
		expected bool
	}{
		{"open mode grants read", open, []string{"any"}, "any-tenant", PermRead, true},
		{"open mode denies write", open, []string{"any"}, "any-tenant", PermWrite, false},
		{"fail-closed denies read", failClosed, []string{"any"}, "any-tenant", PermRead, false},
		{"admin wildcard writes any", configured, []string{"platform-admins"}, "redis-01", PermWrite, true},
		{"prefix rule writes matching tenant", configured, []string{"db-ops"}, "db-a-prod", PermWrite, true},
		{"prefix rule reads matching tenant", configured, []string{"db-ops"}, "db-a-staging", PermRead, true},
		{"prefix rule denies other tenant", configured, []string{"db-ops"}, "redis-01", PermWrite, false},
		{"viewer reads any", configured, []string{"viewers"}, "any-tenant", PermRead, true},
		{"viewer cannot write", configured, []string{"viewers"}, "any-tenant", PermWrite, false},
		{"multi-group uses best match", configured, []string{"viewers", "db-ops"}, "db-a-prod", PermWrite, true},
		{"unknown group denied", configured, []string{"no-such-group"}, "db-a-prod", PermRead, false},
		{"nil groups denied in configured mode", configured, nil, "db-a-prod", PermRead, false},
		{"empty groups denied in configured mode", configured, []string{}, "db-a-prod", PermRead, false},
		{"two teams: read own tenant via first group", twoTeams, []string{"team-a", "team-b"}, "db-a", PermRead, true},
		{"two teams: write other tenant via second group", twoTeams, []string{"team-a", "team-b"}, "db-b", PermWrite, true},
		{"two teams: single group has no cross-tenant access", twoTeams, []string{"team-a"}, "db-b", PermRead, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := tc.m.Allowed(&VerifiedPrincipal{Groups: tc.groups}, tc.tenant, tc.want); got != tc.expected {
				t.Errorf("Allowed(&VP{Groups:%v}, %q, %s) = %v, want %v",
					tc.groups, tc.tenant, tc.want, got, tc.expected)
			}
		})
	}
}

func TestMatch_Evaluation_Exhaustive(t *testing.T) {
	t.Parallel()
	m := NewForTest(matchEvalConfig())

	vp := func(groups []string, claims map[string]string) *VerifiedPrincipal {
		return &VerifiedPrincipal{Groups: groups, Claims: claims}
	}

	cases := []struct {
		name   string
		p      *VerifiedPrincipal
		tenant string
		want   Permission
		expect bool
	}{
		// nil principal (anonymous): no rule can ever match.
		{"nil principal denied read", nil, "ops-1", PermRead, false},
		{"nil principal denied write", nil, "alpha-1", PermWrite, false},

		// Groups-only match.
		{"groups-only hit", vp([]string{"operators"}, nil), "ops-1", PermRead, true},
		{"groups-only OR-within second entry", vp([]string{"sre"}, nil), "ops-1", PermRead, true},
		{"groups-only wrong tenant", vp([]string{"operators"}, nil), "other-1", PermRead, false},
		{"groups-only grants no write", vp([]string{"operators"}, nil), "ops-1", PermWrite, false},
		{"groups-only no matching group", vp([]string{"viewers"}, nil), "ops-1", PermRead, false},

		// Claims-only match, multi-value OR.
		{"claims-only first value", vp(nil, map[string]string{"org": "ORG-A"}), "any-tenant", PermRead, true},
		{"claims-only second value (OR-within)", vp(nil, map[string]string{"org": "ORG-B"}), "any-tenant", PermRead, true},
		{"claims-only value mismatch", vp(nil, map[string]string{"org": "ORG-C"}), "any-tenant", PermRead, false},
		{"claims-only read is not write", vp(nil, map[string]string{"org": "ORG-A"}), "any-tenant", PermWrite, false},
		{"claims-only with empty groups slice", vp([]string{}, map[string]string{"org": "ORG-B"}), "any-tenant", PermRead, true},

		// Groups AND claims.
		{"AND both hold", vp([]string{"operators"}, map[string]string{"org": "ORG-A"}), "alpha-1", PermWrite, true},
		{"AND missing claim (nil claims) fail-closed", vp([]string{"operators"}, nil), "alpha-1", PermWrite, false},
		{"AND missing claim key fail-closed", vp([]string{"operators"}, map[string]string{"region": "eu-1"}), "alpha-1", PermWrite, false},
		{"AND claim value mismatch", vp([]string{"operators"}, map[string]string{"org": "ORG-C"}), "alpha-1", PermWrite, false},
		{"AND group condition fails despite claim", vp([]string{"viewers"}, map[string]string{"org": "ORG-A"}), "alpha-1", PermWrite, false},

		// Two claim keys AND-across, OR-within each list.
		{"two claim keys both hold", vp(nil, map[string]string{"org": "ORG-A", "region": "eu-1"}), "eu-9", PermWrite, true},
		{"two claim keys OR-within second list", vp(nil, map[string]string{"org": "ORG-A", "region": "eu-2"}), "eu-9", PermWrite, true},
		{"two claim keys one missing", vp(nil, map[string]string{"org": "ORG-A"}), "eu-9", PermWrite, false},
		{"two claim keys one mismatched", vp(nil, map[string]string{"org": "ORG-A", "region": "us-1"}), "eu-9", PermWrite, false},

		// A match-rule's NAME is a pure label: being in an IdP group named
		// like the rule must NOT match it.
		{"match-rule name as group is not a hit", vp([]string{"org-a-operators"}, nil), "alpha-1", PermWrite, false},
		{"match-rule name as group is not a hit (groups-only rule)", vp([]string{"ops-rule"}, nil), "ops-1", PermRead, false},

		// Legacy rule still matches by name on the same code path.
		{"legacy rule by name", vp([]string{"legacy-admins"}, nil), "any-tenant", PermAdmin, true},
		{"legacy rule unaffected by claims", vp([]string{"legacy-admins"}, map[string]string{"org": "ORG-C"}), "any-tenant", PermAdmin, true},

		// Multi-rule union: permissions accumulate across matched rules.
		{"union: write via AND rule", vp([]string{"operators"}, map[string]string{"org": "ORG-A"}), "alpha-1", PermWrite, true},
		{"union: read outside alpha via claims-only rule", vp([]string{"operators"}, map[string]string{"org": "ORG-A"}), "zzz-1", PermRead, true},
		{"union: read on ops via groups-only rule", vp([]string{"operators"}, map[string]string{"org": "ORG-A"}), "ops-1", PermRead, true},
		{"union does not invent admin", vp([]string{"operators"}, map[string]string{"org": "ORG-A"}), "alpha-1", PermAdmin, false},
		{"legacy+match mix", vp([]string{"legacy-admins", "operators"}, map[string]string{"org": "ORG-A"}), "alpha-1", PermAdmin, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := m.Allowed(tc.p, tc.tenant, tc.want); got != tc.expect {
				t.Errorf("Allowed(%+v, %q, %s) = %v, want %v", tc.p, tc.tenant, tc.want, got, tc.expect)
			}
		})
	}
}

// TestMatch_RulesMatching pins the /me-facing view onto the same predicate:
// matched rule NAMES, including match-block hits, nothing else.
func TestMatch_RulesMatching(t *testing.T) {
	t.Parallel()
	m := NewForTest(matchEvalConfig())

	names := func(rules []GroupRule) []string {
		var out []string
		for _, r := range rules {
			out = append(out, r.Name)
		}
		return out
	}

	got := names(m.RulesMatching(&VerifiedPrincipal{
		Groups: []string{"operators"},
		Claims: map[string]string{"org": "ORG-A"},
	}))
	want := []string{"ops-rule", "org-readers", "org-a-operators"}
	if len(got) != len(want) {
		t.Fatalf("RulesMatching names = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("RulesMatching names = %v, want %v", got, want)
		}
	}

	if got := m.RulesMatching(nil); len(got) != 0 {
		t.Errorf("RulesMatching(nil) = %v, want none", names(got))
	}
	if got := names(m.RulesMatching(&VerifiedPrincipal{Groups: []string{"legacy-admins"}})); len(got) != 1 || got[0] != "legacy-admins" {
		t.Errorf("RulesMatching(legacy-admins) = %v, want [legacy-admins]", got)
	}
}

// TestMatch_MetadataAndAccessibleSets: the shared predicate drives the other
// evaluation methods too — a claims-matched rule contributes its
// environment/domain scope exactly like a name-matched one.
func TestMatch_MetadataAndAccessibleSets(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{Groups: []GroupRule{
		{Name: "env-scoped-match", Match: &MatchBlock{Claims: map[string][]string{"org": {"ORG-A"}}},
			Tenants: []string{"*"}, Permissions: []Permission{PermRead},
			Environments: []string{"production"}, Domains: []string{"finance"}},
		{Name: "legacy-unscoped", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
	}})

	claimed := &VerifiedPrincipal{Claims: map[string]string{"org": "ORG-A"}}
	unclaimed := &VerifiedPrincipal{Claims: map[string]string{"org": "ORG-Z"}}

	if got := m.AccessibleEnvironmentsFor(claimed); len(got) != 1 || got[0] != "production" {
		t.Errorf("AccessibleEnvironmentsFor(claimed) = %v, want [production]", got)
	}
	if got := m.AccessibleDomainsFor(claimed); len(got) != 1 || got[0] != "finance" {
		t.Errorf("AccessibleDomainsFor(claimed) = %v, want [finance]", got)
	}
	if got := m.AccessibleEnvironmentsFor(unclaimed); len(got) != 0 {
		t.Errorf("AccessibleEnvironmentsFor(unclaimed) = %v, want empty", got)
	}

	// Labeled-tenant membership is mode-independent (both shadow and enforce).
	if !m.MetadataAllowed(claimed, "t1", "production", "finance") {
		t.Error("MetadataAllowed(claimed, production/finance) = false, want true")
	}
	if m.MetadataAllowed(claimed, "t1", "staging", "finance") {
		t.Error("MetadataAllowed(claimed, staging) = true, want false (env outside the matched rule's scope)")
	}
	if m.MetadataAllowed(unclaimed, "t1", "production", "finance") {
		t.Error("MetadataAllowed(unclaimed) = true, want false (no rule matches)")
	}

	// The legacy-unscoped rule keeps its wildcard semantics by name.
	legacy := &VerifiedPrincipal{Groups: []string{"legacy-unscoped"}}
	if got := m.AccessibleEnvironmentsFor(legacy); got != nil {
		t.Errorf("AccessibleEnvironmentsFor(legacy) = %v, want nil (no restriction)", got)
	}
}

// TestMatch_EmptyMatchNeverMatches: validateConfig rejects an empty match at
// load, but a snapshot injected around the loader (NewForTest / Override)
// bypasses validation — the evaluator itself must fail closed, because the
// only wrong default for "empty match" in an enforcement layer is match-all.
// (The load-time rejection is the "empty match block" row of
// invalidConfigTable in config_load_test.go — a deliberate double defense.)
func TestMatch_EmptyMatchNeverMatches(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{Groups: []GroupRule{
		{Name: "trap", Match: &MatchBlock{}, Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
	}})

	principals := map[string]*VerifiedPrincipal{
		"nil":             nil,
		"with groups":     {Groups: []string{"anything", "trap"}},
		"with claims":     {Claims: map[string]string{"org": "ORG-A"}},
		"groups+claims":   {Groups: []string{"trap"}, Claims: map[string]string{"org": "ORG-A"}},
		"empty principal": {},
	}
	for name, p := range principals {
		if m.Allowed(p, "any-tenant", PermRead) {
			t.Errorf("empty match block matched principal %q — empty match must NEVER be match-all", name)
		}
	}
}

// TestAllowed_NilPrincipalContract pins the documented nil-principal
// (anonymous) contract: nil evaluates exactly like the empty groups slice —
// open mode still grants read (the pre-existing rbac open-read behavior must
// NOT be tightened by the principal refactor), configured and fail-closed
// modes deny, and nil / &VP{} / &VP{Groups: nil} are indistinguishable.
func TestAllowed_NilPrincipalContract(t *testing.T) {
	t.Parallel()

	t.Run("open mode keeps anonymous read, denies write", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(&RBACConfig{})
		if !m.Allowed(nil, "any-tenant", PermRead) {
			t.Error("open mode: Allowed(nil, read) must stay TRUE (legacy open-read; must not tighten)")
		}
		if m.Allowed(nil, "any-tenant", PermWrite) {
			t.Error("open mode: Allowed(nil, write) must be false")
		}
		if m.Allowed(nil, "any-tenant", PermAdmin) {
			t.Error("open mode: Allowed(nil, admin) must be false")
		}
	})

	t.Run("fail-closed mode denies anonymous entirely", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(&RBACConfig{})
		m.failClosedOnEmpty = true
		if m.Allowed(nil, "*", PermRead) {
			t.Error("fail-closed: Allowed(nil, read) must be false")
		}
	})

	t.Run("configured mode never matches a rule for anonymous", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(equivConfig())
		for _, tenant := range equivTenants {
			for _, want := range equivPerms {
				if m.Allowed(nil, tenant, want) {
					t.Errorf("configured: Allowed(nil, %q, %s) must be false", tenant, want)
				}
			}
		}
	})

	t.Run("nil equals empty principal equals nil-groups principal", func(t *testing.T) {
		t.Parallel()
		for mode, m := range equivManagers() {
			for _, tenant := range equivTenants {
				for _, want := range equivPerms {
					viaNil := m.Allowed(nil, tenant, want)
					viaEmpty := m.Allowed(&VerifiedPrincipal{}, tenant, want)
					viaNilGroups := m.Allowed(&VerifiedPrincipal{Groups: nil}, tenant, want)
					if viaNil != viaEmpty || viaNil != viaNilGroups {
						t.Errorf("[%s] anonymous representations disagree for (%q, %s): nil=%v empty=%v nilGroups=%v",
							mode, tenant, want, viaNil, viaEmpty, viaNilGroups)
					}
				}
			}
		}
	})

	t.Run("MetadataAllowed nil principal", func(t *testing.T) {
		t.Parallel()
		open := NewForTest(&RBACConfig{})
		if !open.MetadataAllowed(nil, "t", "production", "finance") {
			t.Error("open mode: MetadataAllowed(nil) must be true (no restrictions)")
		}
		failClosed := NewForTest(&RBACConfig{})
		failClosed.failClosedOnEmpty = true
		if failClosed.MetadataAllowed(nil, "t", "production", "finance") {
			t.Error("fail-closed: MetadataAllowed(nil) must be false")
		}
		configured := NewForTest(equivConfig())
		if configured.MetadataAllowed(nil, "t", "production", "finance") {
			t.Error("configured: MetadataAllowed(nil) must be false (no rule matches anonymous)")
		}
	})

	t.Run("Accessible sets for nil principal", func(t *testing.T) {
		t.Parallel()
		open := NewForTest(&RBACConfig{})
		if got := open.AccessibleEnvironmentsFor(nil); got != nil {
			t.Errorf("open mode: AccessibleEnvironmentsFor(nil) = %v, want nil (no restriction)", got)
		}
		if got := open.AccessibleDomainsFor(nil); got != nil {
			t.Errorf("open mode: AccessibleDomainsFor(nil) = %v, want nil (no restriction)", got)
		}
		configured := NewForTest(equivConfig())
		if got := configured.AccessibleEnvironmentsFor(nil); got == nil || len(got) != 0 {
			t.Errorf("configured: AccessibleEnvironmentsFor(nil) = %v, want empty non-nil set", got)
		}
		if got := configured.AccessibleDomainsFor(nil); got == nil || len(got) != 0 {
			t.Errorf("configured: AccessibleDomainsFor(nil) = %v, want empty non-nil set", got)
		}
	})
}
