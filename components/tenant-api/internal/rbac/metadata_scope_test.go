package rbac

// Metadata (environment/domain) axis: the pure matchers (metadataMatches /
// scopeFieldModes), the HasMetadataAccess restriction matrix, the accessible
// env/domain union sets, and the shadow/enforce scope-mode behavior with
// per-tenant would-deny recording (ADR-027 / LD-6 P1, Option Y).
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
		// Empty value on a NON-empty allow-list is no longer a match: the
		// "unlabeled tenant" fail-open moved out of metadataMatches into the
		// mode-aware scopeFieldModes (ADR-027/LD-6 P1). metadataMatches is now
		// pure exact-membership. The shadow/enforce behavior is covered by
		// TestScopeFieldModes / TestHasMetadataAccess_ScopeMode below.
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
		// Open mode (no groups configured)
		{"open mode allows any metadata",
			&RBACConfig{Groups: []GroupRule{}},
			[]string{"any-group"}, "any-tenant", "production", "finance", true},
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
		// Empty metadata in tenant (should pass)
		{"empty environment passes despite restriction",
			rules(GroupRule{Name: "ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}, Environments: []string{"production"}, Domains: []string{}}),
			[]string{"ops"}, "db-a", "", "", true},
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
		{"open mode returns nil",
			&RBACConfig{Groups: []GroupRule{}}, []string{"any"}, nil},
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
		{"user in no matching groups returns nil",
			rules(GroupRule{Name: "prod-ops", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, Environments: []string{"production"}}),
			[]string{"other-group"}, nil},
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
		{"open mode returns nil",
			&RBACConfig{Groups: []GroupRule{}}, []string{"any"}, nil},
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

// TestScopeFieldModes pins the pure two-mode field evaluator: wildcard passes
// both modes, an unlabeled value passes shadow but not enforce, and a labeled
// value is identical exact-membership in both modes.
func TestScopeFieldModes(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name        string
		allowList   []string
		value       string
		wantShadow  bool
		wantEnforce bool
	}{
		{"empty allow-list is wildcard", nil, "production", true, true},
		{"empty allow-list wildcard even for empty value", nil, "", true, true},
		{"unlabeled on restricted: shadow yes, enforce no", []string{"production"}, "", true, false},
		{"labeled member: both yes", []string{"production", "staging"}, "production", true, true},
		{"labeled non-member: both no", []string{"production"}, "dev", false, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			gotShadow, gotEnforce := scopeFieldModes(c.allowList, c.value)
			if gotShadow != c.wantShadow || gotEnforce != c.wantEnforce {
				t.Errorf("scopeFieldModes(%v, %q) = (%v, %v), want (%v, %v)",
					c.allowList, c.value, gotShadow, gotEnforce, c.wantShadow, c.wantEnforce)
			}
		})
	}
}

// TestHasMetadataAccess_ScopeMode exercises the mode end-to-end through the
// public method the list filter calls, and asserts the would-deny counter is
// PER-TENANT (Option Y): one observation per would-be-hidden tenant, not per
// field-check, and zero when another rule grants access under strict semantics.
func TestHasMetadataAccess_ScopeMode(t *testing.T) {
	t.Parallel()

	envRule := &RBACConfig{Groups: []GroupRule{{
		Name:         "ops",
		Tenants:      []string{"db-a"},
		Permissions:  []Permission{PermWrite},
		Environments: []string{"production"},
	}}}

	t.Run("shadow: unlabeled tenant stays visible, one would-deny", func(t *testing.T) {
		m := NewForTest(envRule)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if !m.HasMetadataAccess([]string{"ops"}, "db-a", "", "") {
			t.Error("shadow: unlabeled tenant must remain accessible (byte-identical to legacy)")
		}
		if got := rec.counts[scopeAxisMetadata]; got != 1 {
			t.Errorf("shadow: want 1 would-deny, got %d", got)
		}
	})

	t.Run("enforce: unlabeled tenant hidden, one would-deny", func(t *testing.T) {
		m := NewForTest(envRule)
		m.EnableMetadataScopeEnforce()
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.HasMetadataAccess([]string{"ops"}, "db-a", "", "") {
			t.Error("enforce: unlabeled tenant must be denied")
		}
		if got := rec.counts[scopeAxisMetadata]; got != 1 {
			t.Errorf("enforce: want 1 would-deny, got %d", got)
		}
	})

	t.Run("labeled matching tenant unaffected by mode, no would-deny", func(t *testing.T) {
		for _, enforce := range []bool{false, true} {
			m := NewForTest(envRule)
			if enforce {
				m.EnableMetadataScopeEnforce()
			}
			rec := newFakeScopeRecorder()
			m.SetScopeAuditor(rec)
			if !m.HasMetadataAccess([]string{"ops"}, "db-a", "production", "") {
				t.Errorf("enforce=%v: labeled matching tenant must stay visible", enforce)
			}
			if got := rec.counts[scopeAxisMetadata]; got != 0 {
				t.Errorf("enforce=%v: labeled tenant must not record would-deny, got %d", enforce, got)
			}
		}
	})

	// Option Y fix: two restricted fields, both unlabeled → exactly ONE
	// observation for the tenant (not two, one per field).
	t.Run("two restricted fields both unlabeled record once", func(t *testing.T) {
		cfg := &RBACConfig{Groups: []GroupRule{{
			Name:         "ops",
			Tenants:      []string{"db-a"},
			Permissions:  []Permission{PermWrite},
			Environments: []string{"production"},
			Domains:      []string{"finance"},
		}}}
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if !m.HasMetadataAccess([]string{"ops"}, "db-a", "", "") {
			t.Error("shadow: unlabeled tenant must remain visible")
		}
		if got := rec.counts[scopeAxisMetadata]; got != 1 {
			t.Errorf("two-field unlabeled: want exactly 1 would-deny (per-tenant), got %d", got)
		}
	})

	// Option Y fix: a wildcard rule grants access under strict semantics, so the
	// tenant is NOT would-be-hidden → zero would-deny even though a co-matching
	// restricted rule would deny the unlabeled field. Prevents a stuck counter.
	t.Run("wildcard rule rescues unlabeled tenant: no would-deny", func(t *testing.T) {
		cfg := &RBACConfig{Groups: []GroupRule{
			{Name: "ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}, Environments: []string{"production"}},
			{Name: "ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}}, // wildcard env/domain
		}}
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		for _, enforce := range []bool{false, true} {
			m.metadataScopeEnforce = enforce
			if !m.HasMetadataAccess([]string{"ops"}, "db-a", "", "") {
				t.Errorf("enforce=%v: wildcard rule must keep tenant visible", enforce)
			}
		}
		if got := rec.counts[scopeAxisMetadata]; got != 0 {
			t.Errorf("wildcard-rescued tenant must record 0 would-deny, got %d", got)
		}
	})

	t.Run("nil recorder does not panic", func(t *testing.T) {
		m := NewForTest(envRule) // scopeAudit nil
		if !m.HasMetadataAccess([]string{"ops"}, "db-a", "", "") {
			t.Error("shadow with nil recorder must still allow")
		}
	})
}
