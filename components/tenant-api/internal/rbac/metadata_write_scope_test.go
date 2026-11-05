package rbac

import "testing"

// Regression tests for #1597: `environments:` / `domains:` in _rbac.yaml used
// to bind ONLY the list plane. AllowedInOrg took no environment/domain and
// never read rule.Environments / rule.Domains, so the same rule meant two
// different things depending on which plane read it — and the 403 told the
// denied operator to go tune exactly the field that could not deny them.
//
// The measurement on the ticket was:
//
//	WRITE  AllowedInOrg(rule environments:[production], any tenant)   = true
//	LIST   ScopeAllowed(same rule, tenant metadata environment=dev)   = false
//
// Both now answer false (ADR-027 §7: every scope axis is enforced).

// metaWriteCfg grants write on db-* but only in production. Deliberately no
// OrgScope: this isolates the METADATA axis, so a failure here cannot be the
// org axis in disguise.
func metaWriteCfg() *RBACConfig {
	return &RBACConfig{Groups: []GroupRule{
		{
			Name: "prod-ops", Tenants: []string{"db-*"},
			Permissions:  []Permission{PermRead, PermWrite},
			Environments: []string{"production"},
		},
	}}
}

func metaWritePrincipal() *VerifiedPrincipal {
	return &VerifiedPrincipal{Email: "ops@example.com", Groups: []string{"prod-ops"}}
}

// The core of the fix: a write against a tenant whose environment is NOT in the
// rule's allow-list is denied — and the two planes answer the same question
// the same way. There is no shadow mode (ADR-027 §7).
func TestMetadataScopeBindsOnTheWritePlane(t *testing.T) {
	t.Parallel()
	p := metaWritePrincipal()

	t.Run("denies a write outside the rule's environments", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(metaWriteCfg())
		if m.AllowedInOrg(p, "db-w1", PermWrite, nil, "dev", "") {
			t.Error("environments:[production] must deny a write to a dev tenant")
		}
	})

	t.Run("denies a write to an unlabeled tenant", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(metaWriteCfg())
		if m.AllowedInOrg(p, "db-w1", PermWrite, nil, "", "") {
			t.Error("an unlabeled tenant must not be writable through an environments-restricted rule")
		}
	})

	t.Run("allows a write inside the rule's environments", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(metaWriteCfg())
		if !m.AllowedInOrg(p, "db-w1", PermWrite, nil, "production", "") {
			t.Error("a matching environment must still be granted — the axis narrows, it must not deny outright")
		}
	})

	// The asymmetry the ticket is about: same rule, same principal, same tenant
	// metadata, two planes. Before #1597 these disagreed.
	t.Run("the write plane agrees with the list plane", func(t *testing.T) {
		t.Parallel()
		for _, env := range []string{"dev", "production", ""} {
			m := NewForTest(metaWriteCfg())
			list := m.ScopeAllowed(p, "db-w1", env, "", nil)
			write := m.AllowedInOrg(p, "db-w1", PermWrite, nil, env, "")
			if list != write {
				t.Errorf("environment=%q: list plane says %v but write plane says %v — the two planes disagree again", env, list, write)
			}
		}
	})
}

// The write plane records its scope denial on its OWN axis, never on the
// list/read axis, and records nothing for an allowed write.
func TestMetadataWritePlaneRecordsOnItsOwnAxis(t *testing.T) {
	t.Parallel()

	t.Run("denied write counts metadata_write once", func(t *testing.T) {
		t.Parallel()
		rec := newFakeScopeRecorder()
		m := NewForTest(metaWriteCfg())
		m.SetScopeAuditor(rec)
		if m.AllowedInOrg(metaWritePrincipal(), "db-w1", PermWrite, nil, "dev", "") {
			t.Fatal("precondition: an out-of-scope write must be denied")
		}
		if rec.counts[scopeAxisMetadataWrite] != 1 {
			t.Errorf("scope-denied on %q = %d, want 1", scopeAxisMetadataWrite, rec.counts[scopeAxisMetadataWrite])
		}
		if rec.counts[scopeAxisMetadata] != 0 {
			t.Errorf("a WRITE polluted the list/read axis %q (%d) — the two signals must stay separable",
				scopeAxisMetadata, rec.counts[scopeAxisMetadata])
		}
		if rec.counts[scopeAxisOrgWrite] != 0 {
			t.Errorf("the org axis did not deny, yet %q = %d", scopeAxisOrgWrite, rec.counts[scopeAxisOrgWrite])
		}
	})

	t.Run("allowed write records nothing", func(t *testing.T) {
		t.Parallel()
		rec := newFakeScopeRecorder()
		m := NewForTest(metaWriteCfg())
		m.SetScopeAuditor(rec)
		if !m.AllowedInOrg(metaWritePrincipal(), "db-w1", PermWrite, nil, "production", "") {
			t.Fatal("precondition: an in-scope write must be allowed")
		}
		if len(rec.counts) != 0 {
			t.Errorf("an allowed write recorded %v; it must record nothing", rec.counts)
		}
	})

	t.Run("write the permission does not cover records nothing", func(t *testing.T) {
		t.Parallel()
		rec := newFakeScopeRecorder()
		m := NewForTest(metaWriteCfg())
		m.SetScopeAuditor(rec)
		if m.AllowedInOrg(metaWritePrincipal(), "db-w1", PermAdmin, nil, "dev", "") {
			t.Fatal("precondition: the rule does not grant admin")
		}
		if len(rec.counts) != 0 {
			t.Errorf("a denial the permission check caused was attributed to a scope axis: %v", rec.counts)
		}
	})
}

// The scope-blind platform path stays blind on the metadata axis too, and
// records nothing: a scope-blind decision cannot be denied by scope.
func TestAllowedStaysBlindOnBothAxes(t *testing.T) {
	t.Parallel()
	rec := newFakeScopeRecorder()
	m := NewForTest(metaWriteCfg())
	m.SetScopeAuditor(rec)

	if !m.Allowed(metaWritePrincipal(), "db-w1", PermWrite) {
		t.Error("Allowed must remain metadata-blind (platform-wildcard path)")
	}
	if len(rec.counts) != 0 {
		t.Errorf("Allowed recorded scope-denied observations %v; it must record none", rec.counts)
	}
}

// The read-by-id plane does not bind the metadata axis (AllowedInOrgRead is
// metadata-blind), so it must neither deny on it nor touch the metadata
// series: a read-by-id on an environments-scoped rule is granted, and records
// nothing on {axis="metadata"} (the LIST plane's series) or the write axis.
func TestReadPlaneDoesNotTouchTheMetadataSeries(t *testing.T) {
	t.Parallel()
	rec := newFakeScopeRecorder()
	m := NewForTest(metaWriteCfg())
	m.SetScopeAuditor(rec)

	if !m.AllowedInOrgRead(metaWritePrincipal(), "db-w1", PermRead, nil) {
		t.Fatal("precondition: the rule grants read and the read plane is metadata-blind")
	}
	if n := rec.counts[scopeAxisMetadata]; n != 0 {
		t.Errorf("a read-by-id incremented %q to %d — that is the LIST plane's series and the read plane cannot be denied by metadata",
			scopeAxisMetadata, n)
	}
	if n := rec.counts[scopeAxisMetadataWrite]; n != 0 {
		t.Errorf("a read-by-id incremented the write axis %q to %d", scopeAxisMetadataWrite, n)
	}
}
