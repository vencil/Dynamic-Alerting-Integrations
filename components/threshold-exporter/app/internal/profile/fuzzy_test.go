package profile

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/parser"
)

// ─── duration canonicalisation tests ────────────────────────────────

func TestDurationToMillis(t *testing.T) {
	cases := []struct {
		in   string
		want int64
		ok   bool
	}{
		{"5m", 300_000, true},
		{"300s", 300_000, true},
		{"300000ms", 300_000, true},
		{"1h", 3_600_000, true},
		{"1h30m", 5_400_000, true},
		{"2h45m30s", 9_930_000, true},
		{"100ms", 100, true},
		{"1d", 86_400_000, true},
		{"1w", 604_800_000, true},
		{"1y", 31_536_000_000, true},
		// malformed
		{"", 0, false},
		{"5", 0, false},   // missing unit
		{"m", 0, false},   // missing digits
		{"5x", 0, false},  // unknown unit
		{"-5m", 0, false}, // negative not supported
	}
	for _, tc := range cases {
		t.Run(tc.in, func(t *testing.T) {
			got, ok := durationToMillis(tc.in)
			if ok != tc.ok {
				t.Errorf("ok = %v, want %v (got=%d)", ok, tc.ok, got)
			}
			if ok && got != tc.want {
				t.Errorf("got %d ms, want %d ms", got, tc.want)
			}
		})
	}
}

func TestBase26Encode_RoundTripStable(t *testing.T) {
	// Encoding must be deterministic and unique per input — two calls
	// with the same n produce the same string; different n produces
	// different strings (within tested range).
	seen := make(map[string]int64)
	for n := int64(0); n < 1000; n++ {
		enc := base26Encode(n)
		if enc == "" {
			t.Errorf("base26Encode(%d) returned empty", n)
		}
		if prev, ok := seen[enc]; ok {
			t.Errorf("base26Encode(%d) collides with base26Encode(%d) = %q", n, prev, enc)
		}
		seen[enc] = n
		// All-letter contract: encoding must contain only [a-z] +
		// optionally a leading 'n' for negatives. The strict pass
		// (numericLiteral) ignores letters so the placeholder
		// survives. If digits sneak in here, that contract breaks.
		for _, c := range enc {
			if !(c >= 'a' && c <= 'z') {
				t.Errorf("base26Encode(%d) = %q contains non-letter %q", n, enc, c)
			}
		}
	}
}

func TestBase26Encode_NegativeInputDefensiveOnly(t *testing.T) {
	// We never expect negatives in production (parser rejects them)
	// but the encoder shouldn't panic. The output is documented to
	// prefix with 'n'; we just pin the contract.
	got := base26Encode(-5)
	if !strings.HasPrefix(got, "n") {
		t.Errorf("negative encoding %q missing 'n' prefix", got)
	}
}

func TestCanonicaliseDurations_BasicEquivalence(t *testing.T) {
	cases := []struct {
		name    string
		a, b    string
		shouldEq bool
	}{
		{"5m equals 300s", "rate(foo[5m])", "rate(foo[300s])", true},
		{"300000ms equals 5m", "rate(foo[300000ms])", "rate(foo[5m])", true},
		{"1h equals 60m", "rate(foo[1h])", "rate(foo[60m])", true},
		{"1h30m equals 90m", "rate(foo[1h30m])", "rate(foo[90m])", true},
		{"different durations stay different", "rate(foo[5m])", "rate(foo[10m])", false},
		{"different metric stays different", "rate(foo[5m])", "rate(bar[5m])", false},
		{"unparseable left alone", "rate(foo[5x])", "rate(foo[10x])", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a := canonicaliseDurations(tc.a)
			b := canonicaliseDurations(tc.b)
			if (a == b) != tc.shouldEq {
				t.Errorf("canonicaliseDurations equivalence mismatch:\n  a in : %q\n  a out: %q\n  b in : %q\n  b out: %q\n  shouldEq: %v",
					tc.a, a, tc.b, b, tc.shouldEq)
			}
		})
	}
}

func TestCanonicaliseDurations_LeavesNonRangeDurationsAlone(t *testing.T) {
	// `offset 1h` is a duration outside `[]` — PR-5 deliberately
	// doesn't canonicalise it (rare in customer corpora, more grammar
	// edge cases). The expression should pass through unchanged.
	in := `rate(foo[5m] offset 1h)`
	out := canonicaliseDurations(in)
	if !strings.Contains(out, "offset 1h") {
		t.Errorf("offset duration should pass through verbatim; got %q", out)
	}
	if !strings.Contains(out, "[<DUR_") {
		t.Errorf("range duration should be canonicalised; got %q", out)
	}
}

func TestCanonicaliseDurations_PreservesIdentifiersWithNumbers(t *testing.T) {
	// Identifiers like `http_requests_5xx` contain digits + letter
	// combinations that look superficially like durations. The pattern
	// requires `[...]` brackets, which protects identifiers. Pin this
	// behaviour so a future regex tweak doesn't accidentally rewrite
	// identifier names.
	in := `rate(http_requests_total_5xx[5m])`
	out := canonicaliseDurations(in)
	if !strings.Contains(out, "http_requests_total_5xx") {
		t.Errorf("identifier mangled; got %q", out)
	}
}

func TestNormaliseExpr_BackwardsCompatNoOptions(t *testing.T) {
	// PR-1 callers pass no options. Default behaviour must NOT do
	// duration canonicalisation — `[5m]` vs `[300s]` stay separate.
	a := normaliseExpr(`rate(foo[5m])`)
	b := normaliseExpr(`rate(foo[300s])`)
	if a == b {
		t.Errorf("strict normaliseExpr should keep [5m] / [300s] distinct;\n  got a = %q\n  got b = %q", a, b)
	}
}

func TestNormaliseExpr_WithCanonicalDurationsCollapses(t *testing.T) {
	// Opt-in via WithCanonicalDurations() — same inputs that were
	// distinct under strict now collapse to the same signature.
	a := normaliseExpr(`rate(foo[5m])`, WithCanonicalDurations())
	b := normaliseExpr(`rate(foo[300s])`, WithCanonicalDurations())
	if a != b {
		t.Errorf("fuzzy normaliseExpr should collapse [5m] / [300s];\n  got a = %q\n  got b = %q", a, b)
	}
}

func TestNormaliseExpr_FuzzyStillStripsNumbersAndStrings(t *testing.T) {
	// The fuzzy pass adds duration canonicalisation BEFORE the
	// numeric/string strip — both must still run afterwards. Catch
	// regressions where the fuzzy path accidentally short-circuits.
	in := `rate(foo{tenant="db-a"}[5m]) > 0.9`
	out := normaliseExpr(in, WithCanonicalDurations())
	// Number stripped:
	if strings.Contains(out, "0.9") {
		t.Errorf("numeric literal not stripped; got %q", out)
	}
	// Quoted string stripped:
	if strings.Contains(out, `"db-a"`) {
		t.Errorf("quoted string not stripped; got %q", out)
	}
	// Duration canonicalised:
	if !strings.Contains(out, "<DUR_") {
		t.Errorf("duration not canonicalised; got %q", out)
	}
}

// ─── cluster fuzzy-pass tests ───────────────────────────────────────

// makeRule is a small constructor mirroring cluster_test.go style.
func makeRule(id, expr, forVal, dialect string, labels map[string]string) parser.ParsedRule {
	return parser.ParsedRule{
		Alert:        "TestAlert",
		Expr:         expr,
		For:          forVal,
		Labels:       labels,
		SourceRuleID: id,
		Dialect:      parser.Dialect(dialect),
	}
}

func TestBuildProposals_FuzzyOff_PreservesPR1Behavior(t *testing.T) {
	// Two rules: one uses [5m], one uses [300s]. Strict signature
	// keeps them apart. With EnableFuzzy=false (default), they stay
	// in Unclustered.
	rules := []parser.ParsedRule{
		makeRule("file#g[0].r[0]", `rate(foo[5m]) > 0.9`, "5m", "prom", map[string]string{"severity": "warn"}),
		makeRule("file#g[0].r[1]", `rate(foo[300s]) > 0.9`, "5m", "prom", map[string]string{"severity": "warn"}),
	}
	out, err := BuildProposals(rules, ClusterOptions{})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	if len(out.Proposals) != 0 {
		t.Errorf("strict mode should NOT cluster duration variants; got %d proposals", len(out.Proposals))
	}
	if len(out.Unclustered) != 2 {
		t.Errorf("both rules should be Unclustered; got %d", len(out.Unclustered))
	}
}

func TestBuildProposals_FuzzyOn_CollapsesDurationVariants(t *testing.T) {
	rules := []parser.ParsedRule{
		makeRule("file#g[0].r[0]", `rate(foo[5m]) > 0.9`, "5m", "prom", map[string]string{"severity": "warn"}),
		makeRule("file#g[0].r[1]", `rate(foo[300s]) > 0.9`, "5m", "prom", map[string]string{"severity": "warn"}),
	}
	out, err := BuildProposals(rules, ClusterOptions{EnableFuzzy: true})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	if len(out.Proposals) != 1 {
		t.Fatalf("fuzzy mode should produce 1 medium proposal; got %d", len(out.Proposals))
	}
	p := out.Proposals[0]
	if p.Confidence != ConfidenceMedium {
		t.Errorf("Confidence = %q, want %q", p.Confidence, ConfidenceMedium)
	}
	if len(p.MemberRuleIDs) != 2 {
		t.Errorf("MemberRuleIDs = %d, want 2", len(p.MemberRuleIDs))
	}
	if !strings.Contains(p.Reason, "duration-equivalence") {
		t.Errorf("Reason = %q, want a 'duration-equivalence' explanation", p.Reason)
	}
	if len(out.Unclustered) != 0 {
		t.Errorf("Unclustered should be empty after fuzzy merge; got %d", len(out.Unclustered))
	}
}

func TestBuildProposals_StrictWinsOverFuzzy(t *testing.T) {
	// 4 rules all use [5m] (strict cluster of 4 → high), plus 1 rule
	// uses [300s] (would fuzzy-merge with the 4). The strict cluster
	// must remain pure ConfidenceHigh; the lone [300s] rule goes to
	// Unclustered (because it forms a fuzzy-residue bucket of size 1
	// — below MinClusterSize).
	rules := []parser.ParsedRule{
		makeRule("a", `rate(foo[5m]) > 0.9`, "5m", "prom", map[string]string{"severity": "warn"}),
		makeRule("b", `rate(foo[5m]) > 0.9`, "5m", "prom", map[string]string{"severity": "warn"}),
		makeRule("c", `rate(foo[5m]) > 0.9`, "5m", "prom", map[string]string{"severity": "warn"}),
		makeRule("d", `rate(foo[5m]) > 0.9`, "5m", "prom", map[string]string{"severity": "warn"}),
		makeRule("e", `rate(foo[300s]) > 0.9`, "5m", "prom", map[string]string{"severity": "warn"}),
	}
	out, err := BuildProposals(rules, ClusterOptions{EnableFuzzy: true})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	if len(out.Proposals) != 1 {
		t.Fatalf("expected 1 high-confidence proposal; got %d", len(out.Proposals))
	}
	p := out.Proposals[0]
	if p.Confidence != ConfidenceHigh {
		t.Errorf("Confidence = %q, want %q (strict cluster must win)", p.Confidence, ConfidenceHigh)
	}
	if len(p.MemberRuleIDs) != 4 {
		t.Errorf("strict cluster size = %d, want 4 (the 4 [5m] rules)", len(p.MemberRuleIDs))
	}
	for _, id := range p.MemberRuleIDs {
		if id == "e" {
			t.Errorf("strict cluster should NOT include 300s rule 'e'; got members %v", p.MemberRuleIDs)
		}
	}
	if len(out.Unclustered) != 1 || out.Unclustered[0] != "e" {
		t.Errorf("Unclustered should be [e]; got %v", out.Unclustered)
	}
}

func TestBuildProposals_FuzzyDoesNotCrossDialects(t *testing.T) {
	// Same expr template (after canonicalisation), same `for:`, but
	// different dialects. Fuzzy MUST NOT merge — vendor-lock-in risk.
	// 2 prom + 2 metricsql, all using [5m] / [300s] cross-references.
	rules := []parser.ParsedRule{
		makeRule("p1", `rate(foo[5m]) > 0.9`, "5m", "prom", nil),
		makeRule("p2", `rate(foo[300s]) > 0.9`, "5m", "prom", nil),
		makeRule("m1", `rate(foo[5m]) > 0.9`, "5m", "metricsql", nil),
		makeRule("m2", `rate(foo[300s]) > 0.9`, "5m", "metricsql", nil),
	}
	out, err := BuildProposals(rules, ClusterOptions{EnableFuzzy: true})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	if len(out.Proposals) != 2 {
		t.Fatalf("expected 2 proposals (one per dialect); got %d", len(out.Proposals))
	}
	dialects := make(map[string]bool)
	for _, p := range out.Proposals {
		dialects[p.Dialect] = true
		if p.Confidence != ConfidenceMedium {
			t.Errorf("proposal %q Confidence = %q, want medium", p.MemberRuleIDs, p.Confidence)
		}
	}
	if !dialects["prom"] || !dialects["metricsql"] {
		t.Errorf("each dialect must have its own proposal; dialects seen = %v", dialects)
	}
}

func TestBuildProposals_FuzzyDoesNotMergeAcrossForVariance(t *testing.T) {
	// Same expr (after canonicalisation), same dialect, but different
	// `for:`. PR-5 fuzzy keeps `for:` in the signature — these are
	// usually intentional alert-tier separations and we don't merge
	// them. (Dedicated `for:`-variance support is documented as a
	// future opt-in.)
	rules := []parser.ParsedRule{
		makeRule("a1", `rate(foo[5m]) > 0.9`, "5m", "prom", nil),
		makeRule("a2", `rate(foo[300s]) > 0.9`, "5m", "prom", nil),
		makeRule("b1", `rate(foo[5m]) > 0.9`, "10m", "prom", nil),
		makeRule("b2", `rate(foo[300s]) > 0.9`, "10m", "prom", nil),
	}
	out, err := BuildProposals(rules, ClusterOptions{EnableFuzzy: true})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	if len(out.Proposals) != 2 {
		t.Fatalf("expected 2 proposals (one per for: tier); got %d", len(out.Proposals))
	}
	forTiers := make(map[string]int)
	for _, p := range out.Proposals {
		forTiers[p.SharedFor]++
		if p.Confidence != ConfidenceMedium {
			t.Errorf("proposal %v Confidence = %q, want medium", p.MemberRuleIDs, p.Confidence)
		}
	}
	if forTiers["5m"] != 1 || forTiers["10m"] != 1 {
		t.Errorf("expected one proposal per for: tier; got %v", forTiers)
	}
}

func TestBuildProposals_FuzzyOutputDeterministic(t *testing.T) {
	// Two BuildProposals runs over the same input + same opts produce
	// byte-identical JSON. Pins the determinism contract for the
	// fuzzy path (PR-1 already pins it for strict).
	rules := []parser.ParsedRule{
		makeRule("z", `rate(foo[300s]) > 0.9`, "5m", "prom", map[string]string{"severity": "warn"}),
		makeRule("a", `rate(foo[5m]) > 0.9`, "5m", "prom", map[string]string{"severity": "warn"}),
		makeRule("m", `rate(foo[5m]) > 0.9`, "5m", "prom", map[string]string{"severity": "warn"}),
	}
	out1, _ := BuildProposals(rules, ClusterOptions{EnableFuzzy: true})
	out2, _ := BuildProposals(rules, ClusterOptions{EnableFuzzy: true})
	b1, _ := json.Marshal(out1)
	b2, _ := json.Marshal(out2)
	if string(b1) != string(b2) {
		t.Errorf("non-deterministic fuzzy output:\n  run1: %s\n  run2: %s", b1, b2)
	}
}

func TestBuildProposals_FuzzyMixedHighAndMedium(t *testing.T) {
	// 3 [5m] rules form a strict (high) cluster.
	// 2 different rules — one [10m], one [600s] — form a fuzzy
	// (medium) cluster.
	// Both proposals should appear in output, sorted by first
	// MemberRuleIDs[0] (NOT by confidence tier).
	rules := []parser.ParsedRule{
		// strict cluster (will sort ahead — IDs start with 'a')
		makeRule("a1", `rate(foo[5m]) > 0.9`, "5m", "prom", nil),
		makeRule("a2", `rate(foo[5m]) > 0.9`, "5m", "prom", nil),
		makeRule("a3", `rate(foo[5m]) > 0.9`, "5m", "prom", nil),
		// fuzzy cluster (IDs start with 'b')
		makeRule("b1", `rate(bar[10m]) > 0.5`, "10m", "prom", nil),
		makeRule("b2", `rate(bar[600s]) > 0.5`, "10m", "prom", nil),
	}
	out, err := BuildProposals(rules, ClusterOptions{EnableFuzzy: true})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	if len(out.Proposals) != 2 {
		t.Fatalf("expected 2 proposals; got %d", len(out.Proposals))
	}
	// First proposal should be the strict (a*) cluster — ordered by
	// MemberRuleIDs[0].
	if out.Proposals[0].MemberRuleIDs[0] != "a1" {
		t.Errorf("first proposal should start with a1; got %v", out.Proposals[0].MemberRuleIDs)
	}
	if out.Proposals[0].Confidence != ConfidenceHigh {
		t.Errorf("first proposal Confidence = %q, want high", out.Proposals[0].Confidence)
	}
	if out.Proposals[1].Confidence != ConfidenceMedium {
		t.Errorf("second proposal Confidence = %q, want medium", out.Proposals[1].Confidence)
	}
}

func TestBuildProposals_FuzzyEmptyInputStillErrors(t *testing.T) {
	_, err := BuildProposals(nil, ClusterOptions{EnableFuzzy: true})
	if err == nil {
		t.Error("expected error on empty input regardless of fuzzy mode")
	}
}

func TestBuildProposals_FuzzySingleResidueGoesToUnclustered(t *testing.T) {
	// One rule alone in fuzzy bucket → still Unclustered (size <
	// MinClusterSize).
	rules := []parser.ParsedRule{
		makeRule("a", `rate(foo[5m]) > 0.9`, "5m", "prom", nil),
	}
	out, err := BuildProposals(rules, ClusterOptions{EnableFuzzy: true})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	if len(out.Proposals) != 0 {
		t.Errorf("singleton residue should not form a fuzzy proposal; got %d", len(out.Proposals))
	}
	if len(out.Unclustered) != 1 || out.Unclustered[0] != "a" {
		t.Errorf("Unclustered should be [a]; got %v", out.Unclustered)
	}
}

func TestSignatureForFuzzy_DialectAndForStillSeparate(t *testing.T) {
	// Pin the contract: signatureForFuzzy keeps for+dialect in the
	// key, only loosens duration. A regression that drops `for:`
	// from the fuzzy key would silently merge alert tiers.
	r1 := makeRule("a", `rate(foo[5m]) > 0.9`, "5m", "prom", nil)
	r2 := makeRule("b", `rate(foo[5m]) > 0.9`, "10m", "prom", nil) // different for
	r3 := makeRule("c", `rate(foo[5m]) > 0.9`, "5m", "metricsql", nil) // different dialect
	if signatureForFuzzy(r1) == signatureForFuzzy(r2) {
		t.Error("fuzzy signature should distinguish different `for:` durations")
	}
	if signatureForFuzzy(r1) == signatureForFuzzy(r3) {
		t.Error("fuzzy signature should distinguish different dialects")
	}
}

func TestBuildProposals_FuzzyOnWithEmptyResidueNoOp(t *testing.T) {
	// Pin the contract: when EnableFuzzy=true but every input rule
	// already strict-clusters, fuzzyPassClusters runs over an empty
	// residue and produces no proposals + no leftover. The output
	// must be byte-identical to what EnableFuzzy=false would have
	// produced.
	//
	// Without this test, a regression that always emitted at least
	// one fuzzy proposal (even for empty residue) would slip through.
	rules := []parser.ParsedRule{
		makeRule("a", `rate(foo[5m]) > 0.9`, "5m", "prom", nil),
		makeRule("b", `rate(foo[5m]) > 0.9`, "5m", "prom", nil),
		makeRule("c", `rate(foo[5m]) > 0.9`, "5m", "prom", nil),
	}
	withFuzzy, err := BuildProposals(rules, ClusterOptions{EnableFuzzy: true})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	withoutFuzzy, err := BuildProposals(rules, ClusterOptions{EnableFuzzy: false})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}

	bWith, _ := json.Marshal(withFuzzy)
	bWithout, _ := json.Marshal(withoutFuzzy)
	if string(bWith) != string(bWithout) {
		t.Errorf("fuzzy on/off should produce identical output when residue is empty\n  with    fuzzy: %s\n  without fuzzy: %s", bWith, bWithout)
	}

	// Sanity: verify there's actually a strict cluster of 3 (else this
	// test isn't exercising the empty-residue path).
	if len(withFuzzy.Proposals) != 1 || len(withFuzzy.Proposals[0].MemberRuleIDs) != 3 {
		t.Fatalf("expected 1 strict cluster of 3; got %+v", withFuzzy.Proposals)
	}
	if withFuzzy.Proposals[0].Confidence != ConfidenceHigh {
		t.Errorf("expected ConfidenceHigh; got %q", withFuzzy.Proposals[0].Confidence)
	}
}

func TestFuzzyReason_DistinctRawDurationsCount(t *testing.T) {
	// fuzzyReason mentions the count when 2+ distinct raw durations
	// collapse. Reviewers use this to gauge merge breadth.
	members := []parser.ParsedRule{
		{Expr: `rate(foo[5m])`, Dialect: "prom"},
		{Expr: `rate(foo[300s])`, Dialect: "prom"},
		{Expr: `rate(foo[5m])`, Dialect: "prom"},
	}
	r := fuzzyReason(members)
	if !strings.Contains(r, "2 distinct raw range durations") {
		t.Errorf("reason should call out 2 distinct durations; got %q", r)
	}
}
