package rbac

// Equivalence pin for the principal-based evaluation core (ADR-027 / LD-6 P3).
//
// The refactor moved the four legacy groups-slice entry points into
// export_test.go as one-line delegates onto the new principal-based core
// (Allowed / MetadataAllowed / AccessibleEnvironmentsFor /
// AccessibleDomainsFor). These tests pin two things:
//
//  1. Delegate equivalence, full matrix: for every combination of manager
//     mode (open / fail-closed / configured), groups set, tenant and
//     permission, Allowed(&VerifiedPrincipal{Groups: g}, ...) returns
//     exactly what HasPermission(g, ...) returns (and likewise for the
//     other three). Trivially true today BY CONSTRUCTION — the point is to
//     fail loudly if a later change makes the test-only shims drift from
//     the production core.
//  2. Direct expected-value assertions on the NEW entry points (not routed
//     through the legacy names), so the new API's semantics are pinned
//     independently of the delegates — including the documented
//     nil-principal (anonymous) contract.

import (
	"fmt"
	"sort"
	"testing"
)

// equivConfig / equivManagers / equivGroupSets / equivTenants / equivPerms
// live in testhelpers_test.go.

func TestAllowed_EquivalentToLegacyHasPermission(t *testing.T) {
	t.Parallel()
	for mode, m := range equivManagers() {
		for gName, groups := range equivGroupSets() {
			for _, tenant := range equivTenants {
				for _, want := range equivPerms {
					legacy := m.HasPermission(groups, tenant, want)
					core := m.Allowed(&VerifiedPrincipal{Groups: groups}, tenant, want)
					if legacy != core {
						t.Errorf("[%s groups=%s] Allowed(&VP{Groups:%v}, %q, %s) = %v, legacy HasPermission = %v",
							mode, gName, groups, tenant, want, core, legacy)
					}
				}
			}
		}
	}
}

func TestMetadataAllowed_EquivalentToLegacyHasMetadataAccess(t *testing.T) {
	t.Parallel()
	metaCases := []struct{ env, domain string }{
		{"", ""},                    // unlabeled tenant
		{"production", "finance"},   // both match the scoped rule
		{"production", ""},          // half labeled
		{"staging", "finance"},      // env mismatch
		{"production", "ecommerce"}, // domain mismatch
	}
	// Run the matrix under BOTH scope fail-modes (LD-6 P1 shadow/enforce).
	for _, enforce := range []bool{false, true} {
		for mode, m := range equivManagers() {
			if enforce {
				m.EnableMetadataScopeEnforce()
			}
			for gName, groups := range equivGroupSets() {
				for _, tenant := range equivTenants {
					for _, mc := range metaCases {
						legacy := m.HasMetadataAccess(groups, tenant, mc.env, mc.domain)
						core := m.MetadataAllowed(&VerifiedPrincipal{Groups: groups}, tenant, mc.env, mc.domain)
						if legacy != core {
							t.Errorf("[%s enforce=%v groups=%s] MetadataAllowed(%q, %q, %q) = %v, legacy = %v",
								mode, enforce, gName, tenant, mc.env, mc.domain, core, legacy)
						}
					}
				}
			}
		}
	}
}

func TestAccessibleFor_EquivalentToLegacyAccessible(t *testing.T) {
	t.Parallel()
	sorted := func(s []string) []string {
		out := append([]string(nil), s...)
		sort.Strings(out)
		return out
	}
	for mode, m := range equivManagers() {
		for gName, groups := range equivGroupSets() {
			p := &VerifiedPrincipal{Groups: groups}

			legacyEnv, coreEnv := m.AccessibleEnvironments(groups), m.AccessibleEnvironmentsFor(p)
			if (legacyEnv == nil) != (coreEnv == nil) || fmt.Sprint(sorted(legacyEnv)) != fmt.Sprint(sorted(coreEnv)) {
				t.Errorf("[%s groups=%s] AccessibleEnvironmentsFor = %v, legacy = %v", mode, gName, coreEnv, legacyEnv)
			}

			legacyDom, coreDom := m.AccessibleDomains(groups), m.AccessibleDomainsFor(p)
			if (legacyDom == nil) != (coreDom == nil) || fmt.Sprint(sorted(legacyDom)) != fmt.Sprint(sorted(coreDom)) {
				t.Errorf("[%s groups=%s] AccessibleDomainsFor = %v, legacy = %v", mode, gName, coreDom, legacyDom)
			}
		}
	}
}

// TestAllowed_ExpectedValues pins the new entry point's semantics directly
// (NOT via the legacy delegates): the historical HasPermission truth table
// re-asserted against Allowed.
func TestAllowed_ExpectedValues(t *testing.T) {
	t.Parallel()
	configured := NewForTest(equivConfig())
	open := NewForTest(&RBACConfig{})
	failClosed := NewForTest(&RBACConfig{})
	failClosed.failClosedOnEmpty = true

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
		{"prefix rule denies other tenant", configured, []string{"db-ops"}, "redis-01", PermWrite, false},
		{"viewer reads any", configured, []string{"viewers"}, "any-tenant", PermRead, true},
		{"viewer cannot write", configured, []string{"viewers"}, "any-tenant", PermWrite, false},
		{"multi-group uses best match", configured, []string{"viewers", "db-ops"}, "db-a-prod", PermWrite, true},
		{"unknown group denied", configured, []string{"no-such-group"}, "db-a-prod", PermRead, false},
		{"nil groups denied in configured mode", configured, nil, "db-a-prod", PermRead, false},
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
