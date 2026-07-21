package rbac

import (
	"embed"
	"sort"
	"testing"
)

// wizardFixtures embeds the RBAC Setup Wizard's canonical generator output
// (ADR-027 / LD-6 P7d). Each file is byte-for-byte what tools/portal's
// rbacGenerateYaml emits for a fixed input; the JS side clamps that equality in
// rbac-wizard-golden.drift.test.ts (Buffer-exact), and THIS side proves the
// same bytes load through the live strict parser. Together they form the
// two-ended drift tripwire: a change to the generator that isn't mirrored into
// these fixtures reddens the JS clamp, and a fixture the Go parser would reject
// reddens here — and because the fixtures live under components/tenant-api/**,
// a fixture-only regen flips the CI go_changed path filter so this leg runs.
//
// A zero-match embed glob is a COMPILE error (unlike os.ReadDir over an emptied
// dir), so the harness cannot silently go inert by losing its inputs.
//
//go:embed testdata/wizard/*.yaml
var wizardFixtures embed.FS

// wizardExpectedFixtureCount pins how many fixtures the corpus must contain, so
// adding a generator emission path without wiring a fixture (or losing one to a
// rename) reddens. Keep in sync with rbac-wizard-golden.drift.test.ts.
const wizardExpectedFixtureCount = 5

// wizardDeclaredClaimKeys is the deployment identity-axis declaration the
// fixtures are evaluated against. It MUST equal the exact set of claim / org-
// scope keys the fixtures use (asserted below) — a SUPERSET would let a fixture
// silently drop an axis and still stay green, and the tempting way to green a
// red is to widen this map rather than fix the wizard.
func wizardDeclaredClaimKeys(t *testing.T) map[string]string {
	t.Helper()
	keys, err := ParseClaimHeaders("org-code=X-Forwarded-Org-Code,region=X-Forwarded-Region")
	if err != nil {
		t.Fatalf("ParseClaimHeaders: %v", err)
	}
	return keys
}

// TestWizardOutputStillParses is the load-bearing tripwire: every committed
// wizard fixture must parse through ParseCandidateConfig exactly as a hot-reload
// would accept it. "必 parse 過" is necessary but not sufficient — a rule can
// parse clean and grant nothing (permCovers rejects an off-enum permission; a
// nil tenants slice grants zero tenants), so the corpus-level liveness
// assertions below extend the spec-decision-10 contract to "parses AND every
// emitted permission / tenant pattern is semantically live".
func TestWizardOutputStillParses(t *testing.T) {
	entries, err := wizardFixtures.ReadDir("testdata/wizard")
	if err != nil {
		t.Fatalf("read embedded fixtures: %v", err)
	}
	if len(entries) != wizardExpectedFixtureCount {
		t.Fatalf("fixture count = %d, want %d (add the fixture to the corpus, or update wizardExpectedFixtureCount if a path was intentionally removed)", len(entries), wizardExpectedFixtureCount)
	}

	declared := wizardDeclaredClaimKeys(t)

	// Corpus-level liveness witnesses — each must be seen in at least one rule
	// across all fixtures, or a whole emission path is uncovered.
	var (
		sawOrgScope    bool // an org-scope rule exists
		sawMatchClaims bool // a claims-gated rule exists
		sawLegacy      bool // a bare (match==nil) rule exists
		sawMultiRule   bool // a fixture carries >=2 rules (inter-rule boundary)
	)
	usedKeys := map[string]struct{}{}

	for _, e := range entries {
		name := e.Name()
		data, err := wizardFixtures.ReadFile("testdata/wizard/" + name)
		if err != nil {
			t.Fatalf("%s: read: %v", name, err)
		}
		cfg, err := ParseCandidateConfig(data, declared)
		if err != nil {
			t.Fatalf("%s: ParseCandidateConfig rejected wizard output (drift): %v", name, err)
		}
		if len(cfg.Groups) >= 2 {
			sawMultiRule = true
		}
		for i := range cfg.Groups {
			rule := &cfg.Groups[i]

			// Every emitted permission must be a live enum value — an off-enum
			// permission parses fine and silently grants nothing (permCovers).
			if len(rule.Permissions) == 0 {
				t.Errorf("%s: rule %q has no permissions (grants nothing)", name, rule.Name)
			}
			for _, p := range rule.Permissions {
				if !permCovers(p, PermRead) && p != PermRead {
					t.Errorf("%s: rule %q permission %q is not one of read/write/admin (dead grant)", name, rule.Name, p)
				}
			}

			// Every rule must grant at least one tenant pattern.
			if len(rule.Tenants) == 0 {
				t.Errorf("%s: rule %q has no tenants (grants nothing)", name, rule.Name)
			}

			if rule.OrgScope != "" {
				sawOrgScope = true
				usedKeys[rule.OrgScope] = struct{}{}
			}
			if rule.Match == nil {
				sawLegacy = true
			} else if len(rule.Match.Claims) > 0 {
				sawMatchClaims = true
				for k := range rule.Match.Claims {
					usedKeys[k] = struct{}{}
				}
			}
		}
	}

	if !sawOrgScope {
		t.Error("no fixture exercises org-scope")
	}
	if !sawMatchClaims {
		t.Error("no fixture exercises match.claims")
	}
	if !sawLegacy {
		t.Error("no fixture exercises the legacy (match==nil) shape")
	}
	if !sawMultiRule {
		t.Error("no fixture carries >=2 rules (inter-rule boundary uncovered)")
	}

	// Exact equality (NOT subset): the claim/org-scope keys the corpus uses must
	// equal the declared set. This is the anti-superset guard — softening it to a
	// subset check would let the declaration rot ahead of the fixtures.
	if !sameStringSet(keysOf(usedKeys), keysOf(declared)) {
		t.Errorf("fixture-used claim keys %v != declared keys %v (do not widen the declaration to green this — fix the fixtures)", sortedKeys(usedKeys), sortedMapKeys(declared))
	}

	// NOTE: no deliberately-malformed "negative" fixture lives here. Parser
	// strictness (KnownFields rejection of an unknown key, null-match rejection)
	// is already owned by config_load_test.go and reverse_dogfood_test.go; a
	// wizard-shaped malformed file would be a parser test in a wizard costume,
	// inflating the tripwire's apparent strength without adding coverage.
}

func keysOf[V any](m map[string]V) map[string]struct{} {
	out := make(map[string]struct{}, len(m))
	for k := range m {
		out[k] = struct{}{}
	}
	return out
}

func sameStringSet(a, b map[string]struct{}) bool {
	if len(a) != len(b) {
		return false
	}
	for k := range a {
		if _, ok := b[k]; !ok {
			return false
		}
	}
	return true
}

func sortedKeys(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func sortedMapKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
