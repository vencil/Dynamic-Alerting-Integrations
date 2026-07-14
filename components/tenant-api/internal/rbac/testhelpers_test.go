package rbac

// Shared test fixtures and doubles for the rbac package test suite.
//
// Everything here is cross-file test infrastructure: read-only fixture
// builders (equivConfig / matchEvalConfig / matchLoadYAML), the
// fakeScopeRecorder audit double, the toSet comparison helper, and the
// moduleRootForGuard locator the go/ast guards use. Keep this file free of
// Test functions; fixtures must stay read-only (tests that need mutable
// state build their own Manager).

import (
	"os"
	"path/filepath"
	"testing"
)

// ── scope-audit double ───────────────────────────────────────────────────────

// fakeScopeRecorder is an in-test ScopeAuditRecorder capturing would-deny
// observations per axis, so tests assert on their own isolated instance.
type fakeScopeRecorder struct{ counts map[string]int }

func newFakeScopeRecorder() *fakeScopeRecorder {
	return &fakeScopeRecorder{counts: map[string]int{}}
}

func (f *fakeScopeRecorder) IncWouldDeny(axis string) { f.counts[axis]++ }

// ── legacy-equivalence fixtures (ADR-027 / LD-6 P3) ─────────────────────────

// equivConfig is a rule set that exercises every legacy matching feature:
// wildcard tenants, prefix tenants, multi-permission rules, and
// environment/domain-restricted rules.
func equivConfig() *RBACConfig {
	return &RBACConfig{Groups: []GroupRule{
		{Name: "platform-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
		{Name: "db-ops", Tenants: []string{"db-a-*", "db-b-*"}, Permissions: []Permission{PermRead, PermWrite}},
		{Name: "viewers", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		{
			Name: "prod-finance", Tenants: []string{"*"}, Permissions: []Permission{PermRead},
			Environments: []string{"production"}, Domains: []string{"finance"},
		},
	}}
}

// equivManagers returns the three manager modes the legacy matrix covered:
// path-less open mode, configured-but-empty fail-closed mode (MED-8), and a
// configured rule set.
func equivManagers() map[string]*Manager {
	failClosed := NewForTest(&RBACConfig{})
	failClosed.failClosedOnEmpty = true
	return map[string]*Manager{
		"open":        NewForTest(&RBACConfig{}),
		"fail-closed": failClosed,
		"configured":  NewForTest(equivConfig()),
	}
}

// equivGroupSets covers nil, empty, unknown, single, multi-group callers.
func equivGroupSets() map[string][]string {
	return map[string][]string{
		"nil":            nil,
		"empty":          {},
		"unknown":        {"no-such-group"},
		"viewers":        {"viewers"},
		"db-ops":         {"db-ops"},
		"admin":          {"platform-admins"},
		"viewers+db-ops": {"viewers", "db-ops"},
		"scoped":         {"prod-finance"},
	}
}

var equivTenants = []string{"db-a-prod", "db-b-staging", "redis-01", "*", ""}

var equivPerms = []Permission{PermRead, PermWrite, PermAdmin}

// ── claims-aware match fixtures (ADR-027 / LD-6 P3) ─────────────────────────

// matchEvalConfig exercises every match shape next to a legacy rule.
func matchEvalConfig() *RBACConfig {
	return &RBACConfig{Groups: []GroupRule{
		// Legacy rule: the name IS the matched IdP group.
		{Name: "legacy-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
		// Groups-only match (OR-within the list).
		{Name: "ops-rule", Match: &MatchBlock{Groups: []string{"operators", "sre"}},
			Tenants: []string{"ops-*"}, Permissions: []Permission{PermRead}},
		// Claims-only match, multi-value OR.
		{Name: "org-readers", Match: &MatchBlock{Claims: map[string][]string{"org": {"ORG-A", "ORG-B"}}},
			Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		// Groups AND claims — both condition kinds must hold.
		{Name: "org-a-operators", Match: &MatchBlock{
			Groups: []string{"operators"},
			Claims: map[string][]string{"org": {"ORG-A"}},
		}, Tenants: []string{"alpha-*"}, Permissions: []Permission{PermRead, PermWrite}},
		// Two claim keys — AND across keys, OR within each value list.
		{Name: "org-a-eu", Match: &MatchBlock{
			Claims: map[string][]string{"org": {"ORG-A"}, "region": {"eu-1", "eu-2"}},
		}, Tenants: []string{"eu-*"}, Permissions: []Permission{PermWrite}},
	}}
}

// matchLoadYAML is the canonical loadable match-rule config used by the load,
// reload and middleware end-to-end tests (declared claim key: org).
const matchLoadYAML = `groups:
  - name: org-a-operators
    match:
      groups: [operators]
      claims:
        org: [ORG-A]
    tenants: ["*"]
    permissions: [read, write]
`

// ── misc helpers ─────────────────────────────────────────────────────────────

// toSet converts a slice to a map for order-independent comparison
func toSet(s []string) map[string]bool {
	m := make(map[string]bool)
	for _, v := range s {
		m[v] = true
	}
	return m
}

// moduleRootForGuard walks up from the package directory (the test working
// directory) to the nearest go.mod — the tenant-api module root. Used by the
// go/ast architecture guards in guards_ast_test.go.
func moduleRootForGuard(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("go.mod not found above the rbac package directory")
		}
		dir = parent
	}
}
