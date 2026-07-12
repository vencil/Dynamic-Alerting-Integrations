package rbac

// ADR-027 / LD-6 P3 — the claims-aware match: block.
//
// This file pins the HIGH-risk surface of the P3 change (the rbac gate is
// the only enforcement layer, so a match-evaluation bug is an authorization
// hole):
//
//  1. Exhaustive match evaluation (table-driven): groups-only / claims-only
//     / groups+claims AND / multi-value OR / missing claim / claim value
//     mismatch / multi-rule union / legacy+match mix / nil principal / nil
//     claims / match-rule name is a pure label.
//  2. Fail-closed guardrails: empty match never matches (defense-in-depth
//     at evaluation, on top of the load-time validation error).
//  3. validateConfig, every branch.
//  4. Strict YAML parsing (KnownFields): a `mach:` typo, an unknown
//     top-level key, an unknown rule/match field are LOAD errors — a
//     silently-dropped match block would widen access. Empty/comment-only
//     files still load as the empty config.
//  5. Load semantics: an invalid config fails NewManager (main treats that
//     as fatal); an invalid config arriving via hot-reload keeps the
//     last-good snapshot.
//  6. End-to-end: trusted-hop headers → HeaderResolver claims → match
//     authorization through the real Middleware.

import (
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
)

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

func TestMatch_Evaluation_Exhaustive(t *testing.T) {
	t.Parallel()
	m := NewForTest(matchEvalConfig())

	vp := func(groups []string, claims map[string]string) *VerifiedPrincipal {
		return &VerifiedPrincipal{Groups: groups, Claims: claims}
	}

	cases := []struct {
		name   string
		p      *VerifiedPrincipal
		tenant string
		want   Permission
		expect bool
	}{
		// nil principal (anonymous): no rule can ever match.
		{"nil principal denied read", nil, "ops-1", PermRead, false},
		{"nil principal denied write", nil, "alpha-1", PermWrite, false},

		// Groups-only match.
		{"groups-only hit", vp([]string{"operators"}, nil), "ops-1", PermRead, true},
		{"groups-only OR-within second entry", vp([]string{"sre"}, nil), "ops-1", PermRead, true},
		{"groups-only wrong tenant", vp([]string{"operators"}, nil), "other-1", PermRead, false},
		{"groups-only grants no write", vp([]string{"operators"}, nil), "ops-1", PermWrite, false},
		{"groups-only no matching group", vp([]string{"viewers"}, nil), "ops-1", PermRead, false},

		// Claims-only match, multi-value OR.
		{"claims-only first value", vp(nil, map[string]string{"org": "ORG-A"}), "any-tenant", PermRead, true},
		{"claims-only second value (OR-within)", vp(nil, map[string]string{"org": "ORG-B"}), "any-tenant", PermRead, true},
		{"claims-only value mismatch", vp(nil, map[string]string{"org": "ORG-C"}), "any-tenant", PermRead, false},
		{"claims-only read is not write", vp(nil, map[string]string{"org": "ORG-A"}), "any-tenant", PermWrite, false},
		{"claims-only with empty groups slice", vp([]string{}, map[string]string{"org": "ORG-B"}), "any-tenant", PermRead, true},

		// Groups AND claims.
		{"AND both hold", vp([]string{"operators"}, map[string]string{"org": "ORG-A"}), "alpha-1", PermWrite, true},
		{"AND missing claim (nil claims) fail-closed", vp([]string{"operators"}, nil), "alpha-1", PermWrite, false},
		{"AND missing claim key fail-closed", vp([]string{"operators"}, map[string]string{"region": "eu-1"}), "alpha-1", PermWrite, false},
		{"AND claim value mismatch", vp([]string{"operators"}, map[string]string{"org": "ORG-C"}), "alpha-1", PermWrite, false},
		{"AND group condition fails despite claim", vp([]string{"viewers"}, map[string]string{"org": "ORG-A"}), "alpha-1", PermWrite, false},

		// Two claim keys AND-across, OR-within each list.
		{"two claim keys both hold", vp(nil, map[string]string{"org": "ORG-A", "region": "eu-1"}), "eu-9", PermWrite, true},
		{"two claim keys OR-within second list", vp(nil, map[string]string{"org": "ORG-A", "region": "eu-2"}), "eu-9", PermWrite, true},
		{"two claim keys one missing", vp(nil, map[string]string{"org": "ORG-A"}), "eu-9", PermWrite, false},
		{"two claim keys one mismatched", vp(nil, map[string]string{"org": "ORG-A", "region": "us-1"}), "eu-9", PermWrite, false},

		// A match-rule's NAME is a pure label: being in an IdP group named
		// like the rule must NOT match it.
		{"match-rule name as group is not a hit", vp([]string{"org-a-operators"}, nil), "alpha-1", PermWrite, false},
		{"match-rule name as group is not a hit (groups-only rule)", vp([]string{"ops-rule"}, nil), "ops-1", PermRead, false},

		// Legacy rule still matches by name on the same code path.
		{"legacy rule by name", vp([]string{"legacy-admins"}, nil), "any-tenant", PermAdmin, true},
		{"legacy rule unaffected by claims", vp([]string{"legacy-admins"}, map[string]string{"org": "ORG-C"}), "any-tenant", PermAdmin, true},

		// Multi-rule union: permissions accumulate across matched rules.
		{"union: write via AND rule", vp([]string{"operators"}, map[string]string{"org": "ORG-A"}), "alpha-1", PermWrite, true},
		{"union: read outside alpha via claims-only rule", vp([]string{"operators"}, map[string]string{"org": "ORG-A"}), "zzz-1", PermRead, true},
		{"union: read on ops via groups-only rule", vp([]string{"operators"}, map[string]string{"org": "ORG-A"}), "ops-1", PermRead, true},
		{"union does not invent admin", vp([]string{"operators"}, map[string]string{"org": "ORG-A"}), "alpha-1", PermAdmin, false},
		{"legacy+match mix", vp([]string{"legacy-admins", "operators"}, map[string]string{"org": "ORG-A"}), "alpha-1", PermAdmin, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := m.Allowed(tc.p, tc.tenant, tc.want); got != tc.expect {
				t.Errorf("Allowed(%+v, %q, %s) = %v, want %v", tc.p, tc.tenant, tc.want, got, tc.expect)
			}
		})
	}
}

// TestMatch_RulesMatching pins the /me-facing view onto the same predicate:
// matched rule NAMES, including match-block hits, nothing else.
func TestMatch_RulesMatching(t *testing.T) {
	t.Parallel()
	m := NewForTest(matchEvalConfig())

	names := func(rules []GroupRule) []string {
		var out []string
		for _, r := range rules {
			out = append(out, r.Name)
		}
		return out
	}

	got := names(m.RulesMatching(&VerifiedPrincipal{
		Groups: []string{"operators"},
		Claims: map[string]string{"org": "ORG-A"},
	}))
	want := []string{"ops-rule", "org-readers", "org-a-operators"}
	if len(got) != len(want) {
		t.Fatalf("RulesMatching names = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("RulesMatching names = %v, want %v", got, want)
		}
	}

	if got := m.RulesMatching(nil); len(got) != 0 {
		t.Errorf("RulesMatching(nil) = %v, want none", names(got))
	}
	if got := names(m.RulesMatching(&VerifiedPrincipal{Groups: []string{"legacy-admins"}})); len(got) != 1 || got[0] != "legacy-admins" {
		t.Errorf("RulesMatching(legacy-admins) = %v, want [legacy-admins]", got)
	}
}

// TestMatch_MetadataAndAccessibleSets: the shared predicate drives the other
// evaluation methods too — a claims-matched rule contributes its
// environment/domain scope exactly like a name-matched one.
func TestMatch_MetadataAndAccessibleSets(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{Groups: []GroupRule{
		{Name: "env-scoped-match", Match: &MatchBlock{Claims: map[string][]string{"org": {"ORG-A"}}},
			Tenants: []string{"*"}, Permissions: []Permission{PermRead},
			Environments: []string{"production"}, Domains: []string{"finance"}},
		{Name: "legacy-unscoped", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
	}})

	claimed := &VerifiedPrincipal{Claims: map[string]string{"org": "ORG-A"}}
	unclaimed := &VerifiedPrincipal{Claims: map[string]string{"org": "ORG-Z"}}

	if got := m.AccessibleEnvironmentsFor(claimed); len(got) != 1 || got[0] != "production" {
		t.Errorf("AccessibleEnvironmentsFor(claimed) = %v, want [production]", got)
	}
	if got := m.AccessibleDomainsFor(claimed); len(got) != 1 || got[0] != "finance" {
		t.Errorf("AccessibleDomainsFor(claimed) = %v, want [finance]", got)
	}
	if got := m.AccessibleEnvironmentsFor(unclaimed); len(got) != 0 {
		t.Errorf("AccessibleEnvironmentsFor(unclaimed) = %v, want empty", got)
	}

	// Labeled-tenant membership is mode-independent (both shadow and enforce).
	if !m.MetadataAllowed(claimed, "t1", "production", "finance") {
		t.Error("MetadataAllowed(claimed, production/finance) = false, want true")
	}
	if m.MetadataAllowed(claimed, "t1", "staging", "finance") {
		t.Error("MetadataAllowed(claimed, staging) = true, want false (env outside the matched rule's scope)")
	}
	if m.MetadataAllowed(unclaimed, "t1", "production", "finance") {
		t.Error("MetadataAllowed(unclaimed) = true, want false (no rule matches)")
	}

	// The legacy-unscoped rule keeps its wildcard semantics by name.
	legacy := &VerifiedPrincipal{Groups: []string{"legacy-unscoped"}}
	if got := m.AccessibleEnvironmentsFor(legacy); got != nil {
		t.Errorf("AccessibleEnvironmentsFor(legacy) = %v, want nil (no restriction)", got)
	}
}

// TestMatch_EmptyMatchNeverMatches: validateConfig rejects an empty match at
// load, but a snapshot injected around the loader (NewForTest / Override)
// bypasses validation — the evaluator itself must fail closed, because the
// only wrong default for "empty match" in an enforcement layer is match-all.
func TestMatch_EmptyMatchNeverMatches(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{Groups: []GroupRule{
		{Name: "trap", Match: &MatchBlock{}, Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
	}})

	principals := map[string]*VerifiedPrincipal{
		"nil":             nil,
		"with groups":     {Groups: []string{"anything", "trap"}},
		"with claims":     {Claims: map[string]string{"org": "ORG-A"}},
		"groups+claims":   {Groups: []string{"trap"}, Claims: map[string]string{"org": "ORG-A"}},
		"empty principal": {},
	}
	for name, p := range principals {
		if m.Allowed(p, "any-tenant", PermRead) {
			t.Errorf("empty match block matched principal %q — empty match must NEVER be match-all", name)
		}
	}
}

// --- validateConfig branches ---

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

// --- strict parsing (KnownFields) + load semantics ---

const matchLoadYAML = `groups:
  - name: org-a-operators
    match:
      groups: [operators]
      claims:
        org: [ORG-A]
    tenants: ["*"]
    permissions: [read, write]
`

func TestParse_StrictRejectsUnknownFields(t *testing.T) {
	t.Parallel()
	declared := map[string]string{"org": "X-Auth-Request-Org"}

	cases := []struct {
		name    string
		yaml    string
		wantErr string
	}{
		{"mach typo for match", "groups:\n  - name: r\n    mach:\n      groups: [ops]\n    tenants: [\"*\"]\n    permissions: [read]\n", "mach"},
		{"unknown top-level key", "grops:\n  - name: r\n", "grops"},
		{"unknown rule field", "groups:\n  - name: r\n    tenat: [\"*\"]\n    permissions: [read]\n", "tenat"},
		{"unknown match field", "groups:\n  - name: r\n    match:\n      claim:\n        org: [ORG-A]\n    tenants: [\"*\"]\n    permissions: [read]\n", "claim"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", tc.yaml)
			_, err := NewManager(rbacFile, declared)
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("NewManager = %v, want strict-parse error mentioning %q (a silently-ignored key would WIDEN access)", err, tc.wantErr)
			}
		})
	}
}

// TestParse_RejectsNullMatchBlock pins the fail-closed guard for a
// present-but-null `match:` — the form that decodes to a nil *MatchBlock and
// would otherwise slip past validateConfig (which keys off that nil pointer)
// and silently revert the rule to legacy group-name matching, dropping its
// claim scoping. `match: {}` is covered separately by TestValidateConfig_Branches
// (empty-match); this test covers the null forms + the legacy/populated
// negatives that must still load.
func TestParse_RejectsNullMatchBlock(t *testing.T) {
	t.Parallel()
	declared := map[string]string{"org": "X-Auth-Request-Org"}

	reject := map[string]string{
		"bare null match":       "groups:\n  - name: operators\n    match:\n    tenants: [\"*\"]\n    permissions: [read, write]\n",
		"explicit null match":   "groups:\n  - name: operators\n    match: null\n    tenants: [\"*\"]\n    permissions: [read, write]\n",
		"comment-only children": "groups:\n  - name: operators\n    match:\n      # claims:\n      #   org: [ORG-A]\n    tenants: [\"*\"]\n    permissions: [read, write]\n",
	}
	for name, content := range reject {
		t.Run("reject/"+name, func(t *testing.T) {
			t.Parallel()
			_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", content)
			_, err := NewManager(rbacFile, declared)
			if err == nil || !strings.Contains(err.Error(), "present but null") {
				t.Fatalf("NewManager = %v, want a 'present but null' load error — a bare match: must NOT silently degrade to legacy group matching", err)
			}
		})
	}

	// Negatives: absent match (legacy) and a populated match must still load.
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

// Initial-load failures = NewManager error (main.go treats it as FATAL: the
// rbac gate is the only enforcement layer, an unparseable/invalid policy is
// not safe to serve).
func TestNewManager_InvalidMatchConfigIsAnError(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name         string
		yaml         string
		claimHeaders map[string]string
		wantErr      string
	}{
		{"undeclared claim key", matchLoadYAML, nil, "not declared"},
		{"empty match block", "groups:\n  - name: r\n    match: {}\n    tenants: [\"*\"]\n    permissions: [read]\n", nil, "empty match block"},
		{"malformed tenant pattern", "groups:\n  - name: r\n    tenants: [\"**\"]\n    permissions: [read]\n", nil, "invalid tenant pattern"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", tc.yaml)
			_, err := NewManager(rbacFile, tc.claimHeaders)
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("NewManager = %v, want error containing %q", err, tc.wantErr)
			}
		})
	}
}

// Hot-reload failures keep the last-good snapshot: Reload returns the error
// (WatchLoop logs it as a WARN) and Get still serves the previous config —
// verified for a strict-parse typo, an undeclared claim key and an empty
// match block.
func TestReload_InvalidConfigKeepsLastGood(t *testing.T) {
	t.Parallel()
	declared := map[string]string{"org": "X-Auth-Request-Org"}
	granted := &VerifiedPrincipal{Groups: []string{"operators"}, Claims: map[string]string{"org": "ORG-A"}}

	badConfigs := map[string]string{
		"strict-parse typo":    "groups:\n  - name: org-a-operators\n    mach:\n      groups: [operators]\n    tenants: [\"*\"]\n    permissions: [read]\n",
		"undeclared claim key": "groups:\n  - name: r\n    match:\n      claims:\n        region: [eu-1]\n    tenants: [\"*\"]\n    permissions: [read]\n",
		"empty match block":    "groups:\n  - name: r\n    match: {}\n    tenants: [\"*\"]\n    permissions: [read]\n",
	}
	for name, bad := range badConfigs {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", matchLoadYAML)
			m, err := NewManager(rbacFile, declared)
			if err != nil {
				t.Fatalf("NewManager: %v", err)
			}
			if !m.Allowed(granted, "any-tenant", PermWrite) {
				t.Fatal("precondition failed: initial config must grant write")
			}

			if err := os.WriteFile(rbacFile, []byte(bad), 0o600); err != nil {
				t.Fatalf("write bad config: %v", err)
			}
			if err := m.Reload(); err == nil {
				t.Fatal("Reload = nil, want an error for the invalid config")
			}
			// Last-good is still served: same rule count, same decision.
			if got := len(m.Get().Groups); got != 1 {
				t.Errorf("Groups after failed reload = %d, want 1 (last-good)", got)
			}
			if !m.Allowed(granted, "any-tenant", PermWrite) {
				t.Error("Allowed after failed reload = false, want true (last-good must keep serving)")
			}
		})
	}
}

// End-to-end through the real middleware: trusted-hop headers → HeaderResolver
// claims → match evaluation. The full P2+P3 chain, no synthetic principals.
func TestMiddleware_MatchRule_EndToEnd(t *testing.T) {
	t.Parallel()
	_, rbacFile := testutil.MkTempYAML(t, "_rbac.yaml", matchLoadYAML)
	m, err := NewManager(rbacFile, map[string]string{"org": "X-Auth-Request-Org"})
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	mw := m.Middleware(PermWrite, func(*http.Request) string { return "any-tenant" })(inner)

	serve := func(orgHeader string) int {
		req := httptest.NewRequest("PUT", "/api/v1/tenants/any-tenant", nil)
		req.Header.Set("X-Forwarded-Email", "op@example.com")
		req.Header.Set("X-Forwarded-Groups", "operators")
		if orgHeader != "" {
			req.Header.Set("X-Auth-Request-Org", orgHeader)
		}
		w := httptest.NewRecorder()
		mw.ServeHTTP(w, req)
		return w.Code
	}

	if got := serve("ORG-A"); got != http.StatusOK {
		t.Errorf("matching claim header: status = %d, want 200", got)
	}
	if got := serve(""); got != http.StatusForbidden {
		t.Errorf("missing claim header: status = %d, want 403 (missing claim fail-closed)", got)
	}
	if got := serve("ORG-Z"); got != http.StatusForbidden {
		t.Errorf("mismatched claim value: status = %d, want 403", got)
	}
}
