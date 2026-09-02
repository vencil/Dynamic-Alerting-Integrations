package rbac

import "testing"

// Regression tests for #1597: `environments:` / `domains:` in _rbac.yaml used
// to bind ONLY the list plane. AllowedInOrg took no environment/domain and
// never read rule.Environments / rule.Domains, so the same rule meant two
// different things depending on which plane read it — and the 403 told the
// denied operator to go tune exactly the field that could not deny them.
//
// The measurement on the ticket, reproduced as an assertion below:
//
//	WRITE  AllowedInOrg(rule environments:[production], any tenant)   = true
//	LIST   ScopeAllowed(same rule, tenant metadata environment=dev)   = false

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
// rule's allow-list is denied once the metadata axis enforces — and the two
// planes now answer the same question the same way.
func TestMetadataScopeBindsOnTheWritePlane(t *testing.T) {
	t.Parallel()
	p := metaWritePrincipal()

	t.Run("shadow keeps the pre-#1597 lenient answer", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(metaWriteCfg())
		if !m.AllowedInOrg(p, "db-a", PermWrite, nil, "dev", "") {
			t.Error("shadow mode must still allow: the migration must not tighten anyone's permissions before the flip")
		}
	})

	t.Run("enforce denies a write outside the rule's environments", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(metaWriteCfg())
		m.EnableMetadataWriteScopeEnforce()
		if m.AllowedInOrg(p, "db-a", PermWrite, nil, "dev", "") {
			t.Error("environments:[production] must deny a write to a dev tenant once the metadata axis enforces")
		}
	})

	t.Run("enforce still allows a write inside the rule's environments", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(metaWriteCfg())
		m.EnableMetadataWriteScopeEnforce()
		if !m.AllowedInOrg(p, "db-a", PermWrite, nil, "production", "") {
			t.Error("a matching environment must still be granted — the axis narrows, it must not deny outright")
		}
	})

	// The asymmetry the ticket is about: same rule, same principal, same tenant
	// metadata, two planes. Before #1597 these disagreed in enforce mode.
	t.Run("the write plane now agrees with the list plane", func(t *testing.T) {
		t.Parallel()
		for _, env := range []string{"dev", "production"} {
			m := NewForTest(metaWriteCfg())
			// Both axes at enforce: the point is that once BOTH planes are
			// closed they answer identically. During migration they may differ, and
			// that is the whole reason each plane has its own flag.
			m.EnableMetadataScopeEnforce()
			m.EnableMetadataWriteScopeEnforce()
			list := m.ScopeAllowed(p, "db-a", env, "", nil)
			write := m.AllowedInOrg(p, "db-a", PermWrite, nil, env, "")
			if list != write {
				t.Errorf("environment=%q: list plane says %v but write plane says %v — the two planes disagree again", env, list, write)
			}
		}
	})
}

// The write plane must record its would-deny on its OWN axis, so the
// enforce-flip soak criterion can require increase()==0 on the plane it is
// about to close (same reason scopeAxisOrgWrite exists).
func TestMetadataWritePlaneRecordsOnItsOwnAxis(t *testing.T) {
	t.Parallel()
	rec := newFakeScopeRecorder()
	m := NewForTest(metaWriteCfg())
	m.SetScopeAuditor(rec)

	// Shadow mode: the grant survives, but enforce would take it away.
	if !m.AllowedInOrg(metaWritePrincipal(), "db-a", PermWrite, nil, "dev", "") {
		t.Fatal("precondition: shadow must allow")
	}
	if rec.counts[scopeAxisMetadataWrite] != 1 {
		t.Errorf("would-deny on %q = %d, want 1", scopeAxisMetadataWrite, rec.counts[scopeAxisMetadataWrite])
	}
	if rec.counts[scopeAxisMetadata] != 0 {
		t.Errorf("a WRITE polluted the list/read axis %q (%d) — the two soak signals must stay separable",
			scopeAxisMetadata, rec.counts[scopeAxisMetadata])
	}
}

// The org-blind platform path must stay blind on the metadata axis too, and
// must record nothing — otherwise the wildcard route pollutes the soak
// counters that gate the enforce flip.
func TestAllowedStaysBlindOnBothAxes(t *testing.T) {
	t.Parallel()
	rec := newFakeScopeRecorder()
	m := NewForTest(metaWriteCfg())
	m.SetScopeAuditor(rec)
	m.EnableMetadataWriteScopeEnforce()

	if !m.Allowed(metaWritePrincipal(), "db-a", PermWrite) {
		t.Error("Allowed must remain metadata-blind even in enforce mode (platform-wildcard path)")
	}
	if len(rec.counts) != 0 {
		t.Errorf("Allowed recorded would-deny observations %v; it must record none", rec.counts)
	}
}

// A plane where the metadata axis is NOT live must not touch the metadata soak
// series. AllowedInOrgRead passes metaAxisLive=false and the unlabeled pair, so
// the lattice still shows a shadow/enforce difference for any rule carrying
// environments — recording that difference made every read-by-id increment
// {axis="metadata"}, the LIST plane's series, whose increase()==0 gates the
// unrelated --rbac-metadata-scope-enforce flip. Measured before the fix: one
// AllowedInOrgRead moved that counter to 1, so a deployment with a single
// environments-scoped rule could never satisfy its own flip criterion.
func TestReadPlaneDoesNotTouchTheMetadataSoakSeries(t *testing.T) {
	t.Parallel()
	rec := newFakeScopeRecorder()
	m := NewForTest(metaWriteCfg())
	m.SetScopeAuditor(rec)

	if !m.AllowedInOrgRead(metaWritePrincipal(), "db-a", PermRead, nil) {
		t.Fatal("precondition: the rule grants read")
	}
	if n := rec.counts[scopeAxisMetadata]; n != 0 {
		t.Errorf("a read-by-id incremented %q to %d — that is the LIST plane's soak series and the read plane cannot be denied by metadata",
			scopeAxisMetadata, n)
	}
	if n := rec.counts[scopeAxisMetadataWrite]; n != 0 {
		t.Errorf("a read-by-id incremented the write axis %q to %d", scopeAxisMetadataWrite, n)
	}
}
