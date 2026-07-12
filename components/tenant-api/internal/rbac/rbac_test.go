package rbac

import (
	"strings"
	"testing"
)

func TestTenantMatches(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name     string
		patterns []string
		tenantID string
		want     bool
	}{
		{"wildcard", []string{"*"}, "any-tenant", true},
		{"exact match", []string{"db-a"}, "db-a", true},
		{"exact no match", []string{"db-a"}, "db-b", false},
		{"prefix match", []string{"db-a-*"}, "db-a-prod", true},
		{"prefix no match", []string{"db-a-*"}, "db-b-prod", false},
		{"multiple patterns", []string{"db-a-*", "db-b-*"}, "db-b-staging", true},
		{"empty patterns", []string{}, "db-a", false},
		// Malformed patterns must never match (fail-closed backstop for a rule
		// that bypassed validateConfig). NOTE: of these, only the first case —
		// ["**"] vs the platform-scope "*" gate — actually EXERCISES the guard:
		// without it "**" collapses to prefix "*" and HasPrefix("*","*")==true
		// would fail the rule OPEN (mutation-verified: removing the guard reddens
		// exactly that case). The others already return false on their own merits
		// (their derived prefix mismatches the id); they lock dead-rule behavior
		// against a future tenantMatches change. See TestTenantPatternInvariants
		// for the load-bearing, grammar-wide fail-open / matchability pins.
		{"double-star vs platform gate does not fail open", []string{"**"}, "*", false},
		{"double-star vs real tenant does not match", []string{"**"}, "db-a", false},
		{"embedded-star vs platform gate does not fail open", []string{"*a*"}, "*", false},
		{"embedded-star vs real tenant does not match", []string{"*a*"}, "db-a", false},
		{"trailing-double-star vs platform gate does not fail open", []string{"a**"}, "*", false},
		{"trailing-double-star vs real tenant does not match", []string{"a**"}, "db-a", false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if got := tenantMatches(tt.patterns, tt.tenantID); got != tt.want {
				t.Errorf("tenantMatches(%v, %q) = %v, want %v", tt.patterns, tt.tenantID, got, tt.want)
			}
		})
	}
}

// TestTenantPatternInvariants pins — across the whole small pattern grammar —
// the two properties the "**" fail-open fix depends on, so a future edit to
// validTenantPattern or tenantMatches that breaks allowlist↔matcher agreement
// fails loudly. Unlike most TenantMatches rows (which return false on their own
// merits and so never exercise the guard), EVERY case here is load-bearing:
//
//  1. NO FAIL-OPEN — only the literal "*" may pass a platform-scope "*" gate
//     query (Allowed(p, "*", …)). Any other pattern, well-formed or malformed,
//     must NOT match tenantID "*".
//  2. ALLOWLIST↔MATCHER AGREEMENT — every pattern validTenantPattern accepts
//     must be matchable by some id (a validateConfig-accepted rule is never a
//     silently dead rule the guard refuses to match).
func TestTenantPatternInvariants(t *testing.T) {
	t.Parallel()
	grammar := []string{
		"*", "db-a", "db-a-*", "a*", "-*", "x", // well-formed
		"**", "***", "*a", "*a*", "a**", "a*b", "", " ", "   ", // malformed
	}
	for _, pat := range grammar {
		pat := pat
		t.Run("pat="+pat, func(t *testing.T) {
			t.Parallel()
			// Invariant 1 — no fail-open at the platform-scope "*" gate.
			if got := tenantMatches([]string{pat}, "*"); got != (pat == "*") {
				t.Errorf("fail-open: tenantMatches([%q], \"*\") = %v, want %v (only \"*\" may pass a platform gate)", pat, got, pat == "*")
			}
			// Invariant 2 — an accepted pattern must be matchable by some id.
			if !validTenantPattern(pat) {
				return
			}
			var id string
			switch {
			case pat == "*":
				id = "any-tenant"
			case strings.HasSuffix(pat, "*"):
				id = strings.TrimSuffix(pat, "*") + "x" // literal prefix + one char
			default:
				id = pat // exact
			}
			if !tenantMatches([]string{pat}, id) {
				t.Errorf("accepted pattern %q not matchable: tenantMatches([%q], %q) = false", pat, pat, id)
			}
		})
	}
}

func TestPermCovers(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name  string
		grant Permission
		want  Permission
		ok    bool
	}{
		{"admin covers read", PermAdmin, PermRead, true},
		{"admin covers write", PermAdmin, PermWrite, true},
		{"admin covers admin", PermAdmin, PermAdmin, true},
		{"write covers read", PermWrite, PermRead, true},
		{"write covers write", PermWrite, PermWrite, true},
		{"write not covers admin", PermWrite, PermAdmin, false},
		{"read covers read", PermRead, PermRead, true},
		{"read not covers write", PermRead, PermWrite, false},
		{"read not covers admin", PermRead, PermAdmin, false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if got := permCovers(tt.grant, tt.want); got != tt.ok {
				t.Errorf("permCovers(%s, %s) = %v, want %v", tt.grant, tt.want, got, tt.ok)
			}
		})
	}
}

func TestHasPermission(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{
		Groups: []GroupRule{
			{Name: "platform-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
			{Name: "db-ops", Tenants: []string{"db-a-*", "db-b-*"}, Permissions: []Permission{PermRead, PermWrite}},
			{Name: "viewers", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		},
	})

	tests := []struct {
		name     string
		groups   []string
		tenant   string
		want     Permission
		expected bool
	}{
		{"admin can write any", []string{"platform-admins"}, "any", PermWrite, true},
		{"db-ops can write db-a tenant", []string{"db-ops"}, "db-a-prod", PermWrite, true},
		{"db-ops cannot write other tenant", []string{"db-ops"}, "redis-01", PermWrite, false},
		{"db-ops can read db-a tenant", []string{"db-ops"}, "db-a-staging", PermRead, true},
		{"viewer can read any", []string{"viewers"}, "any-tenant", PermRead, true},
		{"viewer cannot write", []string{"viewers"}, "any-tenant", PermWrite, false},
		{"multi-group uses best match", []string{"viewers", "db-ops"}, "db-a-prod", PermWrite, true},
		{"unknown group denied", []string{"unknown"}, "any", PermRead, false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if got := m.HasPermission(tt.groups, tt.tenant, tt.want); got != tt.expected {
				t.Errorf("HasPermission(%v, %q, %s) = %v, want %v",
					tt.groups, tt.tenant, tt.want, got, tt.expected)
			}
		})
	}
}

func TestOpenModeReadOnly(t *testing.T) {
	t.Parallel()
	// Empty config (open mode) allows read, denies write
	m := NewForTest(&RBACConfig{})

	if !m.HasPermission([]string{"any"}, "any-tenant", PermRead) {
		t.Error("open mode should allow read")
	}
	if m.HasPermission([]string{"any"}, "any-tenant", PermWrite) {
		t.Error("open mode should deny write")
	}
}
