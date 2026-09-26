package rbac

import "testing"

// PlatformAdminUnrestricted (#1988): only an admin rule with no org,
// environment or domain restriction passes. PlatformAdminNonOrgScoped lets
// the metadata-restricted admins through; this one must not.
func TestPlatformAdminUnrestricted_Bar(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{Groups: []GroupRule{
		{Name: "platform-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
		{Name: "staging-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}, Environments: []string{"staging"}},
		{Name: "finance-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}, Domains: []string{"finance"}},
		{Name: "org-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}, OrgScope: "org"},
		{Name: "auditors", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		{Name: "team-admins", Tenants: []string{"db-*"}, Permissions: []Permission{PermAdmin}},
	}})
	cases := []struct {
		group        string
		unrestricted bool
		nonOrgScoped bool // the sibling bar, for contrast
	}{
		{"platform-admins", true, true},
		{"staging-admins", false, true},
		{"finance-admins", false, true},
		{"org-admins", false, false},
		{"auditors", false, false},
		{"team-admins", false, false},
	}
	for _, c := range cases {
		p := &VerifiedPrincipal{Groups: []string{c.group}}
		if got := m.PlatformAdminUnrestricted(p); got != c.unrestricted {
			t.Errorf("%s: PlatformAdminUnrestricted = %v, want %v", c.group, got, c.unrestricted)
		}
		if got := m.PlatformAdminNonOrgScoped(p); got != c.nonOrgScoped {
			t.Errorf("%s: PlatformAdminNonOrgScoped = %v, want %v (contrast)", c.group, got, c.nonOrgScoped)
		}
	}
	if m.PlatformAdminUnrestricted(&VerifiedPrincipal{}) {
		t.Error("a caller matching no rule passed")
	}
	if NewForTest(&RBACConfig{}).PlatformAdminUnrestricted(&VerifiedPrincipal{Groups: []string{"platform-admins"}}) {
		t.Error("zero-group config must return false, like PlatformAdminNonOrgScoped")
	}
}
