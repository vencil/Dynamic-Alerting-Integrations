package rbac

// ScopeAllowedUnknownMetadata — the list-plane visibility decision for a
// DEGRADED row (#1680): a tenant whose conf.d file exists but is unusable, so
// its environment/domain are UNKNOWN. The load-bearing difference from
// ScopeAllowed(p, id, "", "", orgs) is that unknown is not unlabeled: a rule
// grants here only if it places no restriction on either metadata axis, and
// the metadata axis records nothing (a broken file is an incident state, not a
// labeling gap). The org axis is evaluated and recorded exactly as
// ScopeAllowed does (ADR-027 §7: enforced, no shadow mode).

import "testing"

func TestScopeAllowedUnknownMetadata_EmptyRuleSet(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{})
	rec := newFakeScopeRecorder()
	m.SetScopeAuditor(rec)
	if m.ScopeAllowedUnknownMetadata(nil, "acme", nil) {
		t.Error("an empty rule set must hide the degraded row (no open mode)")
	}
	if m.ScopeAllowedUnknownMetadata(&VerifiedPrincipal{Groups: []string{"g"}}, "acme", []string{"ORG-A"}) {
		t.Error("an empty rule set must hide the degraded row for any caller")
	}
	if len(rec.counts) != 0 {
		t.Errorf("no rule was blocked by a scope axis, yet recorded %v", rec.counts)
	}
}

func TestScopeAllowedUnknownMetadata_MetadataAxis(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name string
		rule GroupRule
		want bool
	}{
		{"wildcard on both metadata axes sees it",
			GroupRule{Name: "g", Tenants: []string{"*"}, Permissions: []Permission{PermRead}}, true},
		{"environment-restricted does NOT see it",
			GroupRule{Name: "g", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, Environments: []string{"staging"}}, false},
		{"domain-restricted does NOT see it",
			GroupRule{Name: "g", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, Domains: []string{"db"}}, false},
		{"tenant-pattern mismatch hides it",
			GroupRule{Name: "g", Tenants: []string{"other-*"}, Permissions: []Permission{PermRead}}, false},
		{"tenant-prefix match sees it",
			GroupRule{Name: "g", Tenants: []string{"ac*"}, Permissions: []Permission{PermRead}}, true},
	}
	p := &VerifiedPrincipal{Groups: []string{"g"}}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			m := NewForTest(&RBACConfig{Groups: []GroupRule{c.rule}})
			if got := m.ScopeAllowedUnknownMetadata(p, "acme", nil); got != c.want {
				t.Errorf("ScopeAllowedUnknownMetadata = %v, want %v", got, c.want)
			}
		})
	}

	// A caller the rule does not match sees nothing, even through a wildcard.
	m := NewForTest(&RBACConfig{Groups: []GroupRule{{Name: "g", Tenants: []string{"*"}, Permissions: []Permission{PermRead}}}})
	if m.ScopeAllowedUnknownMetadata(&VerifiedPrincipal{Groups: []string{"someone-else"}}, "acme", nil) {
		t.Error("a caller no rule matches must not see the degraded row")
	}
}

// TestScopeAllowedUnknownMetadata_UnknownIsNotLabeled pins WHY the list filter
// must not reuse ScopeAllowed with a guessed environment: a tenant LABELED
// with an allowed environment is visible to an environment-restricted caller,
// but a tenant whose environment is merely unreadable must not be — its real
// environment may be one the caller is not allowed. Both halves are asserted
// so the contrast cannot silently disappear.
func TestScopeAllowedUnknownMetadata_UnknownIsNotLabeled(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "g", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, Environments: []string{"staging"}},
	}}
	m := NewForTest(cfg)
	p := &VerifiedPrincipal{Groups: []string{"g"}}
	if !m.ScopeAllowed(p, "acme", "staging", "", nil) {
		t.Fatal("precondition: a tenant labeled into the rule's environments is visible")
	}
	if m.ScopeAllowed(p, "acme", "", "", nil) {
		t.Error("an unlabeled tenant must be hidden from an environment-restricted caller")
	}
	if m.ScopeAllowedUnknownMetadata(p, "acme", nil) {
		t.Error("an environment-restricted caller must NOT see a tenant whose environment is unknown")
	}
}

func TestScopeAllowedUnknownMetadata_OrgAxis(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "ops", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, OrgScope: "org"},
	}}
	cases := []struct {
		name       string
		claimOrg   string
		tenantOrgs []string
		want       bool
	}{
		{"member org", "ORG-A", []string{"ORG-A"}, true},
		{"non-member org", "ORG-C", []string{"ORG-A"}, false},
		{"no caller org claim, labeled tenant", "", []string{"ORG-A"}, false},
		{"unlabeled tenant", "ORG-A", nil, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var claims map[string]string
			if c.claimOrg != "" {
				claims = map[string]string{"org": c.claimOrg}
			}
			p := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: claims}
			m := NewForTest(cfg)
			if got := m.ScopeAllowedUnknownMetadata(p, "acme", c.tenantOrgs); got != c.want {
				t.Errorf("got %v, want %v", got, c.want)
			}
			// Parity with the org half of ScopeAllowed on a wildcard-metadata
			// rule: the org axis is evaluated identically.
			if want := m.ScopeAllowed(p, "acme", "", "", c.tenantOrgs); m.ScopeAllowedUnknownMetadata(p, "acme", c.tenantOrgs) != want {
				t.Error("diverges from ScopeAllowed's org axis")
			}
		})
	}
}

// TestScopeAllowedUnknownMetadata_CrossRuleNoLeak: the decision is a per-rule
// fold. Rule A has wildcard metadata but the wrong tenant pattern; rule B has
// the right pattern but restricts environment. No single rule grants.
func TestScopeAllowedUnknownMetadata_CrossRuleNoLeak(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "a", Tenants: []string{"other-*"}, Permissions: []Permission{PermRead}},
		{Name: "b", Tenants: []string{"acme"}, Permissions: []Permission{PermRead}, Environments: []string{"production"}},
	}}
	p := &VerifiedPrincipal{Groups: []string{"a", "b"}}
	if NewForTest(cfg).ScopeAllowedUnknownMetadata(p, "acme", nil) {
		t.Error("cross-rule leak — rule A's wildcard metadata combined with rule B's tenant pattern")
	}

	// Org variant: rule A wildcard metadata but org-scoped to a non-member
	// org; rule B no org restriction but environment-restricted.
	cfgOrg := &RBACConfig{Groups: []GroupRule{
		{Name: "a", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, OrgScope: "org"},
		{Name: "b", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, Environments: []string{"production"}},
	}}
	pOrg := &VerifiedPrincipal{Groups: []string{"a", "b"}, Claims: map[string]string{"org": "ORG-USER"}}
	for _, orgs := range [][]string{{"ORG-OTHER"}, nil} {
		if NewForTest(cfgOrg).ScopeAllowedUnknownMetadata(pOrg, "acme", orgs) {
			t.Errorf("orgs=%v: cross-rule leak — rule A's metadata wildcard combined with rule B's org pass", orgs)
		}
	}
}

// TestScopeAllowedUnknownMetadata_ScopeDeniedRecording: the decision records
// an ORG-axis denial exactly like ScopeAllowed (a tenant unlabeled in
// _tenant_orgs.yaml, or labeled out of the caller's org, is independent of the
// broken file) and NEVER records the metadata axis.
func TestScopeAllowedUnknownMetadata_ScopeDeniedRecording(t *testing.T) {
	t.Parallel()
	envRule := GroupRule{Name: "g", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, Environments: []string{"staging"}}
	orgRule := GroupRule{Name: "g", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, OrgScope: "org"}
	openRule := GroupRule{Name: "g", Tenants: []string{"*"}, Permissions: []Permission{PermRead}}
	cases := []struct {
		name       string
		rules      []GroupRule
		tenantOrgs []string
		wantVis    bool
		wantOrg    int
	}{
		// Hidden by the org axis → exactly one org observation.
		{"org-scoped rule, unlabeled tenant", []GroupRule{orgRule}, nil, false, 1},
		{"org-scoped rule, labeled non-member tenant", []GroupRule{orgRule}, []string{"ORG-OTHER"}, false, 1},
		// Two org-scoped rules both cut → still ONE observation per decision.
		{"two org-scoped rules, unlabeled tenant", []GroupRule{orgRule, orgRule}, nil, false, 1},
		// Allowed → nothing recorded.
		{"org-scoped rule, labeled member tenant", []GroupRule{orgRule}, []string{"ORG-A"}, true, 0},
		{"no org-scope, unrestricted", []GroupRule{openRule}, nil, true, 0},
		{"org-scoped + unrestricted non-org rule", []GroupRule{orgRule, openRule}, nil, true, 0},
		// Hidden, but by the unknown metadata, which records nothing.
		{"environment-restricted only", []GroupRule{envRule}, nil, false, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			m := NewForTest(&RBACConfig{Groups: c.rules})
			rec := newFakeScopeRecorder()
			m.SetScopeAuditor(rec)
			p := &VerifiedPrincipal{Groups: []string{"g"}, Claims: map[string]string{"org": "ORG-A"}}
			if got := m.ScopeAllowedUnknownMetadata(p, "acme", c.tenantOrgs); got != c.wantVis {
				t.Errorf("visible = %v, want %v", got, c.wantVis)
			}
			if rec.counts[scopeAxisOrg] != c.wantOrg {
				t.Errorf("org scope-denied = %d, want %d (all counts %v)", rec.counts[scopeAxisOrg], c.wantOrg, rec.counts)
			}
			if n := rec.counts[scopeAxisMetadata]; n != 0 {
				t.Errorf("metadata scope-denied = %d, want 0 — unknown metadata is not a labeling gap", n)
			}
			if len(rec.counts) > 1 || (len(rec.counts) == 1 && rec.counts[scopeAxisOrg] == 0) {
				t.Errorf("unexpected axes recorded: %v", rec.counts)
			}
		})
	}

	// Control: ScopeAllowed with the empty (unlabeled) pair DOES record a
	// metadata denial for the environment-restricted rule, so the zero
	// metadata count above is a property of this decision, not a dead
	// recorder.
	m := NewForTest(&RBACConfig{Groups: []GroupRule{envRule}})
	rec := newFakeScopeRecorder()
	m.SetScopeAuditor(rec)
	if m.ScopeAllowed(&VerifiedPrincipal{Groups: []string{"g"}}, "acme", "", "", nil) {
		t.Error("control: ScopeAllowed must hide the unlabeled tenant")
	}
	if rec.counts[scopeAxisMetadata] != 1 {
		t.Errorf("control: ScopeAllowed recorded %v, want metadata=1", rec.counts)
	}
}
