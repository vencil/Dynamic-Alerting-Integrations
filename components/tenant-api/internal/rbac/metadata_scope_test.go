package rbac

// Metadata (environment/domain) axis: the pure matchers (metadataMatches /
// scopeFieldPasses), the HasMetadataAccess restriction matrix, the accessible
// env/domain union sets, and the enforced list-plane behavior with per-decision
// scope-denied recording (ADR-027 §7).
//
// fakeScopeRecorder / newFakeScopeRecorder / toSet live in testhelpers_test.go.

import (
	"testing"
)

func TestMetadataMatches(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name      string
		allowList []string
		value     string
		want      bool
	}{
		// Empty allowList = wildcard (all values allowed)
		{"empty allowList matches any value", []string{}, "production", true},
		{"empty allowList matches empty value", []string{}, "", true},
		// Empty value on a NON-empty allow-list is not a match: metadataMatches
		// is pure exact-membership. The unlabeled-tenant decision lives in
		// scopeFieldPasses, covered by TestScopeFieldPasses /
		// TestHasMetadataAccess_ScopeEnforced below.
		{"empty value does not match non-empty allowList (pure membership)", []string{"production", "staging"}, "", false},
		// Exact match cases
		{"exact match in allowList", []string{"production", "staging", "dev"}, "production", true},
		{"exact match with single entry", []string{"finance"}, "finance", true},
		// No match cases
		{"value not in allowList", []string{"production", "staging"}, "dev", false},
		{"single value not in single allowList entry", []string{"production"}, "staging", false},
		// Multiple entries in allowList
		{"matches second entry in allowList", []string{"production", "staging", "dev"}, "staging", true},
		{"matches last entry in allowList", []string{"production", "staging", "dev"}, "dev", true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := metadataMatches(tt.allowList, tt.value)
			if got != tt.want {
				t.Errorf("metadataMatches(%v, %q) = %v, want %v",
					tt.allowList, tt.value, got, tt.want)
			}
		})
	}
}

// rules builds a config from inline GroupRule literals — the compact fixture
// shape for the metadata tables below. NOTE the two distinct wildcard shapes
// both appear in the rows on purpose: an explicit empty slice
// (Environments: []string{}) is the new-format wildcard, an OMITTED field
// (nil) is the old pre-env/domain format — both must behave identically
// (backward compat).
func rules(rs ...GroupRule) *RBACConfig { return &RBACConfig{Groups: rs} }

func TestHasMetadataAccess(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name        string
		config      *RBACConfig
		idpGroups   []string
		tenantID    string
		environment string
		domain      string
		want        bool
	}{
		// Empty rule set: no open mode (ADR-027 §7)
		{"empty rule set denies any metadata",
			&RBACConfig{Groups: []GroupRule{}},
			[]string{"any-group"}, "any-tenant", "production", "finance", false},
		// Wildcard rules (no environment/domain restrictions, explicit empty slices)
		{"wildcard environments and domains",
			rules(GroupRule{Name: "admins", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, Environments: []string{}, Domains: []string{}}),
			[]string{"admins"}, "db-a", "production", "finance", true},
		// Environment-restricted rule
		{"environment-restricted, matching",
			rules(GroupRule{Name: "prod-ops", Tenants: []string{"db-a-*"}, Permissions: []Permission{PermWrite}, Environments: []string{"production", "staging"}, Domains: []string{}}),
			[]string{"prod-ops"}, "db-a-primary", "production", "any", true},
		{"environment-restricted, not matching",
			rules(GroupRule{Name: "prod-ops", Tenants: []string{"db-a-*"}, Permissions: []Permission{PermWrite}, Environments: []string{"production", "staging"}, Domains: []string{}}),
			[]string{"prod-ops"}, "db-a-primary", "dev", "any", false},
		// Domain-restricted rule
		{"domain-restricted, matching",
			rules(GroupRule{Name: "finance-ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}, Environments: []string{}, Domains: []string{"finance", "compliance"}}),
			[]string{"finance-ops"}, "db-a", "any", "finance", true},
		{"domain-restricted, not matching",
			rules(GroupRule{Name: "finance-ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}, Environments: []string{}, Domains: []string{"finance", "compliance"}}),
			[]string{"finance-ops"}, "db-a", "any", "ecommerce", false},
		// Both environment and domain restricted
		{"both environment and domain, both match",
			rules(GroupRule{Name: "finance-prod-ops", Tenants: []string{"db-a", "db-b"}, Permissions: []Permission{PermWrite}, Environments: []string{"production", "staging"}, Domains: []string{"finance", "compliance"}}),
			[]string{"finance-prod-ops"}, "db-a", "production", "finance", true},
		{"both environment and domain, environment fails",
			rules(GroupRule{Name: "finance-prod-ops", Tenants: []string{"db-a", "db-b"}, Permissions: []Permission{PermWrite}, Environments: []string{"production", "staging"}, Domains: []string{"finance", "compliance"}}),
			[]string{"finance-prod-ops"}, "db-a", "dev", "finance", false},
		{"both environment and domain, domain fails",
			rules(GroupRule{Name: "finance-prod-ops", Tenants: []string{"db-a", "db-b"}, Permissions: []Permission{PermWrite}, Environments: []string{"production", "staging"}, Domains: []string{"finance", "compliance"}}),
			[]string{"finance-prod-ops"}, "db-a", "production", "ecommerce", false},
		// Empty metadata in tenant: unlabeled on a restricted field is denied
		{"empty environment denied under restriction",
			rules(GroupRule{Name: "ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}, Environments: []string{"production"}, Domains: []string{}}),
			[]string{"ops"}, "db-a", "", "", false},
		// ...but an unlabeled field the rule does NOT restrict still passes
		{"empty domain passes when only environment is restricted",
			rules(GroupRule{Name: "ops", Tenants: []string{"svc-meta"}, Permissions: []Permission{PermWrite}, Environments: []string{"production"}, Domains: []string{}}),
			[]string{"ops"}, "svc-meta", "production", "", true},
		// Tenant doesn't match rule
		{"tenant doesn't match pattern",
			rules(GroupRule{Name: "db-a-ops", Tenants: []string{"db-a-*"}, Permissions: []Permission{PermWrite}, Environments: []string{"production"}, Domains: []string{}}),
			[]string{"db-a-ops"}, "db-b-primary", "production", "any", false},
		// User not in matching group
		{"user not in any matching group",
			rules(GroupRule{Name: "prod-ops", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, Environments: []string{"production"}, Domains: []string{}}),
			[]string{"other-group"}, "db-a", "production", "any", false},
		// Multiple groups, one matches
		{"multiple groups, second matches metadata",
			rules(
				GroupRule{Name: "finance-ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}, Environments: []string{"dev"}, Domains: []string{}},
				GroupRule{Name: "prod-ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermRead}, Environments: []string{"production"}, Domains: []string{}},
			),
			[]string{"finance-ops", "prod-ops"}, "db-a", "production", "any", true},
		// Backward compat: rules without Environments/Domains (old format,
		// fields OMITTED → nil) behave as wildcard exactly like the explicit
		// empty-slice shape above.
		{"old rule without fields behaves as wildcard",
			rules(GroupRule{Name: "ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}}),
			[]string{"ops"}, "db-a", "production", "finance", true},
		{"old rule allows any environment",
			rules(GroupRule{Name: "ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}}),
			[]string{"ops"}, "db-a", "dev", "any", true},
		{"old rule allows any domain",
			rules(GroupRule{Name: "ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}}),
			[]string{"ops"}, "db-a", "any", "newdomain", true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			m := NewForTest(tt.config)

			got := m.HasMetadataAccess(tt.idpGroups, tt.tenantID, tt.environment, tt.domain)
			if got != tt.want {
				t.Errorf("HasMetadataAccess(%v, %q, %q, %q) = %v, want %v",
					tt.idpGroups, tt.tenantID, tt.environment, tt.domain, got, tt.want)
			}
		})
	}
}

func TestAccessibleEnvironments(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name      string
		config    *RBACConfig
		idpGroups []string
		want      []string
	}{
		{"empty rule set returns empty (no access)",
			&RBACConfig{Groups: []GroupRule{}}, []string{"any"}, []string{}},
		{"wildcard environments rule returns nil",
			rules(GroupRule{Name: "admins", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, Environments: []string{}}),
			[]string{"admins"}, nil},
		{"single group with restricted environments",
			rules(GroupRule{Name: "prod-ops", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, Environments: []string{"production", "staging"}}),
			[]string{"prod-ops"}, []string{"production", "staging"}},
		{"multiple groups union environments",
			rules(
				GroupRule{Name: "prod-ops", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, Environments: []string{"production", "staging"}},
				GroupRule{Name: "dev-ops", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, Environments: []string{"dev", "local"}},
			),
			[]string{"prod-ops", "dev-ops"}, []string{"production", "staging", "dev", "local"}},
		{"one group wildcard with others restricted returns nil",
			rules(
				GroupRule{Name: "admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}, Environments: []string{}},
				GroupRule{Name: "prod-ops", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, Environments: []string{"production"}},
			),
			[]string{"admins", "prod-ops"}, nil},
		{"user in no matching groups returns empty (no access)",
			rules(GroupRule{Name: "prod-ops", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, Environments: []string{"production"}}),
			[]string{"other-group"}, []string{}},
		{"duplicate environments are deduped",
			rules(
				GroupRule{Name: "group1", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, Environments: []string{"production", "staging"}},
				GroupRule{Name: "group2", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, Environments: []string{"production", "dev"}},
			),
			[]string{"group1", "group2"}, []string{"production", "staging", "dev"}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			m := NewForTest(tt.config)

			got := m.AccessibleEnvironments(tt.idpGroups)

			// nil ("no restriction") and non-nil empty ("no access") are
			// opposite answers; toSet below would conflate them.
			if (tt.want == nil) != (got == nil) {
				t.Fatalf("AccessibleEnvironments(%v) = %#v, want %#v (nil-ness differs)", tt.idpGroups, got, tt.want)
			}
			// For slice comparison, convert to map for order-independent comparison
			if want, got := toSet(tt.want), toSet(got); len(want) != len(got) {
				t.Errorf("AccessibleEnvironments(%v) returned %v, want %v",
					tt.idpGroups, got, tt.want)
			} else {
				for k := range want {
					if !got[k] {
						t.Errorf("AccessibleEnvironments(%v) missing %q", tt.idpGroups, k)
					}
				}
			}
		})
	}
}

func TestAccessibleDomains(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name      string
		config    *RBACConfig
		idpGroups []string
		want      []string
	}{
		{"empty rule set returns empty (no access)",
			&RBACConfig{Groups: []GroupRule{}}, []string{"any"}, []string{}},
		{"wildcard domains rule returns nil",
			rules(GroupRule{Name: "admins", Tenants: []string{"*"}, Domains: []string{}}),
			[]string{"admins"}, nil},
		{"single group with restricted domains",
			rules(GroupRule{Name: "finance-ops", Tenants: []string{"*"}, Domains: []string{"finance", "compliance"}}),
			[]string{"finance-ops"}, []string{"finance", "compliance"}},
		{"multiple groups union domains",
			rules(
				GroupRule{Name: "finance-ops", Tenants: []string{"*"}, Domains: []string{"finance", "compliance"}},
				GroupRule{Name: "ecommerce-ops", Tenants: []string{"*"}, Domains: []string{"ecommerce", "operations"}},
			),
			[]string{"finance-ops", "ecommerce-ops"}, []string{"finance", "compliance", "ecommerce", "operations"}},
		{"one group wildcard with others restricted returns nil",
			rules(
				GroupRule{Name: "admins", Tenants: []string{"*"}, Domains: []string{}},
				GroupRule{Name: "finance-ops", Tenants: []string{"*"}, Domains: []string{"finance"}},
			),
			[]string{"admins", "finance-ops"}, nil},
		{"duplicate domains are deduped",
			rules(
				GroupRule{Name: "group1", Tenants: []string{"*"}, Domains: []string{"finance", "compliance"}},
				GroupRule{Name: "group2", Tenants: []string{"*"}, Domains: []string{"finance", "operations"}},
			),
			[]string{"group1", "group2"}, []string{"finance", "compliance", "operations"}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			m := NewForTest(tt.config)

			got := m.AccessibleDomains(tt.idpGroups)

			if (tt.want == nil) != (got == nil) {
				t.Fatalf("AccessibleDomains(%v) = %#v, want %#v (nil-ness differs)", tt.idpGroups, got, tt.want)
			}
			// Order-independent comparison
			if want, got := toSet(tt.want), toSet(got); len(want) != len(got) {
				t.Errorf("AccessibleDomains(%v) returned %v, want %v",
					tt.idpGroups, got, tt.want)
			} else {
				for k := range want {
					if !got[k] {
						t.Errorf("AccessibleDomains(%v) missing %q", tt.idpGroups, k)
					}
				}
			}
		})
	}
}

// TestScopeFieldPasses pins the pure field evaluator: an empty allow-list is
// the wildcard, an unlabeled value on a restricted field fails, and a labeled
// value is exact membership.
func TestScopeFieldPasses(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name      string
		allowList []string
		value     string
		want      bool
	}{
		{"empty allow-list is wildcard", nil, "production", true},
		{"empty allow-list wildcard even for empty value", nil, "", true},
		{"unlabeled on restricted field fails", []string{"production"}, "", false},
		{"labeled member passes", []string{"production", "staging"}, "production", true},
		{"labeled non-member fails", []string{"production"}, "dev", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := scopeFieldPasses(c.allowList, c.value); got != c.want {
				t.Errorf("scopeFieldPasses(%v, %q) = %v, want %v", c.allowList, c.value, got, c.want)
			}
		})
	}
}

// TestHasMetadataAccess_ScopeEnforced exercises the metadata axis end-to-end
// through the public method the list filter calls, and asserts the
// scope-denied counter: exactly one observation per DENIED decision that the
// metadata axis caused (not one per field), and none for an allowed decision
// or a denial no rule would have granted anyway.
func TestHasMetadataAccess_ScopeEnforced(t *testing.T) {
	t.Parallel()

	envRule := &RBACConfig{Groups: []GroupRule{{
		Name:         "ops",
		Tenants:      []string{"svc-meta"},
		Permissions:  []Permission{PermWrite},
		Environments: []string{"production"},
	}}}

	t.Run("unlabeled tenant hidden, one scope-denied", func(t *testing.T) {
		m := NewForTest(envRule)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.HasMetadataAccess([]string{"ops"}, "svc-meta", "", "") {
			t.Error("unlabeled tenant on a restricted field must be denied")
		}
		if got := rec.counts[scopeAxisMetadata]; got != 1 {
			t.Errorf("want 1 scope-denied on %q, got %d", scopeAxisMetadata, got)
		}
		if got := rec.counts[scopeAxisOrg]; got != 0 {
			t.Errorf("org axis did not deny; want 0 on %q, got %d", scopeAxisOrg, got)
		}
	})

	t.Run("labeled out-of-scope tenant hidden, one scope-denied", func(t *testing.T) {
		m := NewForTest(envRule)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.HasMetadataAccess([]string{"ops"}, "svc-meta", "staging", "") {
			t.Error("labeled tenant outside the rule's environments must be denied")
		}
		if got := rec.counts[scopeAxisMetadata]; got != 1 {
			t.Errorf("want 1 scope-denied, got %d", got)
		}
	})

	t.Run("labeled matching tenant visible, no scope-denied", func(t *testing.T) {
		m := NewForTest(envRule)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if !m.HasMetadataAccess([]string{"ops"}, "svc-meta", "production", "") {
			t.Error("labeled matching tenant must stay visible")
		}
		if len(rec.counts) != 0 {
			t.Errorf("an allowed decision must record nothing, got %v", rec.counts)
		}
	})

	t.Run("denial no rule would have granted records nothing", func(t *testing.T) {
		m := NewForTest(envRule)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.HasMetadataAccess([]string{"ops"}, "other-tenant", "", "") {
			t.Error("tenant outside the rule's pattern must be denied")
		}
		if m.HasMetadataAccess([]string{"stranger"}, "svc-meta", "", "") {
			t.Error("caller matching no rule must be denied")
		}
		if len(rec.counts) != 0 {
			t.Errorf("a denial not caused by a scope axis must record nothing, got %v", rec.counts)
		}
	})

	// Two restricted fields, both unlabeled → exactly ONE observation for the
	// decision (not two, one per field).
	t.Run("two restricted fields both unlabeled record once", func(t *testing.T) {
		cfg := &RBACConfig{Groups: []GroupRule{{
			Name:         "ops",
			Tenants:      []string{"svc-meta"},
			Permissions:  []Permission{PermWrite},
			Environments: []string{"production"},
			Domains:      []string{"finance"},
		}}}
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.HasMetadataAccess([]string{"ops"}, "svc-meta", "", "") {
			t.Error("unlabeled tenant must be denied")
		}
		if got := rec.counts[scopeAxisMetadata]; got != 1 {
			t.Errorf("two-field unlabeled: want exactly 1 scope-denied (per decision), got %d", got)
		}
	})

	// Two co-matching restricted rules both cut by metadata → still ONE
	// observation for the decision.
	t.Run("two blocked rules record once per decision", func(t *testing.T) {
		cfg := &RBACConfig{Groups: []GroupRule{
			{Name: "ops", Tenants: []string{"svc-meta"}, Permissions: []Permission{PermRead}, Environments: []string{"production"}},
			{Name: "ops", Tenants: []string{"svc-*"}, Permissions: []Permission{PermRead}, Domains: []string{"finance"}},
		}}
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.HasMetadataAccess([]string{"ops"}, "svc-meta", "", "") {
			t.Error("unlabeled tenant must be denied by both rules")
		}
		if got := rec.counts[scopeAxisMetadata]; got != 1 {
			t.Errorf("want exactly 1 scope-denied for one decision, got %d", got)
		}
	})

	// A wildcard rule grants the unlabeled tenant, so the decision is ALLOWED
	// → zero scope-denied even though a co-matching restricted rule fails.
	t.Run("wildcard rule rescues unlabeled tenant: no scope-denied", func(t *testing.T) {
		cfg := &RBACConfig{Groups: []GroupRule{
			{Name: "ops", Tenants: []string{"svc-meta"}, Permissions: []Permission{PermWrite}, Environments: []string{"production"}},
			{Name: "ops", Tenants: []string{"svc-meta"}, Permissions: []Permission{PermWrite}}, // wildcard env/domain
		}}
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if !m.HasMetadataAccess([]string{"ops"}, "svc-meta", "", "") {
			t.Error("wildcard rule must keep tenant visible")
		}
		if len(rec.counts) != 0 {
			t.Errorf("wildcard-rescued tenant must record nothing, got %v", rec.counts)
		}
	})

	t.Run("nil recorder does not panic", func(t *testing.T) {
		m := NewForTest(envRule) // scopeAudit nil
		if m.HasMetadataAccess([]string{"ops"}, "svc-meta", "", "") {
			t.Error("nil recorder: unlabeled tenant must still be denied")
		}
	})
}
