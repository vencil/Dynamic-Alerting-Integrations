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

// TestIsAddressableTenantIDRejectsSeparators pins the gap this predicate
// exists for: IsTenantConfigFile answers a NAME question and says yes to ids
// carrying separators, so a write site gating on it alone builds a path it did
// not mean to (#1681).
func TestIsAddressableTenantIDRejectsSeparators(t *testing.T) {
	for _, tc := range []struct {
		id   string
		want bool
	}{
		{"db-a", true},
		{"Upper", true},
		{"", false},
		{"a/b", false},
		{`a\b`, false},
		{"..", false},
		{"../x", false},
		{"a/../../b", false},
		{"/abs", false},
		{"_defaults", false},
		{".hidden", false},
	} {
		if got := IsAddressableTenantID(tc.id); got != tc.want {
			t.Errorf("IsAddressableTenantID(%q) = %v, want %v", tc.id, got, tc.want)
		}
		// Every id this predicate accepts must also satisfy the name rule the
		// scanners skip on — the two namespaces stay structurally equal.
		if tc.want && !IsTenantConfigFile(tc.id+".yaml") {
			t.Errorf("%q is addressable but would not be scanned", tc.id)
		}
	}
}
