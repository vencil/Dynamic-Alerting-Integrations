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

// PlatformUnrestricted at write level (#2405): the bar for replacing a tenant
// file that cannot be parsed. Only a literal "*" rule granting write (or
// admin) with no org / environment / domain restriction passes — in shadow
// mode too; a prefix pattern is not all tenants, and read-only is not write.
func TestPlatformUnrestricted_Write(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{Groups: []GroupRule{
		{Name: "platform-writers", Tenants: []string{"*"}, Permissions: []Permission{PermRead, PermWrite}},
		{Name: "platform-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
		{Name: "staging-writers", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, Environments: []string{"staging"}},
		{Name: "finance-writers", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, Domains: []string{"finance"}},
		{Name: "org-writers", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, OrgScope: "org"},
		{Name: "auditors", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		{Name: "prefix-writers", Tenants: []string{"svc-*"}, Permissions: []Permission{PermWrite}},
		{Name: "listed-writers", Tenants: []string{"svc-alpha", "svc-beta"}, Permissions: []Permission{PermWrite}},
	}})
	m.EnableOrgScopeEnforce() // flags must not matter; also checked in shadow below
	shadow := NewForTest(m.Get())
	for _, c := range []struct {
		group string
		want  bool
	}{
		{"platform-writers", true},
		{"platform-admins", true},
		{"staging-writers", false},
		{"finance-writers", false},
		{"org-writers", false},
		{"auditors", false},
		{"prefix-writers", false},
		{"listed-writers", false},
	} {
		p := &VerifiedPrincipal{Groups: []string{c.group}}
		if got := m.PlatformUnrestricted(p, PermWrite); got != c.want {
			t.Errorf("%s: PlatformUnrestricted(write) = %v, want %v", c.group, got, c.want)
		}
		if got := shadow.PlatformUnrestricted(p, PermWrite); got != c.want {
			t.Errorf("%s (shadow): PlatformUnrestricted(write) = %v, want %v", c.group, got, c.want)
		}
	}
	if NewForTest(&RBACConfig{}).PlatformUnrestricted(&VerifiedPrincipal{Groups: []string{"platform-writers"}}, PermWrite) {
		t.Error("zero-group (open) config must not grant write on all tenants")
	}
}
