package rbac

// Equivalence pin for the principal-based evaluation core (ADR-027 / LD-6 P3).
//
// The refactor moved the four legacy groups-slice entry points into
// export_test.go as one-line delegates onto the new principal-based core
// (Allowed / MetadataAllowed / AccessibleEnvironmentsFor /
// AccessibleDomainsFor). These matrices pin delegate equivalence in full:
// for every combination of manager mode (open / fail-closed / configured),
// groups set, tenant and permission, Allowed(&VerifiedPrincipal{Groups: g},
// ...) returns exactly what HasPermission(g, ...) returns (and likewise for
// the other three). Trivially true today BY CONSTRUCTION — the point is to
// fail loudly if a later change makes the test-only shims drift from the
// production core.
//
// The direct expected-value assertions on the NEW entry points (the truth
// table + nil-principal contract) live in match_eval_test.go; the equiv*
// fixtures live in testhelpers_test.go.

import (
	"fmt"
	"sort"
	"testing"
)

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
