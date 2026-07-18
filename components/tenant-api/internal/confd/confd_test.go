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

// TestValidatorScannerInvariant is the anti-drift guard: for any id, the
// validator's accept decision (does {id}.yaml name a tenant file?) must equal
// what a scanner would pick up. This is the property that stops a caller from
// writing a control file — if it ever fails, the write namespace has diverged
// from the scanned one.
func TestValidatorScannerInvariant(t *testing.T) {
	t.Parallel()
	ids := []string{"db-a", "_domain_policy", "_rbac", "_", ".git", "tenant_123"}
	for _, id := range ids {
		accepted := IsTenantConfigFile(id + ".yaml")
		_, scanned := TenantIDFromFile(id + ".yaml")
		if accepted != scanned {
			t.Errorf("id %q: validator-accepts=%v but scanner-picks-up=%v (namespaces diverged)",
				id, accepted, scanned)
		}
	}
}
