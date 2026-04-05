package rbac

import "testing"

func TestTenantMatches(t *testing.T) {
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
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := tenantMatches(tt.patterns, tt.tenantID); got != tt.want {
				t.Errorf("tenantMatches(%v, %q) = %v, want %v", tt.patterns, tt.tenantID, got, tt.want)
			}
		})
	}
}

func TestPermCovers(t *testing.T) {
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
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := permCovers(tt.grant, tt.want); got != tt.ok {
				t.Errorf("permCovers(%s, %s) = %v, want %v", tt.grant, tt.want, got, tt.ok)
			}
		})
	}
}

func TestHasPermission(t *testing.T) {
	m := &Manager{}
	m.value.Store(&RBACConfig{
		Groups: []GroupRule{
			{Name: "platform-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
			{Name: "db-ops", Tenants: []string{"db-a-*", "db-b-*"}, Permissions: []Permission{PermRead, PermWrite}},
			{Name: "viewers", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		},
	})

	tests := []struct {
		name     string
		groups   []string
		tenant   string
		want     Permission
		expected bool
	}{
		{"admin can write any", []string{"platform-admins"}, "any", PermWrite, true},
		{"db-ops can write db-a tenant", []string{"db-ops"}, "db-a-prod", PermWrite, true},
		{"db-ops cannot write other tenant", []string{"db-ops"}, "redis-01", PermWrite, false},
		{"db-ops can read db-a tenant", []string{"db-ops"}, "db-a-staging", PermRead, true},
		{"viewer can read any", []string{"viewers"}, "any-tenant", PermRead, true},
		{"viewer cannot write", []string{"viewers"}, "any-tenant", PermWrite, false},
		{"multi-group uses best match", []string{"viewers", "db-ops"}, "db-a-prod", PermWrite, true},
		{"unknown group denied", []string{"unknown"}, "any", PermRead, false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := m.HasPermission(tt.groups, tt.tenant, tt.want); got != tt.expected {
				t.Errorf("HasPermission(%v, %q, %s) = %v, want %v",
					tt.groups, tt.tenant, tt.want, got, tt.expected)
			}
		})
	}
}

func TestOpenModeReadOnly(t *testing.T) {
	// Empty config (open mode) allows read, denies write
	m := &Manager{}
	m.value.Store(&RBACConfig{})

	if !m.HasPermission([]string{"any"}, "any-tenant", PermRead) {
		t.Error("open mode should allow read")
	}
	if m.HasPermission([]string{"any"}, "any-tenant", PermWrite) {
		t.Error("open mode should deny write")
	}
}
