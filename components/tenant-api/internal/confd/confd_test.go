package confd

import "testing"

func TestTenantIDFromFile(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name   string
		wantID string
		wantOK bool
	}{
		// Tenant config files
		{"db-a.yaml", "db-a", true},
		{"prod-mariadb-01.yml", "prod-mariadb-01", true},
		{"tenant_123.yaml", "tenant_123", true}, // mid-string underscore is fine

		// Reserved control files ("_" prefix) — every scanner must skip these
		{"_defaults.yaml", "", false},
		{"_rbac.yaml", "", false},
		{"_domain_policy.yaml", "", false},
		{"_routing_profiles.yaml", "", false},
		{"_.yaml", "", false},

		// Hidden / VCS files ("." prefix)
		{".hidden.yaml", "", false},
		{".gitkeep", "", false},

		// Non-YAML
		{"README.md", "", false},
		{"db-a", "", false}, // no extension
		{"db-a.json", "", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			id, ok := TenantIDFromFile(tt.name)
			if ok != tt.wantOK || id != tt.wantID {
				t.Errorf("TenantIDFromFile(%q) = (%q, %v), want (%q, %v)",
					tt.name, id, ok, tt.wantID, tt.wantOK)
			}
		})
	}
}
