package rbac

// Initial-load semantics of the rbac config (ADR-027 / LD-6 P3):
//
//  1. validateConfig, every branch (match block + tenant-pattern allowlist).
//  2. Strict YAML parsing (KnownFields): a `mach:` typo, an unknown
//     top-level key, an unknown rule/match field are LOAD errors — a
//     silently-dropped match block would widen access. Empty/comment-only
//     files still load as the empty config.
//  3. NewManager entry: an invalid config fails NewManager (main treats that
//     as fatal); the canonical rejection corpus is invalidConfigTable, shared
//     with the hot-reload behavior test in config_reload_test.go.
//  4. NewManager construction paths: empty path / missing file / valid file /
//     unparseable file, plus the org-scope claim-key declaration checks.

import (
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
)

// invalidTableDeclared is the claim-header declaration every invalidConfigTable
// row is evaluated under: "org" IS declared, "region" deliberately is NOT.
var invalidTableDeclared = map[string]string{"org": "X-Auth-Request-Org"}

// tenantPatternYAML builds a minimal one-rule config whose tenants list is the
// given YAML flow-sequence body.
func tenantPatternYAML(patterns string) string {
	return "groups:\n  - name: r\n    tenants: [" + patterns + "]\n    permissions: [read]\n"
}

// invalidConfigTable is the canonical corpus of configs the loader must
// REJECT, and the substring each error must carry ("" = any non-nil error).
// It drives BOTH load entry points: initial load
// (TestNewManager_RejectsInvalidConfig, this file) and hot-reload
// (TestReload_InvalidKeepsLastGood, config_reload_test.go) — an invalid
// config must fail NewManager AND leave a running manager on last-good.
var invalidConfigTable = []struct {
	name    string
	yaml    string
	wantErr string
}{
	// Strict parsing (KnownFields): a typo'd or unknown key is a LOAD error —
	// a silently-ignored `mach:` block would WIDEN access.
	{"strict-parse: mach typo for match", "groups:\n  - name: r\n    mach:\n      groups: [ops]\n    tenants: [\"*\"]\n    permissions: [read]\n", "mach"},
	{"strict-parse: unknown top-level key", "grops:\n  - name: r\n", "grops"},
	{"strict-parse: unknown rule field", "groups:\n  - name: r\n    tenat: [\"*\"]\n    permissions: [read]\n", "tenat"},
	{"strict-parse: unknown match field", "groups:\n  - name: r\n    match:\n      claim:\n        org: [ORG-A]\n    tenants: [\"*\"]\n    permissions: [read]\n", "claim"},
	// Present-but-null `match:` — decodes to a nil *MatchBlock and would
	// otherwise silently revert the rule to legacy group-name matching,
	// dropping its claim scoping.
	{"null match: bare null match", "groups:\n  - name: operators\n    match:\n    tenants: [\"*\"]\n    permissions: [read, write]\n", "present but null"},
	{"null match: explicit null match", "groups:\n  - name: operators\n    match: null\n    tenants: [\"*\"]\n    permissions: [read, write]\n", "present but null"},
	{"null match: comment-only children", "groups:\n  - name: operators\n    match:\n      # claims:\n      #   org: [ORG-A]\n    tenants: [\"*\"]\n    permissions: [read, write]\n", "present but null"},
	// validateConfig rejections routed through the real load entry points.
	{"empty match block", "groups:\n  - name: r\n    match: {}\n    tenants: [\"*\"]\n    permissions: [read]\n", "empty match block"},
	{"undeclared claim key", "groups:\n  - name: r\n    match:\n      claims:\n        region: [eu-1]\n    tenants: [\"*\"]\n    permissions: [read]\n", "not declared"},
	{"undeclared org-scope key", "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n    org-scope: region\n", "not declared"},
	// Tenant-pattern allowlist. The load-bearing row is "**": it must be
	// rejected here so it never reaches tenantMatches, where it would collapse
	// to prefix "*" and fail open onto a platform-scope "*" gate (see
	// TestTenantMatches / TestTenantPatternInvariants).
	{"tenant pattern: double star", tenantPatternYAML(`"**"`), "invalid tenant pattern"},
	{"tenant pattern: embedded star", tenantPatternYAML(`"*a*"`), "invalid tenant pattern"},
	{"tenant pattern: trailing double star", tenantPatternYAML(`"a**"`), "invalid tenant pattern"},
	{"tenant pattern: leading star (non-suffix)", tenantPatternYAML(`"*a"`), "invalid tenant pattern"},
	{"tenant pattern: mid star", tenantPatternYAML(`"a*b"`), "invalid tenant pattern"},
	{"tenant pattern: empty entry", tenantPatternYAML(`""`), "invalid tenant pattern"},
	{"tenant pattern: whitespace-only entry", tenantPatternYAML(`"   "`), "invalid tenant pattern"},
	{"tenant pattern: one bad entry among good ones", tenantPatternYAML(`"db-a-*", "**"`), "invalid tenant pattern"},
	// Unparseable YAML.
	{"malformed YAML", "{{not valid yaml", ""},
}

// Initial-load failures = NewManager error (main.go treats it as FATAL: the
// rbac gate is the only enforcement layer, an unparseable/invalid policy is
// not safe to serve).
func TestNewManager_RejectsInvalidConfig(t *testing.T) {
	t.Parallel()
	for _, tc := range invalidConfigTable {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", tc.yaml)
			_, err := NewManager(rbacFile, invalidTableDeclared)
			if err == nil {
				t.Fatal("NewManager = nil error, want a load error (an accepted invalid config would silently widen or degrade access)")
			}
			if tc.wantErr != "" && !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("NewManager error = %v, want substring %q", err, tc.wantErr)
			}
		})
	}
}

// --- validateConfig branches (direct unit level) ---

func TestValidateConfig_Branches(t *testing.T) {
	t.Parallel()
	declared := map[string]string{"org": "X-Auth-Request-Org"}

	rule := func(m *MatchBlock) *RBACConfig {
		return &RBACConfig{Groups: []GroupRule{
			{Name: "r", Match: m, Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		}}
	}

	cases := []struct {
		name     string
		cfg      *RBACConfig
		declared map[string]string
		wantErr  string // "" = valid
	}{
		{"no match block is valid", rule(nil), nil, ""},
		{"groups-only match valid without declared keys", rule(&MatchBlock{Groups: []string{"ops"}}), nil, ""},
		{"claims-only match valid with declared key", rule(&MatchBlock{Claims: map[string][]string{"org": {"ORG-A"}}}), declared, ""},
		{"groups+claims valid", rule(&MatchBlock{Groups: []string{"ops"}, Claims: map[string][]string{"org": {"ORG-A"}}}), declared, ""},
		{"empty match block rejected", rule(&MatchBlock{}), declared, "empty match block"},
		{"undeclared claim key rejected (nil declared)", rule(&MatchBlock{Claims: map[string][]string{"org": {"ORG-A"}}}), nil, "not declared"},
		{"undeclared claim key rejected (other key declared)", rule(&MatchBlock{Claims: map[string][]string{"region": {"eu-1"}}}), declared, "not declared"},
		{"empty claim value list rejected", rule(&MatchBlock{Claims: map[string][]string{"org": {}}}), declared, "empty value list"},
		{"blank claim value rejected", rule(&MatchBlock{Claims: map[string][]string{"org": {"ORG-A", "  "}}}), declared, "empty value"},
		{"blank match.groups entry rejected", rule(&MatchBlock{Groups: []string{"ops", ""}}), nil, "empty entry"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			err := validateConfig(tc.cfg, tc.declared)
			if tc.wantErr == "" {
				if err != nil {
					t.Errorf("validateConfig = %v, want nil", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("validateConfig = %v, want error containing %q", err, tc.wantErr)
			}
		})
	}
}

// TestValidateConfig_TenantPatterns: tenant match patterns are allowlist-checked
// at load (independently of the match block). A well-formed pattern is "*", a
// single-trailing-"*" prefix, or an exact id; everything else fails loud. The
// load-bearing case is "**": it must be rejected here so it never reaches
// tenantMatches, where it would collapse to prefix "*" and fail open onto a
// platform-scope "*" gate (see TestTenantMatches).
func TestValidateConfig_TenantPatterns(t *testing.T) {
	t.Parallel()

	cfg := func(tenants ...string) *RBACConfig {
		return &RBACConfig{Groups: []GroupRule{
			{Name: "r", Tenants: tenants, Permissions: []Permission{PermRead}},
		}}
	}

	cases := []struct {
		name    string
		cfg     *RBACConfig
		wantErr string // "" = valid
	}{
		{"full wildcard valid", cfg("*"), ""},
		{"exact id valid", cfg("db-a"), ""},
		{"prefix pattern valid", cfg("db-a-*"), ""},
		{"multiple valid patterns", cfg("*", "db-a", "db-b-*"), ""},
		{"absent tenants valid", &RBACConfig{Groups: []GroupRule{{Name: "r", Permissions: []Permission{PermRead}}}}, ""},
		{"double star rejected", cfg("**"), "invalid tenant pattern"},
		{"embedded star rejected", cfg("*a*"), "invalid tenant pattern"},
		{"trailing double star rejected", cfg("a**"), "invalid tenant pattern"},
		{"leading star (non-suffix) rejected", cfg("*a"), "invalid tenant pattern"},
		{"mid star rejected", cfg("a*b"), "invalid tenant pattern"},
		{"empty entry rejected", cfg(""), "invalid tenant pattern"},
		{"whitespace-only entry rejected", cfg("   "), "invalid tenant pattern"},
		{"one bad entry among good ones rejected", cfg("db-a-*", "**"), "invalid tenant pattern"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			err := validateConfig(tc.cfg, nil)
			if tc.wantErr == "" {
				if err != nil {
					t.Errorf("validateConfig = %v, want nil", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("validateConfig = %v, want error containing %q", err, tc.wantErr)
			}
		})
	}
}

// The null-match rejection rows of invalidConfigTable have two negatives that
// must still load: an absent match (the legacy rule shape) and a populated
// match block — the rejection must be exactly the present-but-null form.
func TestParse_NullMatchNegativesStillLoad(t *testing.T) {
	t.Parallel()
	declared := map[string]string{"org": "X-Auth-Request-Org"}

	accept := map[string]string{
		"absent match (legacy)": "groups:\n  - name: operators\n    tenants: [\"*\"]\n    permissions: [read, write]\n",
		"populated match":       "groups:\n  - name: r\n    match:\n      claims:\n        org: [ORG-A]\n    tenants: [\"*\"]\n    permissions: [read]\n",
	}
	for name, content := range accept {
		t.Run("accept/"+name, func(t *testing.T) {
			t.Parallel()
			_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", content)
			if _, err := NewManager(rbacFile, declared); err != nil {
				t.Fatalf("NewManager = %v, want nil (this is a valid config)", err)
			}
		})
	}
}

func TestParse_EmptyAndCommentOnlyFilesStillLoad(t *testing.T) {
	t.Parallel()
	for name, content := range map[string]string{
		"empty file":        "",
		"comment-only file": "# no rules yet\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", content)
			m, err := NewManager(rbacFile, nil)
			if err != nil {
				t.Fatalf("NewManager = %v, want nil (the strict decoder must keep the lenient parser's empty-document behavior)", err)
			}
			if got := len(m.Get().Groups); got != 0 {
				t.Errorf("Groups = %d, want 0", got)
			}
		})
	}
}

func TestNewManager_MatchConfigLoadsAndEvaluates(t *testing.T) {
	t.Parallel()
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", matchLoadYAML)
	m, err := NewManager(rbacFile, map[string]string{"org": "X-Auth-Request-Org"})
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	granted := &VerifiedPrincipal{Groups: []string{"operators"}, Claims: map[string]string{"org": "ORG-A"}}
	if !m.Allowed(granted, "any-tenant", PermWrite) {
		t.Error("Allowed(matching principal, write) = false, want true (YAML-loaded match rule)")
	}
	if m.Allowed(&VerifiedPrincipal{Groups: []string{"operators"}}, "any-tenant", PermWrite) {
		t.Error("Allowed(claimless principal, write) = true, want false (missing claim fail-closed)")
	}
}

// --- NewManager construction paths ---

func TestNewManager_EmptyPath(t *testing.T) {
	t.Parallel()
	m, err := NewManager("", nil)
	if err != nil {
		t.Fatalf("NewManager('') returned error: %v", err)
	}
	cfg := m.Get()
	if len(cfg.Groups) != 0 {
		t.Errorf("expected empty groups in open mode, got %d", len(cfg.Groups))
	}
}

func TestNewManager_FileNotFound(t *testing.T) {
	t.Parallel()
	m, err := NewManager("/nonexistent/path/_rbac.yaml", nil)
	if err != nil {
		t.Fatalf("NewManager(nonexistent) returned error: %v", err)
	}
	// Should fall back to open-read mode
	cfg := m.Get()
	if len(cfg.Groups) != 0 {
		t.Errorf("expected empty groups for missing file, got %d", len(cfg.Groups))
	}
}

func TestNewManager_ValidFile(t *testing.T) {
	t.Parallel()
	content := `groups:
  - name: admins
    tenants: ["*"]
    permissions: [admin]
  - name: db-ops
    tenants: ["db-a-*", "db-b-*"]
    permissions: [read, write]
`
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", content)

	m, err := NewManager(rbacFile, nil)
	if err != nil {
		t.Fatalf("NewManager returned error: %v", err)
	}
	cfg := m.Get()
	if len(cfg.Groups) != 2 {
		t.Errorf("expected 2 groups, got %d", len(cfg.Groups))
	}
}

func TestNewManager_InvalidYAML(t *testing.T) {
	t.Parallel()
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", "{{not valid yaml")

	_, err := NewManager(rbacFile, nil)
	if err == nil {
		t.Error("expected error for invalid YAML, got nil")
	}
}

// --- validateConfig: org-scope key must be a declared claim header ---
// (The hot-reload half of this pin is the "undeclared org-scope key" row of
// invalidConfigTable, exercised by TestReload_InvalidKeepsLastGood.)

func TestNewManager_OrgScopeValidation(t *testing.T) {
	t.Parallel()
	declared := map[string]string{"org": "X-Auth-Request-Org"}

	orgScopedYAML := "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n    org-scope: org\n"
	// org-scope on a LEGACY name-matched rule (no match block) — proves the
	// check runs regardless of the match block.
	orgScopedLegacyYAML := orgScopedYAML

	t.Run("undeclared org-scope key is a load error", func(t *testing.T) {
		t.Parallel()
		_, path := testutil.MkTempYAML(t, "_rbac.yaml", orgScopedLegacyYAML)
		_, err := NewManager(path, nil) // "org" NOT declared
		if err == nil || !strings.Contains(err.Error(), "not declared") {
			t.Errorf("NewManager = %v, want error containing \"not declared\"", err)
		}
	})

	t.Run("declared org-scope key loads and evaluates", func(t *testing.T) {
		t.Parallel()
		_, path := testutil.MkTempYAML(t, "_rbac.yaml", orgScopedYAML)
		m, err := NewManager(path, declared)
		if err != nil {
			t.Fatalf("NewManager: %v", err)
		}
		p := &VerifiedPrincipal{Groups: []string{"ops"}, Claims: map[string]string{"org": "ORG-A"}}
		if !m.ScopeAllowed(p, "db-a", "", "", []string{"ORG-A"}) {
			t.Error("declared org-scope: same-org tenant must be visible")
		}
	})

	t.Run("empty org-scope (omitted) is not checked", func(t *testing.T) {
		t.Parallel()
		_, path := testutil.MkTempYAML(t, "_rbac.yaml", "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n")
		if _, err := NewManager(path, nil); err != nil {
			t.Errorf("rule without org-scope must load with no claim headers, got %v", err)
		}
	})

	t.Run("strict parse rejects an org-scope typo key", func(t *testing.T) {
		t.Parallel()
		_, path := testutil.MkTempYAML(t, "_rbac.yaml", "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read]\n    org-scop: org\n")
		if _, err := NewManager(path, declared); err == nil {
			t.Error("NewManager must reject the unknown field org-scop (strict KnownFields)")
		}
	})
}
