package rbac

import (
	"reflect"
	"testing"
)

// ADR-027 §9 item 7: the judgement behind `tenant-api config check --kind
// scope`. The command's end-to-end fixture is cmd/server/scope_check_test.go.

func TestUnlabeledScopeGaps(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "envs", Tenants: []string{"tenant-*"}, Environments: []string{"production"}},
		{Name: "domains", Tenants: []string{"tenant-*"}, Domains: []string{"finance"}},
		{Name: "orgs", Tenants: []string{"tenant-alpha", "tenant-b*"}, OrgScope: "org"},
		{Name: "open", Tenants: []string{"*"}},
	}}
	tenants := []TenantLabels{
		// Labeled everywhere it is restricted; a label outside the
		// allow-list is not "unlabeled".
		{ID: "tenant-alpha", Environment: "staging", Domain: "finance", Orgs: []string{"ORG-1"}},
		// Unlabeled on every axis.
		{ID: "tenant-bravo"},
		// Metadata unknown: every rule restricting a metadata axis is a
		// metadata_unknown gap (the server does not grant it); the org axis
		// is judged as usual.
		{ID: "tenant-bxx", MetadataUnknown: true, Orgs: []string{}},
		// No scoped rule's pattern matches it.
		{ID: "other-zulu"},
	}
	got := cfg.UnlabeledScopeGaps(tenants)
	want := []ScopeGap{
		{Axis: ScopeAxisEnvironment, Rule: "envs", RuleIndex: 0, Tenant: "tenant-bravo", Reason: GapReasonUnlabeled},
		{Axis: ScopeAxisEnvironment, Rule: "envs", RuleIndex: 0, Tenant: "tenant-bxx", Reason: GapReasonMetadataUnknown},
		{Axis: ScopeAxisDomain, Rule: "domains", RuleIndex: 1, Tenant: "tenant-bravo", Reason: GapReasonUnlabeled},
		{Axis: ScopeAxisDomain, Rule: "domains", RuleIndex: 1, Tenant: "tenant-bxx", Reason: GapReasonMetadataUnknown},
		{Axis: ScopeAxisOrg, Rule: "orgs", RuleIndex: 2, Tenant: "tenant-bravo", Reason: GapReasonUnlabeled},
		{Axis: ScopeAxisOrg, Rule: "orgs", RuleIndex: 2, Tenant: "tenant-bxx", Reason: GapReasonUnlabeled},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("UnlabeledScopeGaps:\n got %+v\nwant %+v", got, want)
	}
}

// The order is rule index, axis (environment, domain, org), tenant id —
// whatever order the tenants come in.
func TestUnlabeledScopeGaps_SortedWhateverTheInputOrder(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "both", Tenants: []string{"*"}, Environments: []string{"x"}, Domains: []string{"y"}, OrgScope: "org"},
	}}
	tenants := []TenantLabels{{ID: "tenant-charlie"}, {ID: "tenant-alpha"}}
	got := cfg.UnlabeledScopeGaps(tenants)
	want := []ScopeGap{
		{Axis: ScopeAxisEnvironment, Rule: "both", Tenant: "tenant-alpha", Reason: GapReasonUnlabeled},
		{Axis: ScopeAxisEnvironment, Rule: "both", Tenant: "tenant-charlie", Reason: GapReasonUnlabeled},
		{Axis: ScopeAxisDomain, Rule: "both", Tenant: "tenant-alpha", Reason: GapReasonUnlabeled},
		{Axis: ScopeAxisDomain, Rule: "both", Tenant: "tenant-charlie", Reason: GapReasonUnlabeled},
		{Axis: ScopeAxisOrg, Rule: "both", Tenant: "tenant-alpha", Reason: GapReasonUnlabeled},
		{Axis: ScopeAxisOrg, Rule: "both", Tenant: "tenant-charlie", Reason: GapReasonUnlabeled},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("UnlabeledScopeGaps:\n got %+v\nwant %+v", got, want)
	}
}

func TestUnlabeledScopeGaps_NoRestrictionNoGap(t *testing.T) {
	t.Parallel()
	for _, cfg := range []*RBACConfig{nil, {}, {Groups: []GroupRule{{Name: "open", Tenants: []string{"*"}}}}} {
		if got := cfg.UnlabeledScopeGaps([]TenantLabels{{ID: "tenant-alpha"}}); len(got) != 0 {
			t.Errorf("config %+v: gaps %+v, want none", cfg, got)
		}
	}
}
