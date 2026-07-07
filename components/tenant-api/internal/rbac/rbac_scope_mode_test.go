package rbac

import "testing"

// fakeScopeRecorder is an in-test ScopeAuditRecorder capturing would-deny
// observations per axis, so tests assert on their own isolated instance.
type fakeScopeRecorder struct{ counts map[string]int }

func newFakeScopeRecorder() *fakeScopeRecorder {
	return &fakeScopeRecorder{counts: map[string]int{}}
}

func (f *fakeScopeRecorder) IncWouldDeny(axis string) { f.counts[axis]++ }

// TestScopeFieldModes pins the pure two-mode field evaluator: wildcard passes
// both modes, an unlabeled value passes shadow but not enforce, and a labeled
// value is identical exact-membership in both modes.
func TestScopeFieldModes(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name        string
		allowList   []string
		value       string
		wantShadow  bool
		wantEnforce bool
	}{
		{"empty allow-list is wildcard", nil, "production", true, true},
		{"empty allow-list wildcard even for empty value", nil, "", true, true},
		{"unlabeled on restricted: shadow yes, enforce no", []string{"production"}, "", true, false},
		{"labeled member: both yes", []string{"production", "staging"}, "production", true, true},
		{"labeled non-member: both no", []string{"production"}, "dev", false, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			gotShadow, gotEnforce := scopeFieldModes(c.allowList, c.value)
			if gotShadow != c.wantShadow || gotEnforce != c.wantEnforce {
				t.Errorf("scopeFieldModes(%v, %q) = (%v, %v), want (%v, %v)",
					c.allowList, c.value, gotShadow, gotEnforce, c.wantShadow, c.wantEnforce)
			}
		})
	}
}

// TestHasMetadataAccess_ScopeMode exercises the mode end-to-end through the
// public method the list filter calls, and asserts the would-deny counter is
// PER-TENANT (Option Y): one observation per would-be-hidden tenant, not per
// field-check, and zero when another rule grants access under strict semantics.
func TestHasMetadataAccess_ScopeMode(t *testing.T) {
	t.Parallel()

	envRule := &RBACConfig{Groups: []GroupRule{{
		Name:         "ops",
		Tenants:      []string{"db-a"},
		Permissions:  []Permission{PermWrite},
		Environments: []string{"production"},
	}}}

	t.Run("shadow: unlabeled tenant stays visible, one would-deny", func(t *testing.T) {
		m := NewForTest(envRule)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if !m.HasMetadataAccess([]string{"ops"}, "db-a", "", "") {
			t.Error("shadow: unlabeled tenant must remain accessible (byte-identical to legacy)")
		}
		if got := rec.counts[scopeAxisMetadata]; got != 1 {
			t.Errorf("shadow: want 1 would-deny, got %d", got)
		}
	})

	t.Run("enforce: unlabeled tenant hidden, one would-deny", func(t *testing.T) {
		m := NewForTest(envRule)
		m.EnableMetadataScopeEnforce()
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if m.HasMetadataAccess([]string{"ops"}, "db-a", "", "") {
			t.Error("enforce: unlabeled tenant must be denied")
		}
		if got := rec.counts[scopeAxisMetadata]; got != 1 {
			t.Errorf("enforce: want 1 would-deny, got %d", got)
		}
	})

	t.Run("labeled matching tenant unaffected by mode, no would-deny", func(t *testing.T) {
		for _, enforce := range []bool{false, true} {
			m := NewForTest(envRule)
			if enforce {
				m.EnableMetadataScopeEnforce()
			}
			rec := newFakeScopeRecorder()
			m.SetScopeAuditor(rec)
			if !m.HasMetadataAccess([]string{"ops"}, "db-a", "production", "") {
				t.Errorf("enforce=%v: labeled matching tenant must stay visible", enforce)
			}
			if got := rec.counts[scopeAxisMetadata]; got != 0 {
				t.Errorf("enforce=%v: labeled tenant must not record would-deny, got %d", enforce, got)
			}
		}
	})

	// Option Y fix: two restricted fields, both unlabeled → exactly ONE
	// observation for the tenant (not two, one per field).
	t.Run("two restricted fields both unlabeled record once", func(t *testing.T) {
		cfg := &RBACConfig{Groups: []GroupRule{{
			Name:         "ops",
			Tenants:      []string{"db-a"},
			Permissions:  []Permission{PermWrite},
			Environments: []string{"production"},
			Domains:      []string{"finance"},
		}}}
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		if !m.HasMetadataAccess([]string{"ops"}, "db-a", "", "") {
			t.Error("shadow: unlabeled tenant must remain visible")
		}
		if got := rec.counts[scopeAxisMetadata]; got != 1 {
			t.Errorf("two-field unlabeled: want exactly 1 would-deny (per-tenant), got %d", got)
		}
	})

	// Option Y fix: a wildcard rule grants access under strict semantics, so the
	// tenant is NOT would-be-hidden → zero would-deny even though a co-matching
	// restricted rule would deny the unlabeled field. Prevents a stuck counter.
	t.Run("wildcard rule rescues unlabeled tenant: no would-deny", func(t *testing.T) {
		cfg := &RBACConfig{Groups: []GroupRule{
			{Name: "ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}, Environments: []string{"production"}},
			{Name: "ops", Tenants: []string{"db-a"}, Permissions: []Permission{PermWrite}}, // wildcard env/domain
		}}
		m := NewForTest(cfg)
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		for _, enforce := range []bool{false, true} {
			m.metadataScopeEnforce = enforce
			if !m.HasMetadataAccess([]string{"ops"}, "db-a", "", "") {
				t.Errorf("enforce=%v: wildcard rule must keep tenant visible", enforce)
			}
		}
		if got := rec.counts[scopeAxisMetadata]; got != 0 {
			t.Errorf("wildcard-rescued tenant must record 0 would-deny, got %d", got)
		}
	})

	t.Run("nil recorder does not panic", func(t *testing.T) {
		m := NewForTest(envRule) // scopeAudit nil
		if !m.HasMetadataAccess([]string{"ops"}, "db-a", "", "") {
			t.Error("shadow with nil recorder must still allow")
		}
	})
}
