package rbac

// ScopeAllowedUnknownMetadata — the list-plane visibility decision for a
// DEGRADED row (#1680): a tenant whose conf.d file exists but is unusable, so
// its environment/domain are UNKNOWN. The load-bearing difference from
// ScopeAllowed(p, id, "", "", orgs) is that unknown is not unlabeled: no
// shadow leniency on the metadata axis in either mode.

import "testing"

func TestScopeAllowedUnknownMetadata_OpenMode(t *testing.T) {
	t.Parallel()
	open := NewForTest(&RBACConfig{})
	if !open.ScopeAllowedUnknownMetadata(nil, "acme", nil) {
		t.Error("open mode (no groups, not fail-closed) must show the degraded row, like ScopeAllowed")
	}
	closed := NewForTest(&RBACConfig{})
	closed.failClosedOnEmpty = true
	if closed.ScopeAllowedUnknownMetadata(nil, "acme", nil) {
		t.Error("configured-but-empty _rbac.yaml (failClosedOnEmpty) must hide the degraded row (MED-8)")
	}
}

// unknownMetaManagers returns one Manager over cfg per (metadataEnforce,
// orgEnforce) flag combination, keyed by a readable mode name.
func unknownMetaManagers(cfg *RBACConfig) map[string]*Manager {
	out := map[string]*Manager{}
	for _, meta := range []bool{false, true} {
		for _, org := range []bool{false, true} {
			m := NewForTest(cfg)
			if meta {
				m.EnableMetadataScopeEnforce()
			}
			if org {
				m.EnableOrgScopeEnforce()
			}
			name := "meta=shadow"
			if meta {
				name = "meta=enforce"
			}
			if org {
				name += ",org=enforce"
			} else {
				name += ",org=shadow"
			}
			out[name] = m
		}
	}
	return out
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
		for mode, m := range unknownMetaManagers(&RBACConfig{Groups: []GroupRule{c.rule}}) {
			t.Run(c.name+"/"+mode, func(t *testing.T) {
				if got := m.ScopeAllowedUnknownMetadata(p, "acme", nil); got != c.want {
					t.Errorf("ScopeAllowedUnknownMetadata = %v, want %v", got, c.want)
				}
			})
		}
	}

	// A caller the rule does not match sees nothing, even through a wildcard.
	m := NewForTest(&RBACConfig{Groups: []GroupRule{{Name: "g", Tenants: []string{"*"}, Permissions: []Permission{PermRead}}}})
	if m.ScopeAllowedUnknownMetadata(&VerifiedPrincipal{Groups: []string{"someone-else"}}, "acme", nil) {
		t.Error("a caller no rule matches must not see the degraded row")
	}
}

// TestScopeAllowedUnknownMetadata_NotTheUnlabeledShadowPath pins WHY the list
// filter must not reuse ScopeAllowed with an empty environment/domain: under
// shadow metadata mode that call SHOWS the tenant to an environment-restricted
// caller (the unlabeled leniency), which for a broken file is a leak. Both
// halves are asserted so the contrast cannot silently disappear.
func TestScopeAllowedUnknownMetadata_NotTheUnlabeledShadowPath(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "g", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, Environments: []string{"staging"}},
	}}
	m := NewForTest(cfg) // metadata axis in SHADOW
	p := &VerifiedPrincipal{Groups: []string{"g"}}
	if !m.ScopeAllowed(p, "acme", "", "", nil) {
		t.Fatal("precondition: ScopeAllowed with empty env/domain is expected to shadow-allow (unlabeled leniency)")
	}
	if m.ScopeAllowedUnknownMetadata(p, "acme", nil) {
		t.Error("an environment-restricted caller must NOT see a tenant whose environment is unknown, even in shadow mode")
	}
}

func TestScopeAllowedUnknownMetadata_OrgAxis(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "ops", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, OrgScope: "org"},
	}}
	cases := []struct {
		name        string
		claimOrg    string
		tenantOrgs  []string
		wantShadow  bool
		wantEnforce bool
	}{
		{"member org", "ORG-A", []string{"ORG-A"}, true, true},
		{"non-member org", "ORG-C", []string{"ORG-A"}, false, false},
		{"no caller org claim, labeled tenant", "", []string{"ORG-A"}, false, false},
		{"unlabeled tenant", "ORG-A", nil, true, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var claims map[string]string
			if c.claimOrg != "" {
				claims = map[string]string{"org": c.claimOrg}
			}
			p := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: claims}
			for _, meta := range []bool{false, true} {
				shadowM := NewForTest(cfg)
				enforceM := NewForTest(cfg)
				enforceM.EnableOrgScopeEnforce()
				if meta {
					shadowM.EnableMetadataScopeEnforce()
					enforceM.EnableMetadataScopeEnforce()
				}
				if got := shadowM.ScopeAllowedUnknownMetadata(p, "acme", c.tenantOrgs); got != c.wantShadow {
					t.Errorf("metaEnforce=%v org=shadow: got %v, want %v", meta, got, c.wantShadow)
				}
				if got := enforceM.ScopeAllowedUnknownMetadata(p, "acme", c.tenantOrgs); got != c.wantEnforce {
					t.Errorf("metaEnforce=%v org=enforce: got %v, want %v", meta, got, c.wantEnforce)
				}
				// Parity with the org half of ScopeAllowed on a wildcard-metadata
				// rule: the org axis is evaluated identically.
				if want := shadowM.ScopeAllowed(p, "acme", "", "", c.tenantOrgs); shadowM.ScopeAllowedUnknownMetadata(p, "acme", c.tenantOrgs) != want {
					t.Errorf("metaEnforce=%v org=shadow: diverges from ScopeAllowed's org axis", meta)
				}
				if want := enforceM.ScopeAllowed(p, "acme", "", "", c.tenantOrgs); enforceM.ScopeAllowedUnknownMetadata(p, "acme", c.tenantOrgs) != want {
					t.Errorf("metaEnforce=%v org=enforce: diverges from ScopeAllowed's org axis", meta)
				}
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
	for mode, m := range unknownMetaManagers(cfg) {
		if m.ScopeAllowedUnknownMetadata(p, "acme", nil) {
			t.Errorf("%s: cross-rule leak — rule A's wildcard metadata combined with rule B's tenant pattern", mode)
		}
	}

	// Org variant: rule A wildcard metadata but org-scoped to a non-member
	// org; rule B no org restriction but environment-restricted.
	cfgOrg := &RBACConfig{Groups: []GroupRule{
		{Name: "a", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, OrgScope: "org"},
		{Name: "b", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, Environments: []string{"production"}},
	}}
	pOrg := &VerifiedPrincipal{Groups: []string{"a", "b"}, Claims: map[string]string{"org": "ORG-USER"}}
	for mode, m := range unknownMetaManagers(cfgOrg) {
		if m.ScopeAllowedUnknownMetadata(pOrg, "acme", []string{"ORG-OTHER"}) {
			t.Errorf("%s: cross-rule leak — rule A's metadata wildcard combined with rule B's org pass", mode)
		}
	}
}

// TestScopeAllowedUnknownMetadata_WouldDenyRecording: the decision records the
// ORG-axis shadow gap exactly like ScopeAllowed (a tenant unlabeled in
// _tenant_orgs.yaml is a genuine labeling gap, independent of the broken file)
// and NEVER records the metadata axis (it grants no metadata leniency).
func TestScopeAllowedUnknownMetadata_WouldDenyRecording(t *testing.T) {
	t.Parallel()
	envRule := GroupRule{Name: "g", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, Environments: []string{"staging"}}
	orgRule := GroupRule{Name: "g", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, OrgScope: "org"}
	openRule := GroupRule{Name: "g", Tenants: []string{"*"}, Permissions: []Permission{PermRead}}
	cases := []struct {
		name       string
		rules      []GroupRule
		tenantOrgs []string
		orgEnforce bool
		wantVis    bool
		wantOrg    int
	}{
		// Visible ONLY via the org axis's unlabeled leniency → exactly one
		// org observation, so the {axis="org"} soak announces the flip.
		{"org-scoped rule, unlabeled tenant, org shadow", []GroupRule{orgRule}, nil, false, true, 1},
		// Under enforce the same row is hidden and the counter keeps doubling
		// as the "denied by scope" signal — as ScopeAllowed does.
		{"org-scoped rule, unlabeled tenant, org enforce", []GroupRule{orgRule}, nil, true, false, 1},
		// Nothing hinges on leniency → zero.
		{"org-scoped rule, labeled member tenant", []GroupRule{orgRule}, []string{"ORG-A"}, false, true, 0},
		{"no org-scope, unrestricted", []GroupRule{openRule}, nil, false, true, 0},
		{"environment-restricted only (hidden in both modes)", []GroupRule{envRule}, nil, false, false, 0},
		// An unrestricted non-org rule grants under org enforce too → no gap.
		{"org-scoped + unrestricted non-org rule", []GroupRule{orgRule, openRule}, nil, false, true, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			m := NewForTest(&RBACConfig{Groups: c.rules})
			if c.orgEnforce {
				m.EnableOrgScopeEnforce()
			}
			rec := newFakeScopeRecorder()
			m.SetScopeAuditor(rec)
			p := &VerifiedPrincipal{Groups: []string{"g"}, Claims: map[string]string{"org": "ORG-A"}}
			if got := m.ScopeAllowedUnknownMetadata(p, "acme", c.tenantOrgs); got != c.wantVis {
				t.Errorf("visible = %v, want %v", got, c.wantVis)
			}
			if rec.counts[scopeAxisOrg] != c.wantOrg {
				t.Errorf("org would-deny = %d, want %d (all counts %v)", rec.counts[scopeAxisOrg], c.wantOrg, rec.counts)
			}
			if n := rec.counts[scopeAxisMetadata]; n != 0 {
				t.Errorf("metadata would-deny = %d, want 0 — this decision grants no metadata leniency", n)
			}
			if len(rec.counts) > 1 || (len(rec.counts) == 1 && rec.counts[scopeAxisOrg] == 0) {
				t.Errorf("unexpected axes recorded: %v", rec.counts)
			}
		})
	}

	// Control: ScopeAllowed with the empty (unlabeled) pair DOES record a
	// metadata would-deny for the environment-restricted rule, so the zero
	// metadata count above is a property of the new decision, not a dead
	// recorder.
	m := NewForTest(&RBACConfig{Groups: []GroupRule{envRule}})
	rec := newFakeScopeRecorder()
	m.SetScopeAuditor(rec)
	m.ScopeAllowed(&VerifiedPrincipal{Groups: []string{"g"}}, "acme", "", "", nil)
	if rec.counts[scopeAxisMetadata] == 0 {
		t.Errorf("control: ScopeAllowed recorded %v, want a metadata observation", rec.counts)
	}
}
