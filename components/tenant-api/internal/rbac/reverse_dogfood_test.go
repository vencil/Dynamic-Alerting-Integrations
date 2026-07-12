package rbac

// Dogfood drift-invariant suite for the reverse access report (ADR-027 / LD-6
// P6, spec §5). The reverse report BORROWS the forward predicates; these tests
// pin that the borrowing cannot drift:
//
//   §5.1 Witness-positive (RULE-ISOLATED): every emitted grant, replayed as a
//        synthesized witness against a Manager holding ONLY that rule, must be
//        granted by the real forward gates in both fail-modes. Isolation is
//        load-bearing (round-1 C3): on the full config another rule could
//        supply the allow and mask a rendering error in THIS grant.
//   §5.2 Completeness property (正⟹逆 — the direction fatal to an audit):
//        seeded-fuzz principals from the config vocabulary; whenever a forward
//        gate grants, the report must contain an explaining grant — decided by
//        replaying the SAME forward gate on the grant's single-rule sub-config,
//        never by a re-implemented WHO matcher. Fixtures include the open-mode
//        and fail-closed-empty Managers (round-1 C4), where the explanation is
//        the mode itself.
//   §5.3 Adversarial case table (fixed inclusion list).
//   §5.6 Golden: try-local seed shape pins schema + ordering; a redacted
//        golden pins the allowlist projection.
//
// Bar unit tests (org-scoped wildcard admin → false; non-org-scoped "*" admin
// → true; no "*" admin → false; both zero-group states → false) live in
// reverse_smoke_test.go (TestPlatformAdminNonOrgScoped_Bar); this file adds
// the match-block and malformed-pattern bar cases.

import (
	"encoding/json"
	"math/rand"
	"reflect"
	"strings"
	"testing"
)

// dogfoodConfig is the §5.1/§5.2 fixture: every WHO shape and org-gate
// topology the report can render, all validateConfig-legal (so every grant is
// witness-satisfiable). Synthetic ids throughout (dev-rule #2).
func dogfoodConfig() *RBACConfig {
	return &RBACConfig{Groups: []GroupRule{
		// 0: legacy, platform-wide admin.
		{Name: "platform-admins", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
		// 1: legacy, prefix pattern, read+write.
		{Name: "team-writers", Tenants: []string{"db-*"}, Permissions: []Permission{PermRead, PermWrite}},
		// 2: match block (groups + claims on a NON-org key) + org scope.
		{Name: "org-ops", Match: &MatchBlock{
			Groups: []string{"ops"},
			Claims: map[string][]string{"dept": {"dba", "sre"}},
		}, Tenants: []string{"db-*"}, Permissions: []Permission{PermWrite}, OrgScope: "org"},
		// 3: OrgScope × match.claims on the SAME key, intersection NON-EMPTY
		//    for the labeled fixture tenant ({ORG-1,ORG-9} ∩ {ORG-1,ORG-2}).
		{Name: "org-pinned", Match: &MatchBlock{
			Claims: map[string][]string{"org": {"ORG-1", "ORG-2"}},
		}, Tenants: []string{"db-*"}, Permissions: []Permission{PermRead}, OrgScope: "org"},
		// 4: same shape, intersection EMPTY for the labeled fixture tenant →
		//    unsatisfiable (round-1 C2).
		{Name: "org-disjoint", Match: &MatchBlock{
			Claims: map[string][]string{"org": {"ORG-X"}},
		}, Tenants: []string{"db-*"}, Permissions: []Permission{PermRead}, OrgScope: "org"},
		// 5+6: SAME-NAME pair (round-1 C9) — identity must be the config index.
		{Name: "dup", Tenants: []string{"db-team-1"}, Permissions: []Permission{PermRead}},
		{Name: "dup", Tenants: []string{"db-*"}, Permissions: []Permission{PermRead, PermWrite, PermAdmin},
			Environments: []string{"production"}},
	}}
}

// singleRuleManagers builds the shadow/enforce Manager pair whose config holds
// ONLY rule — the §5.1 isolation harness.
func singleRuleManagers(rule GroupRule) (shadow, enforce *Manager) {
	shadow = NewForTest(&RBACConfig{Groups: []GroupRule{rule}})
	enforce = NewForTest(&RBACConfig{Groups: []GroupRule{rule}})
	enforce.EnableOrgScopeEnforce()
	return shadow, enforce
}

// witnessFor synthesizes the minimal principal satisfying grant g's WHO, per
// the spec §5.1 synthesis rules: legacy → the legacy group; match block → the
// first groups_any_of entry (when present) plus, for EVERY claims_all_of key,
// any allowed value; org-scoped → claims[claim_key] = a passing org value.
// When the org claim key is ALSO a claims_all_of key, the value must come
// from the intersection — passing_org_values IS that intersection (derived by
// enumeration), so pinning passing[0] last satisfies both constraints.
func witnessFor(g ReverseGrant) *VerifiedPrincipal {
	p := &VerifiedPrincipal{Claims: map[string]string{}}
	switch g.Who.Kind {
	case WhoKindLegacyGroup:
		p.Groups = []string{g.Who.LegacyGroup}
	case WhoKindMatchBlock:
		if len(g.Who.GroupsAnyOf) > 0 {
			p.Groups = []string{g.Who.GroupsAnyOf[0]}
		}
		for key, allowed := range g.Who.ClaimsAllOf {
			p.Claims[key] = allowed[0]
		}
	}
	if g.OrgGate.Required && len(g.OrgGate.PassingOrgValues) > 0 {
		p.Claims[g.OrgGate.ClaimKey] = g.OrgGate.PassingOrgValues[0]
	}
	return p
}

// ── §5.1 witness-positive, rule-isolated ────────────────────────────────────

func TestReverseReport_WitnessPositiveRuleIsolated(t *testing.T) {
	t.Parallel()
	cfg := dogfoodConfig()
	tenantCases := []struct {
		name  string
		id    string
		orgs  []string
		known bool
	}{
		{"labeled tenant", "db-team-1", []string{"ORG-1", "ORG-9"}, true},
		{"unlabeled tenant", "db-team-2", []string{}, true},
	}
	for _, tc := range tenantCases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			m := NewForTest(cfg)
			rep := m.ReverseAccessReport(tc.id, tc.orgs, tc.known, "h",
				ReverseReportOptions{IncludeOrgValues: true})
			if len(rep.Grants) == 0 {
				t.Fatal("fixture must yield grants")
			}
			for _, g := range rep.Grants {
				rule := cfg.Groups[g.Index] // grant identity = config index
				shadowM, enforceM := singleRuleManagers(rule)

				// Unsatisfiable claim (labeled tenant, empty passing domain):
				// verify NEGATIVELY by exhausting the candidate value space —
				// every tenant org and every value the rule's own claims pin.
				if g.OrgGate.Required && g.OrgGate.Unsatisfiable {
					cands := append(append([]string{}, tc.orgs...),
						g.Who.ClaimsAllOf[g.OrgGate.ClaimKey]...)
					for _, v := range cands {
						w := witnessFor(g)
						w.Claims[g.OrgGate.ClaimKey] = v
						if shadowM.AllowedInOrgRead(w, tc.id, PermRead, tc.orgs) ||
							enforceM.AllowedInOrgRead(w, tc.id, PermRead, tc.orgs) {
							t.Errorf("grant %d (%s): claimed unsatisfiable but org value %q passes forward",
								g.Index, g.Rule, v)
						}
					}
					continue
				}

				// Project the expected org-gate pass per mode from the grant's
				// dual outcomes (the witness carries a PASSING org value for
				// conditional gates, so conditional projects to pass).
				gatePass := func(outcome string) bool {
					switch outcome {
					case OrgOutcomeNotRequired, OrgOutcomeConditional, OrgOutcomePassUnlabeled:
						return true
					case OrgOutcomeFailUnlabeled:
						return false
					default:
						t.Fatalf("grant %d: unknown org outcome %q", g.Index, outcome)
						return false
					}
				}
				shadowGate := gatePass(g.OrgGate.OutcomeShadow)
				enforceGate := gatePass(g.OrgGate.OutcomeEnforce)

				w := witnessFor(g)
				perms := []struct {
					perm Permission
					eff  bool
				}{
					{PermRead, g.Effective.Read},
					{PermWrite, g.Effective.Write},
					{PermAdmin, g.Effective.Admin},
				}
				for _, pc := range perms {
					wantShadow := pc.eff && shadowGate
					wantEnforce := pc.eff && enforceGate
					if got := shadowM.AllowedInOrg(w, tc.id, pc.perm, tc.orgs); got != wantShadow {
						t.Errorf("grant %d (%s) perm=%s: shadow AllowedInOrg = %v, report claims %v",
							g.Index, g.Rule, pc.perm, got, wantShadow)
					}
					if got := enforceM.AllowedInOrg(w, tc.id, pc.perm, tc.orgs); got != wantEnforce {
						t.Errorf("grant %d (%s) perm=%s: enforce AllowedInOrg = %v, report claims %v",
							g.Index, g.Rule, pc.perm, got, wantEnforce)
					}
					// Read plane shares the decision core (only the metric axis
					// differs) — it must agree with the write-plane replay.
					if got := shadowM.AllowedInOrgRead(w, tc.id, pc.perm, tc.orgs); got != wantShadow {
						t.Errorf("grant %d perm=%s: shadow AllowedInOrgRead = %v, want %v",
							g.Index, pc.perm, got, wantShadow)
					}
					if got := enforceM.AllowedInOrgRead(w, tc.id, pc.perm, tc.orgs); got != wantEnforce {
						t.Errorf("grant %d perm=%s: enforce AllowedInOrgRead = %v, want %v",
							g.Index, pc.perm, got, wantEnforce)
					}
					// Org-blind Allowed: the org axis is invisible (tenantOrgs=nil
					// degenerates the gate), so it must equal the bare effective bit.
					if got := shadowM.Allowed(w, tc.id, pc.perm); got != pc.eff {
						t.Errorf("grant %d perm=%s: org-blind Allowed = %v, report effective %v",
							g.Index, pc.perm, got, pc.eff)
					}
				}
			}
		})
	}
}

// ── §5.2 completeness property (正⟹逆) ─────────────────────────────────────

// seededPrincipals derives the fuzz corpus from the dogfoodConfig vocabulary
// (all group names, claim keys/values, org values) ∪ {absent, out-of-vocab},
// with a FIXED seed and fixed claim-key iteration order so a failure is
// reproducible byte-for-byte.
func seededPrincipals() []*VerifiedPrincipal {
	rng := rand.New(rand.NewSource(20260712)) // fixed seed: deterministic fuzz, reproducible failures
	groups := []string{
		"platform-admins", "team-writers", "org-ops", "org-pinned",
		"org-disjoint", "dup", "ops", "no-such-group",
	}
	claimKeys := []string{"dept", "org"} // fixed order — map iteration would break seed determinism
	claimVals := map[string][]string{
		"dept": {"dba", "sre", "intern"},
		"org":  {"ORG-1", "ORG-2", "ORG-9", "ORG-X", "ORG-OUT"},
	}
	ps := []*VerifiedPrincipal{
		nil, // documented anonymous caller
		{Groups: []string{"platform-admins"}},
		{Groups: []string{"ops"}, Claims: map[string]string{"dept": "dba", "org": "ORG-1"}},
	}
	for i := 0; i < 150; i++ {
		p := &VerifiedPrincipal{Claims: map[string]string{}}
		for j, n := 0, rng.Intn(3); j < n; j++ {
			p.Groups = append(p.Groups, groups[rng.Intn(len(groups))])
		}
		for _, key := range claimKeys {
			if rng.Intn(5) < 3 {
				p.Claims[key] = claimVals[key][rng.Intn(len(claimVals[key]))]
			}
		}
		ps = append(ps, p)
	}
	return ps
}

func TestReverseReport_CompletenessProperty(t *testing.T) {
	t.Parallel()
	fixtures := []struct {
		name       string
		cfg        *RBACConfig
		failClosed bool
	}{
		{"configured", dogfoodConfig(), false},
		{"open", &RBACConfig{}, false},             // round-1 C4: must be in the fixture set
		{"fail-closed-empty", &RBACConfig{}, true}, // or this direction is never tested
	}
	tenants := []struct {
		id    string
		orgs  []string
		known bool
	}{
		{"db-team-1", []string{"ORG-1", "ORG-9"}, true}, // labeled, hits db-* and the literal dup rule
		{"db-team-2", []string{}, true},                 // unlabeled
		{"redis-01", []string{"ORG-2"}, true},           // outside db-*, labeled
		{"svc-edge", nil, false},                        // not onboarded
	}
	principals := seededPrincipals()

	for _, fx := range fixtures {
		for _, enforce := range []bool{false, true} {
			m := NewForTest(fx.cfg)
			m.failClosedOnEmpty = fx.failClosed
			if enforce {
				m.EnableOrgScopeEnforce()
			}
			// Single-rule sub-managers (same flags), lazily built per rule index:
			// the "does this grant explain the allow" question is answered by the
			// SAME forward gate on the isolated rule — never by a re-implemented
			// WHO matcher in test code.
			subs := map[int]*Manager{}
			subFor := func(idx int) *Manager {
				if s, ok := subs[idx]; ok {
					return s
				}
				s := NewForTest(&RBACConfig{Groups: []GroupRule{fx.cfg.Groups[idx]}})
				if enforce {
					s.EnableOrgScopeEnforce()
				}
				subs[idx] = s
				return s
			}
			for _, tn := range tenants {
				rep := m.ReverseAccessReport(tn.id, tn.orgs, tn.known, "h", ReverseReportOptions{})
				// Mode honesty pins (independent of any principal).
				switch {
				case fx.failClosed:
					if rep.Mode != ReverseModeFailClosedEmpty || rep.Verdict != ReverseVerdictFailClosedEmpty {
						t.Fatalf("[%s] mode/verdict = %q/%q, want fail_closed_empty", fx.name, rep.Mode, rep.Verdict)
					}
				case len(fx.cfg.Groups) == 0:
					if rep.Mode != ReverseModeOpenRead || rep.Verdict != ReverseVerdictOpenRead {
						t.Fatalf("[%s] mode/verdict = %q/%q, want open_read", fx.name, rep.Mode, rep.Verdict)
					}
				}
				for pi, p := range principals {
					for _, want := range []Permission{PermRead, PermWrite, PermAdmin} {
						if !m.AllowedInOrg(p, tn.id, want, tn.orgs) {
							continue
						}
						// Forward granted → the report must explain it.
						switch rep.Mode {
						case ReverseModeFailClosedEmpty:
							t.Errorf("[%s enforce=%v tenant=%s p#%d perm=%s] forward granted under fail_closed_empty",
								fx.name, enforce, tn.id, pi, want)
						case ReverseModeOpenRead:
							// The mode IS the explanation — but only for read.
							if want != PermRead {
								t.Errorf("[%s enforce=%v tenant=%s p#%d] open mode granted %s (read-only expected)",
									fx.name, enforce, tn.id, pi, want)
							}
						default:
							explained := false
							for _, g := range rep.Grants {
								if subFor(g.Index).AllowedInOrg(p, tn.id, want, tn.orgs) {
									explained = true
									break
								}
							}
							if !explained {
								t.Errorf("[%s enforce=%v tenant=%s p#%d perm=%s] forward grants but NO report grant explains it (groups=%v claims=%v)",
									fx.name, enforce, tn.id, pi, want, p.Groups, p.Claims)
							}
						}
					}
				}
			}
		}
	}
}

// ── §5.3 adversarial case table (fixed inclusion list) ──────────────────────

func TestReverseReport_AdversarialCases(t *testing.T) {
	t.Parallel()

	t.Run("unlabeled tenant renders the (true,false) leniency as dual outcomes", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(dogfoodConfig())
		rep := m.ReverseAccessReport("db-team-2", []string{}, true, "h",
			ReverseReportOptions{IncludeOrgValues: true})
		if rep.Tenant.OrgStatus != OrgStatusUnlabeled {
			t.Fatalf("org_status = %q, want %q", rep.Tenant.OrgStatus, OrgStatusUnlabeled)
		}
		seen := 0
		for _, g := range rep.Grants {
			if !g.OrgGate.Required {
				continue
			}
			seen++
			if g.OrgGate.OutcomeShadow != OrgOutcomePassUnlabeled || g.OrgGate.OutcomeEnforce != OrgOutcomeFailUnlabeled {
				t.Errorf("grant %d outcomes = %q/%q, want pass_unlabeled/fail_unlabeled",
					g.Index, g.OrgGate.OutcomeShadow, g.OrgGate.OutcomeEnforce)
			}
			if g.OrgGate.Unsatisfiable {
				t.Errorf("grant %d: unlabeled tenant must not be marked unsatisfiable (that is the labeled-empty-intersection state)", g.Index)
			}
			if g.OrgGate.PassingOrgValues != nil {
				t.Errorf("grant %d: passing_org_values = %v on an unlabeled tenant, want absent", g.Index, g.OrgGate.PassingOrgValues)
			}
		}
		if seen == 0 {
			t.Fatal("fixture must include org-scoped grants")
		}
	})

	t.Run("caller missing the org claim is denied in both modes on a labeled tenant", func(t *testing.T) {
		t.Parallel()
		cfg := dogfoodConfig()
		orgs := []string{"ORG-1", "ORG-9"}
		m := NewForTest(cfg)
		rep := m.ReverseAccessReport("db-team-1", orgs, true, "h",
			ReverseReportOptions{IncludeOrgValues: true})
		g := rep.Grants[2] // org-ops: match block + org scope on a separate key
		if g.Rule != "org-ops" || g.OrgGate.OutcomeShadow != OrgOutcomeConditional {
			t.Fatalf("fixture drift: grant2 = %s/%s", g.Rule, g.OrgGate.OutcomeShadow)
		}
		shadowM, enforceM := singleRuleManagers(cfg.Groups[g.Index])
		w := witnessFor(g)
		delete(w.Claims, g.OrgGate.ClaimKey) // strip the org claim
		if shadowM.AllowedInOrg(w, "db-team-1", PermWrite, orgs) ||
			enforceM.AllowedInOrg(w, "db-team-1", PermWrite, orgs) {
			t.Error("conditional gate must deny a caller with NO org claim in BOTH modes (labeled tenant = no basis to match)")
		}
		// Control: the intact witness passes (proves the denial above is the
		// missing claim, not a broken fixture).
		if !enforceM.AllowedInOrg(witnessFor(g), "db-team-1", PermWrite, orgs) {
			t.Error("control: intact witness must pass under enforce")
		}
	})

	t.Run("empty match block (injected) is statically dead — never rendered as a grant", func(t *testing.T) {
		t.Parallel()
		// NewForTest injects around the loader; validateConfig would reject both
		// dead shapes below. The report must NOT render them as grants: an empty
		// WHO reads as "no conditions = everyone" — the fail-open inversion of
		// ruleMatches' actual empty-match-never-matches defense.
		cfg := &RBACConfig{Groups: []GroupRule{
			{Name: "dead-empty-match", Match: &MatchBlock{}, Tenants: []string{"db-*"}, Permissions: []Permission{PermRead}},
			{Name: "dead-empty-values", Match: &MatchBlock{Claims: map[string][]string{"org": {}}}, Tenants: []string{"db-*"}, Permissions: []Permission{PermRead}},
			{Name: "live", Tenants: []string{"db-*"}, Permissions: []Permission{PermRead}},
		}}
		m := NewForTest(cfg)
		rep := m.ReverseAccessReport("db-team-1", nil, true, "h", ReverseReportOptions{})
		if len(rep.Grants) != 1 || rep.Grants[0].Index != 2 || rep.Grants[0].Rule != "live" {
			t.Fatalf("grants = %+v, want exactly the live rule at CONFIG index 2 (dead rules skipped, index preserved)", rep.Grants)
		}
		// Forward agrees the dead rules match nobody (defense-in-depth branch).
		if m.Allowed(&VerifiedPrincipal{Groups: []string{"dead-empty-match"}}, "db-team-1", PermRead) {
			// (would pass via "live" only for group "live" — this principal has no live group)
			t.Error("forward granted via a dead rule?!")
		}
		// And the YAML load path rejects the shape outright (the injection above
		// is the ONLY way it can exist).
		if _, err := parseConfig([]byte("groups:\n  - name: x\n    match: {}\n    tenants: [\"*\"]\n    permissions: [read]\n"), nil); err == nil {
			t.Error("parseConfig accepted an empty match block")
		}
	})

	t.Run("match null: struct-level nil is legacy; YAML-level null fails load", func(t *testing.T) {
		t.Parallel()
		// Struct level: a nil *MatchBlock IS the legacy shape (indistinguishable
		// by design) — the report must render legacy_group, not match_block.
		m := NewForTest(&RBACConfig{Groups: []GroupRule{
			{Name: "legacy-team", Match: nil, Tenants: []string{"db-*"}, Permissions: []Permission{PermRead}},
		}})
		rep := m.ReverseAccessReport("db-team-1", nil, true, "h", ReverseReportOptions{})
		if len(rep.Grants) != 1 || rep.Grants[0].Who.Kind != WhoKindLegacyGroup ||
			rep.Grants[0].Who.LegacyGroup != "legacy-team" {
			t.Fatalf("who = %+v, want legacy_group/legacy-team", rep.Grants[0].Who)
		}
		// YAML level: a present-but-null match: never reaches the report — the
		// loader rejects it (detectNullMatchBlocks), so the nil-means-legacy
		// rendering above cannot misrepresent a mid-authored claim scoping.
		if _, err := parseConfig([]byte("groups:\n  - name: x\n    match:\n    tenants: [\"*\"]\n    permissions: [read]\n"), nil); err == nil {
			t.Error("parseConfig accepted a present-but-null match block")
		}
	})

	t.Run("malformed tenant pattern (injected) fails closed — #1084", func(t *testing.T) {
		t.Parallel()
		cfg := &RBACConfig{Groups: []GroupRule{
			{Name: "mal-wild", Tenants: []string{"**"}, Permissions: []Permission{PermAdmin}},
			{Name: "mal-mid", Tenants: []string{"*a*"}, Permissions: []Permission{PermRead}},
			{Name: "mixed", Tenants: []string{"**", "db-*"}, Permissions: []Permission{PermRead}},
		}}
		m := NewForTest(cfg)
		rep := m.ReverseAccessReport("db-team-1", nil, true, "h", ReverseReportOptions{})
		if len(rep.Grants) != 1 || rep.Grants[0].Rule != "mixed" {
			t.Fatalf("grants = %+v, want only the mixed rule (malformed patterns match nothing)", rep.Grants)
		}
		// The malformed member of a mixed list is skipped; the VALID pattern is
		// the one named as the hit.
		if rep.Grants[0].TenantPattern != "db-*" {
			t.Errorf("tenant_pattern = %q, want the valid db-* member, never the malformed **", rep.Grants[0].TenantPattern)
		}
		// The "**"-wildcard admin shape must not satisfy the endpoint bar either
		// (tenantMatches' fail-closed backstop, not a prefix collapse to "*").
		if m.PlatformAdminNonOrgScoped(&VerifiedPrincipal{Groups: []string{"mal-wild"}}) {
			t.Error("bar satisfied via a malformed ** pattern")
		}
	})

	t.Run("platform wildcard rule renders platform_wide", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(dogfoodConfig())
		rep := m.ReverseAccessReport("db-team-1", nil, true, "h", ReverseReportOptions{})
		g := rep.Grants[0]
		if !g.PlatformWide || g.TenantPattern != "*" {
			t.Errorf("grant0 = platform_wide %v pattern %q, want true/*", g.PlatformWide, g.TenantPattern)
		}
		for _, g := range rep.Grants[1:] {
			if g.PlatformWide {
				t.Errorf("grant %d (%s): platform_wide on a non-* pattern %q", g.Index, g.Rule, g.TenantPattern)
			}
		}
	})

	t.Run("mixed tenant list: platform_wide is rule-level, tenant_pattern stays the first hit", func(t *testing.T) {
		t.Parallel()
		// A rule listing BOTH a literal id and "*": the recorded pattern is the
		// first hit (the literal), but platform_wide is decided per RULE with
		// the same tenantMatches(rule.Tenants,"*") borrow the endpoint bar runs
		// — a "*" anywhere in the list makes the grant platform-wide.
		m := NewForTest(&RBACConfig{Groups: []GroupRule{
			{Name: "mixed-wide", Tenants: []string{"db-team-1", "*"}, Permissions: []Permission{PermRead}},
		}})
		rep := m.ReverseAccessReport("db-team-1", nil, true, "h", ReverseReportOptions{})
		if len(rep.Grants) != 1 {
			t.Fatalf("grants = %+v, want exactly 1", rep.Grants)
		}
		g := rep.Grants[0]
		if g.TenantPattern != "db-team-1" {
			t.Errorf("tenant_pattern = %q, want the first hit db-team-1 (verbatim recording unchanged)", g.TenantPattern)
		}
		if !g.PlatformWide {
			t.Error("platform_wide = false, want true (the rule also carries a literal \"*\")")
		}
		// Redacted: the rule-level wildcard wins the pattern-kind downgrade —
		// kind must not contradict the platform_wide boolean next to it.
		red := RedactReverseReport(rep)
		if k := red.Grants[0].PatternKind; k != PatternKindWildcard {
			t.Errorf("redacted pattern_kind = %q, want wildcard (rule-level wildcard beats the literal first hit)", k)
		}
	})

	t.Run("redacted view strips environments/domains verbatim to counts", func(t *testing.T) {
		t.Parallel()
		// Owner ruling: env/domain strings are free-form text that can carry
		// customer-recognizable markers, and the redacted audience is wider —
		// the redacted view keeps only environments_count/domains_count. The
		// smoke/golden fixtures carry empty env lists, so this NON-EMPTY case
		// is the pin that verbatim strings never survive redaction.
		m := NewForTest(&RBACConfig{Groups: []GroupRule{
			{Name: "env-bound", Tenants: []string{"db-*"}, Permissions: []Permission{PermRead},
				Environments: []string{"production", "staging"}, Domains: []string{"payments"}},
		}})
		full := m.ReverseAccessReport("db-team-1", nil, true, "h", ReverseReportOptions{})
		if want := []string{"production", "staging"}; !reflect.DeepEqual(full.Grants[0].Environments, want) {
			t.Fatalf("full view environments = %v, want verbatim %v", full.Grants[0].Environments, want)
		}
		if full.Grants[0].EnvironmentsCount != nil || full.Grants[0].DomainsCount != nil {
			t.Errorf("full view must not carry the redacted-only count fields")
		}
		red := RedactReverseReport(full)
		g := red.Grants[0]
		if len(g.Environments) != 0 || len(g.Domains) != 0 {
			t.Errorf("redacted env/domains = %v/%v, want emptied", g.Environments, g.Domains)
		}
		if g.EnvironmentsCount == nil || *g.EnvironmentsCount != 2 {
			t.Errorf("environments_count = %v, want 2", g.EnvironmentsCount)
		}
		if g.DomainsCount == nil || *g.DomainsCount != 1 {
			t.Errorf("domains_count = %v, want 1", g.DomainsCount)
		}
		body, err := json.Marshal(red)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		for _, tok := range []string{"production", "staging", "payments"} {
			if strings.Contains(string(body), tok) {
				t.Errorf("redacted JSON leaks env/domain verbatim %q", tok)
			}
		}
		// Idempotent: counts survive re-redaction of the emptied lists.
		if again := RedactReverseReport(red); !reflect.DeepEqual(again, red) {
			t.Errorf("re-redaction drifted: %+v vs %+v", again.Grants[0], red.Grants[0])
		}
	})

	t.Run("cross-rule union is never merged into a fabricated grant", func(t *testing.T) {
		t.Parallel()
		// The TestAllowedInOrg_CrossRuleUnionNoLeak fixture: rule A grants write
		// but is org-gated; rule B passes org trivially but only grants read. A
		// cross-rule merge would fabricate an org-free write grant.
		cfg := &RBACConfig{Groups: []GroupRule{
			{Name: "org-writers", Tenants: []string{"*"}, Permissions: []Permission{PermWrite}, OrgScope: "org"},
			{Name: "viewers", Tenants: []string{"*"}, Permissions: []Permission{PermRead}},
		}}
		m := NewForTest(cfg)
		tenantOrgs := []string{"ORG-OTHER"}
		rep := m.ReverseAccessReport("db-x", tenantOrgs, true, "h",
			ReverseReportOptions{IncludeOrgValues: true})
		if len(rep.Grants) != 2 {
			t.Fatalf("len(grants) = %d, want 2 (one per rule, never merged)", len(rep.Grants))
		}
		for _, g := range rep.Grants {
			if g.Effective.Write && !g.OrgGate.Required {
				t.Errorf("grant %d (%s): fabricated org-free write — the cross-rule union leak in reverse form", g.Index, g.Rule)
			}
		}
		// Forward cross-check: the caller holding BOTH WHOs but outside the org
		// gets read (rule B) and never write — in either mode.
		p := &VerifiedPrincipal{Groups: []string{"org-writers", "viewers"}, Claims: map[string]string{"org": "ORG-USER"}}
		for _, enforce := range []bool{false, true} {
			mm := NewForTest(cfg)
			if enforce {
				mm.EnableOrgScopeEnforce()
			}
			if mm.AllowedInOrg(p, "db-x", PermWrite, tenantOrgs) {
				t.Errorf("enforce=%v: forward write granted across rules — premise broken", enforce)
			}
			if !mm.AllowedInOrg(p, "db-x", PermRead, tenantOrgs) {
				t.Errorf("enforce=%v: forward read via rule B must hold", enforce)
			}
		}
	})

	t.Run("org-scope × claims same key: intersection surfaces, empty marks unsatisfiable", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(dogfoodConfig())
		rep := m.ReverseAccessReport("db-team-1", []string{"ORG-1", "ORG-9"}, true, "h",
			ReverseReportOptions{IncludeOrgValues: true})
		gNon := rep.Grants[3] // org-pinned: {ORG-1,ORG-9} ∩ {ORG-1,ORG-2} = {ORG-1}
		if gNon.OrgGate.Unsatisfiable || len(gNon.OrgGate.PassingOrgValues) != 1 || gNon.OrgGate.PassingOrgValues[0] != "ORG-1" {
			t.Errorf("org-pinned gate = %+v, want passing exactly [ORG-1]", gNon.OrgGate)
		}
		gEmpty := rep.Grants[4] // org-disjoint: ∩ {ORG-X} = ∅
		if !gEmpty.OrgGate.Unsatisfiable || len(gEmpty.OrgGate.PassingOrgValues) != 0 {
			t.Errorf("org-disjoint gate = %+v, want unsatisfiable with no passing values", gEmpty.OrgGate)
		}
	})

	t.Run("same-name rules stay separate grants keyed by config index", func(t *testing.T) {
		t.Parallel()
		m := NewForTest(dogfoodConfig())
		rep := m.ReverseAccessReport("db-team-1", nil, true, "h", ReverseReportOptions{})
		var dups []ReverseGrant
		for _, g := range rep.Grants {
			if g.Rule == "dup" {
				dups = append(dups, g)
			}
		}
		if len(dups) != 2 || dups[0].Index == dups[1].Index {
			t.Fatalf("dup grants = %+v, want two entries with distinct config indexes", dups)
		}
		// They must keep their OWN rule's shape — merging would erase the
		// difference between a literal read grant and a prefix admin grant.
		if dups[0].TenantPattern != "db-team-1" || dups[0].Effective.Admin {
			t.Errorf("dup#%d = %+v, want the literal read-only rule", dups[0].Index, dups[0])
		}
		if dups[1].TenantPattern != "db-*" || !dups[1].Effective.Admin {
			t.Errorf("dup#%d = %+v, want the prefix admin rule", dups[1].Index, dups[1])
		}
	})
}

// ── bar supplements (the legacy trio lives in reverse_smoke_test.go) ────────

func TestPlatformAdminNonOrgScoped_MatchBlockShapes(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{Groups: []GroupRule{
		// Match-block platform admin (WHO shape must not matter to the bar).
		{Name: "platform-admins-label", Match: &MatchBlock{
			Groups: []string{"platform-crew"},
			Claims: map[string][]string{"dept": {"core"}},
		}, Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
		// Match-block ORG-SCOPED wildcard admin — the org-blind seam, match-block
		// edition: must fail the bar exactly like its legacy twin.
		{Name: "org-admins-label", Match: &MatchBlock{
			Groups: []string{"org-crew"},
		}, Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}, OrgScope: "org"},
	}})
	full := &VerifiedPrincipal{Groups: []string{"platform-crew"}, Claims: map[string]string{"dept": "core"}}
	if !m.PlatformAdminNonOrgScoped(full) {
		t.Error("match-block non-org-scoped platform admin must pass the bar")
	}
	missingClaim := &VerifiedPrincipal{Groups: []string{"platform-crew"}}
	if m.PlatformAdminNonOrgScoped(missingClaim) {
		t.Error("caller missing a required match claim must fail the bar (fail-closed ruleMatches)")
	}
	orgAdmin := &VerifiedPrincipal{Groups: []string{"org-crew"}, Claims: map[string]string{"org": "ORG-1"}}
	if m.PlatformAdminNonOrgScoped(orgAdmin) {
		t.Error("match-block org-scoped wildcard admin must fail the tightened bar")
	}
}

// ── §5.6 golden: try-local seed shape pins schema + ordering ────────────────

// goldenSeedReport is the full JSON a seed-shaped config (try-local
// seed/conf.d/_rbac.yaml: one legacy platform-wide read+write+admin rule, name
// swapped to a synthetic fixture id) renders for a not-onboarded tenant.
// generated_at is normalized to "" (excluded from diff semantics, spec §3).
// Any field rename, reorder, enum change or default-view leak shows up here as
// a byte diff — this literal IS the schema pin for reverseSchemaVersion 1.
const goldenSeedReport = `{
  "schema_version": 1,
  "advisory": "audit-only; authorization decisions remain with forward gates",
  "generated_at": "",
  "verdict": "grants_found",
  "mode": "rules",
  "flags": {
    "metadata_scope_enforce": {
      "value": false,
      "source": "runtime"
    },
    "org_scope_enforce": {
      "value": false,
      "source": "runtime"
    }
  },
  "config_anchor": {
    "rbac_sha256": {
      "value": "unanchored",
      "source": "runtime"
    },
    "tenant_orgs_sha256": {
      "value": "unanchored",
      "source": "runtime"
    }
  },
  "tenant": {
    "id": "db-team-1",
    "org_status": "not_onboarded"
  },
  "completeness": {
    "covers": [
      "rbac_rules",
      "org_scope_read_write_list",
      "platform_wildcard_rules"
    ],
    "not_covered": [
      {
        "surface": "dev_bypass_auth (ADR-022)",
        "status": "inactive"
      },
      {
        "surface": "metadata_scope effective evaluation (env/domain vs tenant labels)",
        "status": "by_design"
      }
    ]
  },
  "grants": [
    {
      "index": 0,
      "rule": "demo-admins",
      "tenant_pattern": "*",
      "platform_wide": true,
      "permissions": [
        "read",
        "write",
        "admin"
      ],
      "effective": {
        "read": true,
        "write": true,
        "admin": true
      },
      "who": {
        "kind": "legacy_group",
        "legacy_group": "demo-admins"
      },
      "org_gate": {
        "required": false,
        "outcome_shadow": "not_required",
        "outcome_enforce": "not_required",
        "unsatisfiable": false
      },
      "environments": [],
      "domains": [],
      "surfaces": [
        "list",
        "read_by_id",
        "write",
        "admin"
      ],
      "constraints_not_evaluated": [
        "environments",
        "domains"
      ]
    }
  ]
}`

func TestReverseReport_GoldenSeedShape(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{Groups: []GroupRule{
		{Name: "demo-admins", Tenants: []string{"*"}, Permissions: []Permission{PermRead, PermWrite, PermAdmin}},
	}})
	rep := m.ReverseAccessReport("db-team-1", nil, false, "", ReverseReportOptions{})
	rep.GeneratedAt = "" // excluded from diff semantics (spec §3)
	got, err := json.MarshalIndent(rep, "", "  ")
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if string(got) != goldenSeedReport {
		t.Errorf("seed-shape report drifted from the schema golden.\n--- got ---\n%s\n--- want ---\n%s", got, goldenSeedReport)
	}
}

// goldenRedactedReport pins the redacted projection on the smoke fixture
// (legacy platform admin / org-scoped match / unsatisfiable) — the allowlist
// skeleton, the counts that replace stripped identifiers, and the pattern-kind
// downgrade. The identifier-absence sweep below guards the same output
// mechanically, so an allowlist widening fails twice.
const goldenRedactedReport = `{
  "schema_version": 1,
  "advisory": "audit-only; authorization decisions remain with forward gates",
  "generated_at": "",
  "verdict": "grants_found",
  "mode": "rules",
  "flags": {
    "metadata_scope_enforce": {
      "value": false,
      "source": "runtime"
    },
    "org_scope_enforce": {
      "value": false,
      "source": "runtime"
    }
  },
  "config_anchor": {
    "rbac_sha256": {
      "value": "unanchored",
      "source": "runtime"
    },
    "tenant_orgs_sha256": {
      "value": "torgs-h",
      "source": "runtime"
    }
  },
  "tenant": {
    "id": "db-team-1",
    "org_status": "labeled"
  },
  "completeness": {
    "covers": [
      "rbac_rules",
      "org_scope_read_write_list",
      "platform_wildcard_rules"
    ],
    "not_covered": [
      {
        "surface": "dev_bypass_auth (ADR-022)",
        "status": "inactive"
      },
      {
        "surface": "metadata_scope effective evaluation (env/domain vs tenant labels)",
        "status": "by_design"
      }
    ]
  },
  "grants": [
    {
      "index": 0,
      "pattern_kind": "wildcard",
      "platform_wide": true,
      "permissions": [
        "admin"
      ],
      "effective": {
        "read": true,
        "write": true,
        "admin": true
      },
      "who": {
        "kind": "legacy_group",
        "groups_count": 1,
        "claims_count": 0
      },
      "org_gate": {
        "required": false,
        "outcome_shadow": "not_required",
        "outcome_enforce": "not_required",
        "unsatisfiable": false
      },
      "environments": [],
      "environments_count": 0,
      "domains": [],
      "domains_count": 0,
      "surfaces": [
        "list",
        "read_by_id",
        "write",
        "admin"
      ],
      "constraints_not_evaluated": [
        "environments",
        "domains"
      ]
    },
    {
      "index": 1,
      "pattern_kind": "prefix",
      "platform_wide": false,
      "permissions": [
        "read",
        "write"
      ],
      "effective": {
        "read": true,
        "write": true,
        "admin": false
      },
      "who": {
        "kind": "match_block",
        "groups_count": 1,
        "claims_count": 1
      },
      "org_gate": {
        "required": true,
        "outcome_shadow": "conditional_on_caller_org",
        "outcome_enforce": "conditional_on_caller_org",
        "unsatisfiable": false
      },
      "environments": [],
      "environments_count": 0,
      "domains": [],
      "domains_count": 0,
      "surfaces": [
        "list",
        "read_by_id",
        "write"
      ],
      "constraints_not_evaluated": [
        "environments",
        "domains"
      ]
    },
    {
      "index": 2,
      "pattern_kind": "prefix",
      "platform_wide": false,
      "permissions": [
        "read"
      ],
      "effective": {
        "read": true,
        "write": false,
        "admin": false
      },
      "who": {
        "kind": "match_block",
        "groups_count": 0,
        "claims_count": 1
      },
      "org_gate": {
        "required": true,
        "outcome_shadow": "conditional_on_caller_org",
        "outcome_enforce": "conditional_on_caller_org",
        "unsatisfiable": true
      },
      "environments": [],
      "environments_count": 0,
      "domains": [],
      "domains_count": 0,
      "surfaces": [
        "list",
        "read_by_id"
      ],
      "constraints_not_evaluated": [
        "environments",
        "domains"
      ]
    }
  ]
}`

func TestReverseReport_GoldenRedacted(t *testing.T) {
	t.Parallel()
	m := NewForTest(smokeReverseConfig())
	full := m.ReverseAccessReport("db-team-1", []string{"ORG-1", "ORG-9"}, true, "torgs-h",
		ReverseReportOptions{IncludeOrgValues: true})
	red := RedactReverseReport(full)
	red.GeneratedAt = ""
	got, err := json.MarshalIndent(red, "", "  ")
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if string(got) != goldenRedactedReport {
		t.Errorf("redacted report drifted from the golden.\n--- got ---\n%s\n--- want ---\n%s", got, goldenRedactedReport)
	}
	// Mechanical identifier-absence sweep over the SERIALIZED bytes (the three
	// stripped classes: group/rule names, claim keys+values, org values, plus
	// the verbatim tenant pattern). `"org"` is the exact quoted claim-key token
	// — JSON keys like "org_gate" do not contain it.
	for _, ident := range []string{
		"platform-admins", "org-ops", "org-pinned-elsewhere", // rule / legacy group names
		`"ops"`,         // groups_any_of entry
		`"org"`, "ORG-", // claim key + org values
		"db-*", // verbatim tenant pattern
	} {
		if strings.Contains(string(got), ident) {
			t.Errorf("redacted JSON leaks identifier %q", ident)
		}
	}
}
