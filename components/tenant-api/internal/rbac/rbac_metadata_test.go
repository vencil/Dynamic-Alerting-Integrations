package rbac

import (
	"testing"
)

func TestMetadataMatches(t *testing.T) {
	tests := []struct {
		name      string
		allowList []string
		value     string
		want      bool
	}{
		// Empty allowList = wildcard (all values allowed)
		{
			name:      "empty allowList matches any value",
			allowList: []string{},
			value:     "production",
			want:      true,
		},
		{
			name:      "empty allowList matches empty value",
			allowList: []string{},
			value:     "",
			want:      true,
		},
		// Empty value always matches (tenant has no metadata)
		{
			name:      "empty value matches despite non-empty allowList",
			allowList: []string{"production", "staging"},
			value:     "",
			want:      true,
		},
		// Exact match cases
		{
			name:      "exact match in allowList",
			allowList: []string{"production", "staging", "dev"},
			value:     "production",
			want:      true,
		},
		{
			name:      "exact match with single entry",
			allowList: []string{"finance"},
			value:     "finance",
			want:      true,
		},
		// No match cases
		{
			name:      "value not in allowList",
			allowList: []string{"production", "staging"},
			value:     "dev",
			want:      false,
		},
		{
			name:      "single value not in single allowList entry",
			allowList: []string{"production"},
			value:     "staging",
			want:      false,
		},
		// Multiple entries in allowList
		{
			name:      "matches second entry in allowList",
			allowList: []string{"production", "staging", "dev"},
			value:     "staging",
			want:      true,
		},
		{
			name:      "matches last entry in allowList",
			allowList: []string{"production", "staging", "dev"},
			value:     "dev",
			want:      true,
		},
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

func TestHasMetadataAccess(t *testing.T) {
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
		{
			name:        "open mode allows any metadata",
			config:      &RBACConfig{Groups: []GroupRule{}},
			idpGroups:   []string{"any-group"},
			tenantID:    "any-tenant",
			environment: "production",
			domain:      "finance",
			want:        true,
		},
		// Wildcard rules (no environment/domain restrictions)
		{
			name: "wildcard environments and domains",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "admins",
						Tenants:      []string{"*"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{},
						Domains:      []string{},
					},
				},
			},
			idpGroups:   []string{"admins"},
			tenantID:    "db-a",
			environment: "production",
			domain:      "finance",
			want:        true,
		},
		// Environment-restricted rule
		{
			name: "environment-restricted, matching",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "prod-ops",
						Tenants:      []string{"db-a-*"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"production", "staging"},
						Domains:      []string{},
					},
				},
			},
			idpGroups:   []string{"prod-ops"},
			tenantID:    "db-a-primary",
			environment: "production",
			domain:      "any",
			want:        true,
		},
		{
			name: "environment-restricted, not matching",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "prod-ops",
						Tenants:      []string{"db-a-*"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"production", "staging"},
						Domains:      []string{},
					},
				},
			},
			idpGroups:   []string{"prod-ops"},
			tenantID:    "db-a-primary",
			environment: "dev",
			domain:      "any",
			want:        false,
		},
		// Domain-restricted rule
		{
			name: "domain-restricted, matching",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "finance-ops",
						Tenants:      []string{"db-a"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{},
						Domains:      []string{"finance", "compliance"},
					},
				},
			},
			idpGroups:   []string{"finance-ops"},
			tenantID:    "db-a",
			environment: "any",
			domain:      "finance",
			want:        true,
		},
		{
			name: "domain-restricted, not matching",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "finance-ops",
						Tenants:      []string{"db-a"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{},
						Domains:      []string{"finance", "compliance"},
					},
				},
			},
			idpGroups:   []string{"finance-ops"},
			tenantID:    "db-a",
			environment: "any",
			domain:      "ecommerce",
			want:        false,
		},
		// Both environment and domain restricted
		{
			name: "both environment and domain, both match",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "finance-prod-ops",
						Tenants:      []string{"db-a", "db-b"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"production", "staging"},
						Domains:      []string{"finance", "compliance"},
					},
				},
			},
			idpGroups:   []string{"finance-prod-ops"},
			tenantID:    "db-a",
			environment: "production",
			domain:      "finance",
			want:        true,
		},
		{
			name: "both environment and domain, environment fails",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "finance-prod-ops",
						Tenants:      []string{"db-a", "db-b"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"production", "staging"},
						Domains:      []string{"finance", "compliance"},
					},
				},
			},
			idpGroups:   []string{"finance-prod-ops"},
			tenantID:    "db-a",
			environment: "dev",
			domain:      "finance",
			want:        false,
		},
		{
			name: "both environment and domain, domain fails",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "finance-prod-ops",
						Tenants:      []string{"db-a", "db-b"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"production", "staging"},
						Domains:      []string{"finance", "compliance"},
					},
				},
			},
			idpGroups:   []string{"finance-prod-ops"},
			tenantID:    "db-a",
			environment: "production",
			domain:      "ecommerce",
			want:        false,
		},
		// Empty metadata in tenant (should pass)
		{
			name: "empty environment passes despite restriction",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "ops",
						Tenants:      []string{"db-a"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"production"},
						Domains:      []string{},
					},
				},
			},
			idpGroups:   []string{"ops"},
			tenantID:    "db-a",
			environment: "",
			domain:      "",
			want:        true,
		},
		// Tenant doesn't match rule
		{
			name: "tenant doesn't match pattern",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "db-a-ops",
						Tenants:      []string{"db-a-*"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"production"},
						Domains:      []string{},
					},
				},
			},
			idpGroups:   []string{"db-a-ops"},
			tenantID:    "db-b-primary",
			environment: "production",
			domain:      "any",
			want:        false,
		},
		// User not in matching group
		{
			name: "user not in any matching group",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "prod-ops",
						Tenants:      []string{"*"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"production"},
						Domains:      []string{},
					},
				},
			},
			idpGroups:   []string{"other-group"},
			tenantID:    "db-a",
			environment: "production",
			domain:      "any",
			want:        false,
		},
		// Multiple groups, one matches
		{
			name: "multiple groups, second matches metadata",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "finance-ops",
						Tenants:      []string{"db-a"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"dev"},
						Domains:      []string{},
					},
					{
						Name:         "prod-ops",
						Tenants:      []string{"db-a"},
						Permissions:  []Permission{PermRead},
						Environments: []string{"production"},
						Domains:      []string{},
					},
				},
			},
			idpGroups:   []string{"finance-ops", "prod-ops"},
			tenantID:    "db-a",
			environment: "production",
			domain:      "any",
			want:        true,
		},
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
	tests := []struct {
		name      string
		config    *RBACConfig
		idpGroups []string
		want      []string
	}{
		// Open mode
		{
			name:      "open mode returns nil",
			config:    &RBACConfig{Groups: []GroupRule{}},
			idpGroups: []string{"any"},
			want:      nil,
		},
		// Wildcard rule (no restrictions)
		{
			name: "wildcard environments rule returns nil",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "admins",
						Tenants:      []string{"*"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{},
					},
				},
			},
			idpGroups: []string{"admins"},
			want:      nil,
		},
		// Single group with restrictions
		{
			name: "single group with restricted environments",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "prod-ops",
						Tenants:      []string{"*"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"production", "staging"},
					},
				},
			},
			idpGroups: []string{"prod-ops"},
			want:      []string{"production", "staging"},
		},
		// Multiple groups with different restrictions
		{
			name: "multiple groups union environments",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "prod-ops",
						Tenants:      []string{"*"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"production", "staging"},
					},
					{
						Name:         "dev-ops",
						Tenants:      []string{"*"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"dev", "local"},
					},
				},
			},
			idpGroups: []string{"prod-ops", "dev-ops"},
			want:      []string{"production", "staging", "dev", "local"},
		},
		// One group has wildcard (includes all others)
		{
			name: "one group wildcard with others restricted returns nil",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "admins",
						Tenants:      []string{"*"},
						Permissions:  []Permission{PermAdmin},
						Environments: []string{},
					},
					{
						Name:         "prod-ops",
						Tenants:      []string{"*"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"production"},
					},
				},
			},
			idpGroups: []string{"admins", "prod-ops"},
			want:      nil,
		},
		// User not in any group
		{
			name: "user in no matching groups returns nil",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "prod-ops",
						Tenants:      []string{"*"},
						Permissions:  []Permission{PermWrite},
						Environments: []string{"production"},
					},
				},
			},
			idpGroups: []string{"other-group"},
			want:      nil,
		},
		// Duplicate environments across groups (dedup)
		{
			name: "duplicate environments are deduped",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:         "group1",
						Tenants:      []string{"*"},
						Permissions:  []Permission{PermRead},
						Environments: []string{"production", "staging"},
					},
					{
						Name:         "group2",
						Tenants:      []string{"*"},
						Permissions:  []Permission{PermRead},
						Environments: []string{"production", "dev"},
					},
				},
			},
			idpGroups: []string{"group1", "group2"},
			want:      []string{"production", "staging", "dev"},
		},
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
	tests := []struct {
		name      string
		config    *RBACConfig
		idpGroups []string
		want      []string
	}{
		// Open mode
		{
			name:      "open mode returns nil",
			config:    &RBACConfig{Groups: []GroupRule{}},
			idpGroups: []string{"any"},
			want:      nil,
		},
		// Wildcard rule (no restrictions)
		{
			name: "wildcard domains rule returns nil",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:    "admins",
						Tenants: []string{"*"},
						Domains: []string{},
					},
				},
			},
			idpGroups: []string{"admins"},
			want:      nil,
		},
		// Single group with restrictions
		{
			name: "single group with restricted domains",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:    "finance-ops",
						Tenants: []string{"*"},
						Domains: []string{"finance", "compliance"},
					},
				},
			},
			idpGroups: []string{"finance-ops"},
			want:      []string{"finance", "compliance"},
		},
		// Multiple groups with different restrictions
		{
			name: "multiple groups union domains",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:    "finance-ops",
						Tenants: []string{"*"},
						Domains: []string{"finance", "compliance"},
					},
					{
						Name:    "ecommerce-ops",
						Tenants: []string{"*"},
						Domains: []string{"ecommerce", "operations"},
					},
				},
			},
			idpGroups: []string{"finance-ops", "ecommerce-ops"},
			want:      []string{"finance", "compliance", "ecommerce", "operations"},
		},
		// One group has wildcard (includes all others)
		{
			name: "one group wildcard with others restricted returns nil",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:    "admins",
						Tenants: []string{"*"},
						Domains: []string{},
					},
					{
						Name:    "finance-ops",
						Tenants: []string{"*"},
						Domains: []string{"finance"},
					},
				},
			},
			idpGroups: []string{"admins", "finance-ops"},
			want:      nil,
		},
		// Duplicate domains across groups (dedup)
		{
			name: "duplicate domains are deduped",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:    "group1",
						Tenants: []string{"*"},
						Domains: []string{"finance", "compliance"},
					},
					{
						Name:    "group2",
						Tenants: []string{"*"},
						Domains: []string{"finance", "operations"},
					},
				},
			},
			idpGroups: []string{"group1", "group2"},
			want:      []string{"finance", "compliance", "operations"},
		},
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

func TestHasMetadataAccess_BackwardCompatible(t *testing.T) {
	// Test that rules without Environments/Domains (old format) work as before
	tests := []struct {
		name      string
		config    *RBACConfig
		idpGroups []string
		tenantID  string
		env       string
		domain    string
		want      bool
	}{
		{
			name: "old rule without fields behaves as wildcard",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:        "ops",
						Tenants:     []string{"db-a"},
						Permissions: []Permission{PermWrite},
						// No Environments or Domains field
					},
				},
			},
			idpGroups: []string{"ops"},
			tenantID:  "db-a",
			env:       "production",
			domain:    "finance",
			want:      true,
		},
		{
			name: "old rule allows any environment",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:        "ops",
						Tenants:     []string{"db-a"},
						Permissions: []Permission{PermWrite},
					},
				},
			},
			idpGroups: []string{"ops"},
			tenantID:  "db-a",
			env:       "dev",
			domain:    "any",
			want:      true,
		},
		{
			name: "old rule allows any domain",
			config: &RBACConfig{
				Groups: []GroupRule{
					{
						Name:        "ops",
						Tenants:     []string{"db-a"},
						Permissions: []Permission{PermWrite},
					},
				},
			},
			idpGroups: []string{"ops"},
			tenantID:  "db-a",
			env:       "any",
			domain:    "newdomain",
			want:      true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			m := NewForTest(tt.config)

			got := m.HasMetadataAccess(tt.idpGroups, tt.tenantID, tt.env, tt.domain)
			if got != tt.want {
				t.Errorf("HasMetadataAccess(%v, %q, %q, %q) = %v, want %v",
					tt.idpGroups, tt.tenantID, tt.env, tt.domain, got, tt.want)
			}
		})
	}
}

// toSet converts a slice to a map for order-independent comparison
func toSet(s []string) map[string]bool {
	m := make(map[string]bool)
	for _, v := range s {
		m[v] = true
	}
	return m
}
