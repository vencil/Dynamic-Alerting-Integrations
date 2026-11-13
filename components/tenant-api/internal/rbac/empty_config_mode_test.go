package rbac

import (
	"os"
	"path/filepath"
	"testing"
)

// Empty rule set semantics (ADR-027 §7): there is NO open mode. Whether the
// manager has no --rbac path at all or a configured _rbac.yaml that parses to
// zero groups, every evaluation entry point denies — no identity, whatever
// groups or claims it carries, gets any access. The pre-§7 path-less
// open-read degrade and the --rbac-empty-open escape hatch are gone.
//
// Each state is built through the real NewManager wiring (not NewForTest) so a
// regression that reintroduced a path-dependent bit would fail here.

// emptyRuleSetManagers returns the zero-group states a deployment can reach.
func emptyRuleSetManagers(t *testing.T) map[string]*Manager {
	t.Helper()
	pathless, err := NewManager("", nil) // no --rbac path
	if err != nil {
		t.Fatalf("NewManager(no path): %v", err)
	}
	path := filepath.Join(t.TempDir(), "_rbac.yaml")
	if err := os.WriteFile(path, []byte("groups: []\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	configured, err := NewManager(path, nil)
	if err != nil {
		t.Fatalf("NewManager(configured empty): %v", err)
	}
	return map[string]*Manager{
		"no-path":          pathless,
		"configured-empty": configured,
		"for-test-empty":   NewForTest(&RBACConfig{}),
	}
}

// Every entry point denies on an empty rule set, for every caller shape —
// including the nil (anonymous) principal and one carrying groups + claims.
// adr-check: ADR-027/9.6c
func TestEmptyRuleSet_EveryEntryPointDenies(t *testing.T) {
	t.Parallel()
	callers := map[string]*VerifiedPrincipal{
		"nil":    nil,
		"empty":  {},
		"groups": {Groups: []string{"anything", "platform-admins"}},
		"claims": {Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-1"}},
	}
	tenants := []string{"*", "tenant-x", ""}
	perms := []Permission{PermRead, PermWrite, PermAdmin}

	for mName, m := range emptyRuleSetManagers(t) {
		rec := newFakeScopeRecorder()
		m.SetScopeAuditor(rec)
		for cName, p := range callers {
			for _, tenant := range tenants {
				for _, want := range perms {
					if m.Allowed(p, tenant, want) {
						t.Errorf("[%s/%s] Allowed(%q, %s) = true, want false", mName, cName, tenant, want)
					}
					if m.AllowedInOrg(p, tenant, want, []string{"ORG-1"}, "production", "finance") {
						t.Errorf("[%s/%s] AllowedInOrg(%q, %s) = true, want false", mName, cName, tenant, want)
					}
					if m.AllowedInOrg(p, tenant, want, nil, "", "") {
						t.Errorf("[%s/%s] AllowedInOrg(%q, %s, unlabeled) = true, want false", mName, cName, tenant, want)
					}
					if m.AllowedInOrgRead(p, tenant, want, []string{"ORG-1"}) {
						t.Errorf("[%s/%s] AllowedInOrgRead(%q, %s) = true, want false", mName, cName, tenant, want)
					}
					if m.AllowedInOrgRejudge(p, tenant, want, []string{"ORG-1"}, "production", "finance", true, true) {
						t.Errorf("[%s/%s] AllowedInOrgRejudge(%q, %s) = true, want false", mName, cName, tenant, want)
					}
				}
				if m.ScopeAllowed(p, tenant, "production", "finance", []string{"ORG-1"}) {
					t.Errorf("[%s/%s] ScopeAllowed(%q, labeled) = true, want false", mName, cName, tenant)
				}
				if m.ScopeAllowed(p, tenant, "", "", nil) {
					t.Errorf("[%s/%s] ScopeAllowed(%q, unlabeled) = true, want false", mName, cName, tenant)
				}
				if m.MetadataAllowed(p, tenant, "production", "finance") {
					t.Errorf("[%s/%s] MetadataAllowed(%q) = true, want false", mName, cName, tenant)
				}
				if m.ScopeAllowedUnknownMetadata(p, tenant, []string{"ORG-1"}) {
					t.Errorf("[%s/%s] ScopeAllowedUnknownMetadata(%q) = true, want false", mName, cName, tenant)
				}
			}
			for _, want := range perms {
				if m.GrantsSomeTenant(p, want) {
					t.Errorf("[%s/%s] GrantsSomeTenant(%s) = true, want false", mName, cName, want)
				}
				if m.PlatformNonOrgScoped(p, want) {
					t.Errorf("[%s/%s] PlatformNonOrgScoped(%s) = true, want false", mName, cName, want)
				}
				if m.PlatformUnrestricted(p, want) {
					t.Errorf("[%s/%s] PlatformUnrestricted(%s) = true, want false", mName, cName, want)
				}
			}
			// No rule matched → no access: a NON-nil empty slice, never the
			// nil "no restriction" wildcard.
			if envs := m.AccessibleEnvironmentsFor(p); envs == nil || len(envs) != 0 {
				t.Errorf("[%s/%s] AccessibleEnvironmentsFor = %#v, want non-nil empty", mName, cName, envs)
			}
			if doms := m.AccessibleDomainsFor(p); doms == nil || len(doms) != 0 {
				t.Errorf("[%s/%s] AccessibleDomainsFor = %#v, want non-nil empty", mName, cName, doms)
			}
			if rules := m.RulesMatching(p); len(rules) != 0 {
				t.Errorf("[%s/%s] RulesMatching = %v, want none", mName, cName, rules)
			}
		}
		// No rule exists to be cut by a scope axis, so no denial is
		// attributed to one.
		if len(rec.counts) != 0 {
			t.Errorf("[%s] scope-denied counts = %v, want none (no rule was blocked by a scope axis)", mName, rec.counts)
		}
	}
}

// The legacy groups-slice shims follow the production core on an empty rule
// set (the historical HasPermission/HasMetadataAccess call sites deny too).
// adr-check: ADR-027/9.6c
func TestEmptyRuleSet_LegacyShimsDeny(t *testing.T) {
	t.Parallel()
	for mName, m := range emptyRuleSetManagers(t) {
		if m.HasPermission(nil, "*", PermRead) {
			t.Errorf("[%s] HasPermission(nil, *, read) = true, want false", mName)
		}
		if m.HasPermission([]string{"any"}, "any-tenant", PermRead) {
			t.Errorf("[%s] HasPermission(any, read) = true, want false", mName)
		}
		if m.HasPermission([]string{"any"}, "any-tenant", PermWrite) {
			t.Errorf("[%s] HasPermission(any, write) = true, want false", mName)
		}
		if m.HasMetadataAccess(nil, "t", "production", "finance") {
			t.Errorf("[%s] HasMetadataAccess = true, want false", mName)
		}
	}
}
