package profile

import (
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/parser"
)

// makeProposal builds a minimal ExtractionProposal for decision tests.
// Real proposals come from BuildProposals; here we just need stable
// Key()-relevant fields.
func makeProposal(exprTpl string, ids ...string) ExtractionProposal {
	return ExtractionProposal{
		MemberRuleIDs:      ids,
		SharedExprTemplate: exprTpl,
		Confidence:         ConfidenceHigh,
		Dialect:            "prom",
	}
}

func TestProposalKey_StableAndDistinct(t *testing.T) {
	p1 := makeProposal("avg(rate(x[5m])) > T", "rule-a", "rule-b", "rule-c")
	p2 := makeProposal("avg(rate(x[5m])) > T", "rule-a", "rule-b", "rule-c")
	if p1.Key() != p2.Key() {
		t.Fatalf("identical proposals must produce identical Key; got %s vs %s", p1.Key(), p2.Key())
	}
	if len(p1.Key()) != 64 {
		t.Errorf("Key should be 64-char hex SHA-256; got len=%d (%q)", len(p1.Key()), p1.Key())
	}

	// Adding a member must shift the key.
	p3 := makeProposal("avg(rate(x[5m])) > T", "rule-a", "rule-b", "rule-c", "rule-d")
	if p1.Key() == p3.Key() {
		t.Errorf("proposals with different members must produce different Keys")
	}

	// Changing the expression must shift the key.
	p4 := makeProposal("avg(rate(y[5m])) > T", "rule-a", "rule-b", "rule-c")
	if p1.Key() == p4.Key() {
		t.Errorf("proposals with different SharedExprTemplate must produce different Keys")
	}
}

func TestScaffoldDecisions_NilSafeAndPopulated(t *testing.T) {
	// Nil ProposalSet → empty scaffold, default policy, no panic.
	d := ScaffoldDecisions(nil)
	if d == nil {
		t.Fatal("ScaffoldDecisions(nil) should return non-nil ProposalDecisions")
	}
	if d.UndecidedPolicy != UndecidedEmit {
		t.Errorf("default UndecidedPolicy should be 'emit'; got %q", d.UndecidedPolicy)
	}
	if len(d.Proposals) != 0 {
		t.Errorf("nil ProposalSet should yield no decision rows; got %d", len(d.Proposals))
	}

	// Populated ProposalSet → one row per proposal, all pending.
	set := &ProposalSet{
		Proposals: []ExtractionProposal{
			makeProposal("expr-a", "ra1", "ra2"),
			makeProposal("expr-b", "rb1", "rb2", "rb3"),
		},
	}
	d2 := ScaffoldDecisions(set)
	if got, want := len(d2.Proposals), 2; got != want {
		t.Fatalf("scaffold rows: got %d, want %d", got, want)
	}
	for i, row := range d2.Proposals {
		if row.Decision != DecisionPending {
			t.Errorf("row[%d] decision: got %q, want %q", i, row.Decision, DecisionPending)
		}
		if row.Key != set.Proposals[i].Key() {
			t.Errorf("row[%d] key mismatch: got %q, want %q", i, row.Key, set.Proposals[i].Key())
		}
	}
}

func TestScaffoldDecisions_DefensiveCopyOfMembers(t *testing.T) {
	// If ScaffoldDecisions stored a reference to MemberRuleIDs,
	// mutating the underlying slice afterwards would corrupt the
	// scaffold. This test pins the defensive copy contract.
	set := &ProposalSet{
		Proposals: []ExtractionProposal{makeProposal("expr-x", "r1", "r2")},
	}
	d := ScaffoldDecisions(set)
	original := d.Proposals[0].MemberRuleIDs[0]
	set.Proposals[0].MemberRuleIDs[0] = "MUTATED"
	if d.Proposals[0].MemberRuleIDs[0] != original {
		t.Errorf("scaffold MemberRuleIDs must be defensively copied; mutation leaked through (now %q, was %q)",
			d.Proposals[0].MemberRuleIDs[0], original)
	}
}

func TestParseDecisions_RoundTrip(t *testing.T) {
	original := &ProposalDecisions{
		UndecidedPolicy: UndecidedSkip,
		Proposals: []ProposalDecision{
			{
				Key:           "abc123",
				Decision:      DecisionAccept,
				MemberRuleIDs: []string{"r1", "r2"},
				Summary:       "2 rules; dialect=prom; expr=foo",
				Note:          "good cluster",
			},
			{
				Key:      "def456",
				Decision: DecisionReject,
				Note:     "covers legacy alerts",
			},
		},
	}
	encoded, err := EncodeDecisions(original)
	if err != nil {
		t.Fatalf("EncodeDecisions: %v", err)
	}
	if !strings.Contains(string(encoded), "C-9 Profile Builder") {
		t.Errorf("encoded output should include header comment; got %q", encoded)
	}

	parsed, err := ParseDecisions(encoded)
	if err != nil {
		t.Fatalf("ParseDecisions: %v", err)
	}
	if parsed.UndecidedPolicy != original.UndecidedPolicy {
		t.Errorf("policy round-trip: got %q, want %q", parsed.UndecidedPolicy, original.UndecidedPolicy)
	}
	if len(parsed.Proposals) != 2 {
		t.Fatalf("proposals count: got %d, want 2", len(parsed.Proposals))
	}
	if parsed.Proposals[0].Key != "abc123" || parsed.Proposals[0].Decision != DecisionAccept {
		t.Errorf("row[0] mismatch: %+v", parsed.Proposals[0])
	}
	if parsed.Proposals[0].Note != "good cluster" {
		t.Errorf("row[0] note round-trip lost: %q", parsed.Proposals[0].Note)
	}
}

func TestParseDecisions_EmptyAndWhitespaceTreatedAsEmpty(t *testing.T) {
	for _, in := range [][]byte{
		nil,
		[]byte(""),
		[]byte("   \n\t  "),
	} {
		d, err := ParseDecisions(in)
		if err != nil {
			t.Errorf("ParseDecisions(%q) should not error on empty input; got %v", in, err)
		}
		if d == nil {
			t.Errorf("ParseDecisions(%q) should return non-nil empty ProposalDecisions", in)
		}
	}
}

func TestParseDecisions_MalformedYAMLReturnsError(t *testing.T) {
	bad := []byte("proposals: [this is not a list of maps\nkey: value")
	_, err := ParseDecisions(bad)
	if err == nil {
		t.Fatal("ParseDecisions should error on malformed YAML")
	}
	if !strings.Contains(err.Error(), "decisions:") {
		t.Errorf("error should be wrapped with package prefix; got %v", err)
	}
}

func TestEncodeDecisions_NilErrors(t *testing.T) {
	if _, err := EncodeDecisions(nil); err == nil {
		t.Fatal("EncodeDecisions(nil) should error")
	}
}

// --- Filter behaviour ----------------------------------------------------

// makeProposalSet builds a fixed two-proposal set used across filter tests.
func makeProposalSet() (*ProposalSet, []ExtractionProposal) {
	props := []ExtractionProposal{
		makeProposal("expr-A", "rule-a1", "rule-a2"),
		makeProposal("expr-B", "rule-b1", "rule-b2"),
	}
	return &ProposalSet{Proposals: props}, props
}

func TestApplyDecisions_NilDecisions_PassThrough(t *testing.T) {
	_, props := makeProposalSet()
	idx, warns := applyDecisions(props, nil)
	if len(idx) != len(props) {
		t.Errorf("nil decisions should pass through all indices; got %d of %d", len(idx), len(props))
	}
	if len(warns) != 0 {
		t.Errorf("nil decisions should not produce warnings; got %v", warns)
	}
}

func TestApplyDecisions_AcceptOneRejectOne(t *testing.T) {
	_, props := makeProposalSet()
	d := &ProposalDecisions{
		Proposals: []ProposalDecision{
			{Key: props[0].Key(), Decision: DecisionAccept, MemberRuleIDs: props[0].MemberRuleIDs},
			{Key: props[1].Key(), Decision: DecisionReject, MemberRuleIDs: props[1].MemberRuleIDs},
		},
	}
	idx, warns := applyDecisions(props, d)
	if len(idx) != 1 || idx[0] != 0 {
		t.Errorf("only proposal[0] should emit; got idx=%v", idx)
	}
	// Pure accept/reject — no warnings expected.
	for _, w := range warns {
		if !strings.Contains(w, "no decision recorded") {
			// Accept/reject of clusters that DO have decisions and DO match members shouldn't warn.
			t.Errorf("unexpected warning on clean accept/reject path: %q", w)
		}
	}
}

func TestApplyDecisions_PendingRespectsPolicy(t *testing.T) {
	_, props := makeProposalSet()

	// UndecidedEmit: pending rows still emit, with warning.
	dEmit := &ProposalDecisions{
		UndecidedPolicy: UndecidedEmit,
		Proposals: []ProposalDecision{
			{Key: props[0].Key(), Decision: DecisionPending, MemberRuleIDs: props[0].MemberRuleIDs},
			{Key: props[1].Key(), Decision: DecisionAccept, MemberRuleIDs: props[1].MemberRuleIDs},
		},
	}
	idx, warns := applyDecisions(props, dEmit)
	if len(idx) != 2 {
		t.Errorf("UndecidedEmit: pending should still emit; idx=%v", idx)
	}
	if !anyWarnContains(warns, "pending") {
		t.Errorf("UndecidedEmit: pending row should produce a warning; warns=%v", warns)
	}

	// UndecidedSkip: pending rows drop out.
	dSkip := &ProposalDecisions{
		UndecidedPolicy: UndecidedSkip,
		Proposals: []ProposalDecision{
			{Key: props[0].Key(), Decision: DecisionPending, MemberRuleIDs: props[0].MemberRuleIDs},
			{Key: props[1].Key(), Decision: DecisionAccept, MemberRuleIDs: props[1].MemberRuleIDs},
		},
	}
	idx2, warns2 := applyDecisions(props, dSkip)
	if len(idx2) != 1 || idx2[0] != 1 {
		t.Errorf("UndecidedSkip: only accepted proposal[1] should emit; idx=%v", idx2)
	}
	if !anyWarnContains(warns2, "skipped") {
		t.Errorf("UndecidedSkip: pending row should warn it was skipped; warns=%v", warns2)
	}
}

func TestApplyDecisions_MissingDecisionRespectsPolicy(t *testing.T) {
	_, props := makeProposalSet()
	// Decisions file with NO row for proposal[0].
	d := &ProposalDecisions{
		UndecidedPolicy: UndecidedSkip,
		Proposals: []ProposalDecision{
			{Key: props[1].Key(), Decision: DecisionAccept, MemberRuleIDs: props[1].MemberRuleIDs},
		},
	}
	idx, warns := applyDecisions(props, d)
	if len(idx) != 1 || idx[0] != 1 {
		t.Errorf("UndecidedSkip with missing row: only proposal[1] should emit; idx=%v", idx)
	}
	if !anyWarnContains(warns, "no decision recorded") {
		t.Errorf("missing decision should produce a warning; warns=%v", warns)
	}
}

func TestApplyDecisions_StaleKeyWarns(t *testing.T) {
	_, props := makeProposalSet()
	// Decision references a key not present in current ProposalSet.
	d := &ProposalDecisions{
		Proposals: []ProposalDecision{
			{Key: "ghost-key-not-in-current-set", Decision: DecisionAccept},
			{Key: props[0].Key(), Decision: DecisionAccept, MemberRuleIDs: props[0].MemberRuleIDs},
		},
	}
	_, warns := applyDecisions(props, d)
	if !anyWarnContains(warns, "ghost-key-not-in-current-set") {
		t.Errorf("stale key should surface in warnings; got %v", warns)
	}
}

func TestApplyDecisions_MemberDriftWarnsButHonorsVerdict(t *testing.T) {
	_, props := makeProposalSet()
	d := &ProposalDecisions{
		Proposals: []ProposalDecision{
			{
				Key:      props[0].Key(),
				Decision: DecisionAccept,
				// Member list mismatches: recorded 1 member, live has 2.
				MemberRuleIDs: []string{"rule-a1"},
			},
		},
	}
	idx, warns := applyDecisions(props, d)
	// Verdict honoured: accept means emit.
	emitted := false
	for _, i := range idx {
		if i == 0 {
			emitted = true
		}
	}
	if !emitted {
		t.Errorf("member-drift accept should still emit (verdict honoured); idx=%v", idx)
	}
	if !anyWarnContains(warns, "re-review recommended") {
		t.Errorf("member drift should produce re-review warning; got %v", warns)
	}
}

func TestApplyDecisions_UnknownVerdictTreatedAsPending(t *testing.T) {
	_, props := makeProposalSet()
	d := &ProposalDecisions{
		UndecidedPolicy: UndecidedEmit,
		Proposals: []ProposalDecision{
			{Key: props[0].Key(), Decision: "maybe", MemberRuleIDs: props[0].MemberRuleIDs},
		},
	}
	idx, warns := applyDecisions(props, d)
	// Unknown + UndecidedEmit → emit but warn.
	emitted := false
	for _, i := range idx {
		if i == 0 {
			emitted = true
		}
	}
	if !emitted {
		t.Errorf("unknown verdict + UndecidedEmit should emit; idx=%v", idx)
	}
	if !anyWarnContains(warns, "unknown decision verdict") {
		t.Errorf("unknown verdict should produce a typo-surface warning; got %v", warns)
	}
}

func TestApplyDecisions_DuplicateKeyWarns(t *testing.T) {
	_, props := makeProposalSet()
	d := &ProposalDecisions{
		Proposals: []ProposalDecision{
			{Key: props[0].Key(), Decision: DecisionAccept, MemberRuleIDs: props[0].MemberRuleIDs},
			// Same key, different verdict — reviewer copy-paste error.
			{Key: props[0].Key(), Decision: DecisionReject, MemberRuleIDs: props[0].MemberRuleIDs},
			{Key: props[1].Key(), Decision: DecisionAccept, MemberRuleIDs: props[1].MemberRuleIDs},
		},
	}
	_, warns := applyDecisions(props, d)
	if !anyWarnContains(warns, "duplicate key") {
		t.Errorf("duplicate key in decisions file should produce a warning; got %v", warns)
	}
}

func TestApplyDecisions_EmptyKeyEntriesTolerated(t *testing.T) {
	_, props := makeProposalSet()
	d := &ProposalDecisions{
		Proposals: []ProposalDecision{
			{Key: "", Decision: DecisionReject}, // junk row, ignored
			{Key: props[0].Key(), Decision: DecisionAccept, MemberRuleIDs: props[0].MemberRuleIDs},
			{Key: props[1].Key(), Decision: DecisionAccept, MemberRuleIDs: props[1].MemberRuleIDs},
		},
	}
	idx, _ := applyDecisions(props, d)
	if len(idx) != 2 {
		t.Errorf("empty-key row should be ignored, both real proposals emit; idx=%v", idx)
	}
}

// --- Integration: EmitProposals respects the filter --------------------

func TestEmitProposals_DecisionsFilterIntegration(t *testing.T) {
	// Two proposals; reject one via decisions; assert only the
	// accepted one's artifacts land in EmissionOutput.Files.
	rules := []parser.ParsedRule{
		{SourceRuleID: "rule-a1", Expr: "vector(1)", Labels: map[string]string{"tenant": "ta1"}, Dialect: "prom"},
		{SourceRuleID: "rule-a2", Expr: "vector(1)", Labels: map[string]string{"tenant": "ta2"}, Dialect: "prom"},
		{SourceRuleID: "rule-b1", Expr: "vector(2)", Labels: map[string]string{"tenant": "tb1"}, Dialect: "prom"},
		{SourceRuleID: "rule-b2", Expr: "vector(2)", Labels: map[string]string{"tenant": "tb2"}, Dialect: "prom"},
	}
	propA := ExtractionProposal{
		MemberRuleIDs:      []string{"rule-a1", "rule-a2"},
		SharedExprTemplate: "vector(1)",
		Confidence:         ConfidenceHigh,
		Dialect:            "prom",
		VaryingLabelKeys:   []string{"tenant"},
	}
	propB := ExtractionProposal{
		MemberRuleIDs:      []string{"rule-b1", "rule-b2"},
		SharedExprTemplate: "vector(2)",
		Confidence:         ConfidenceHigh,
		Dialect:            "prom",
		VaryingLabelKeys:   []string{"tenant"},
	}
	set := &ProposalSet{Proposals: []ExtractionProposal{propA, propB}}
	decisions := &ProposalDecisions{
		Proposals: []ProposalDecision{
			{Key: propA.Key(), Decision: DecisionAccept, MemberRuleIDs: propA.MemberRuleIDs},
			{Key: propB.Key(), Decision: DecisionReject, MemberRuleIDs: propB.MemberRuleIDs},
		},
	}
	out, err := EmitProposals(EmissionInput{
		ProposalSet: set,
		AllRules:    rules,
		Layout: EmissionLayout{
			ProposalDirs: []string{"cluster-a", "cluster-b"},
			RootPrefix:   "out",
		},
		Decisions: decisions,
	})
	if err != nil {
		t.Fatalf("EmitProposals: %v", err)
	}
	// Cluster A's artifacts SHOULD be present.
	if _, ok := out.Files["out/cluster-a/PROPOSAL.md"]; !ok {
		t.Errorf("accepted cluster-a should emit PROPOSAL.md; files=%v", keys(out.Files))
	}
	// Cluster B's artifacts SHOULD NOT be present.
	for path := range out.Files {
		if strings.HasPrefix(path, "out/cluster-b/") {
			t.Errorf("rejected cluster-b should not emit any artifact; got %q", path)
		}
	}
}

func TestEmitProposals_DecisionsScaffoldEmission(t *testing.T) {
	set, _ := makeProposalSet()
	rules := []parser.ParsedRule{
		{SourceRuleID: "rule-a1", Expr: "vector(1)", Labels: map[string]string{"tenant": "ta"}, Dialect: "prom"},
		{SourceRuleID: "rule-a2", Expr: "vector(1)", Labels: map[string]string{"tenant": "tb"}, Dialect: "prom"},
		{SourceRuleID: "rule-b1", Expr: "vector(2)", Labels: map[string]string{"tenant": "tc"}, Dialect: "prom"},
		{SourceRuleID: "rule-b2", Expr: "vector(2)", Labels: map[string]string{"tenant": "td"}, Dialect: "prom"},
	}
	// Re-state varying label keys so per-tenant files actually emit
	// (TestEmitProposals_DecisionsFilterIntegration uses real props
	// with VaryingLabelKeys; makeProposalSet's stripped-down props
	// don't, so add them back here).
	for i := range set.Proposals {
		set.Proposals[i].VaryingLabelKeys = []string{"tenant"}
	}
	out, err := EmitProposals(EmissionInput{
		ProposalSet: set,
		AllRules:    rules,
		Layout: EmissionLayout{
			ProposalDirs: []string{"a", "b"},
			RootPrefix:   "out",
		},
		EmitDecisionsScaffold: true,
	})
	if err != nil {
		t.Fatalf("EmitProposals: %v", err)
	}
	scaffold, ok := out.Files["out/proposal-decisions.yaml"]
	if !ok {
		t.Fatalf("scaffold should land at out/proposal-decisions.yaml; files=%v", keys(out.Files))
	}
	parsed, err := ParseDecisions(scaffold)
	if err != nil {
		t.Fatalf("scaffold should round-trip parse: %v", err)
	}
	if len(parsed.Proposals) != 2 {
		t.Errorf("scaffold should have 2 proposal rows; got %d", len(parsed.Proposals))
	}
	for i, row := range parsed.Proposals {
		if row.Decision != DecisionPending {
			t.Errorf("scaffold row[%d] should default to pending; got %q", i, row.Decision)
		}
	}
}

func TestEmitProposals_NoDecisionsBackwardCompat(t *testing.T) {
	// Pinning the backward-compat contract: a call without
	// Decisions/EmitDecisionsScaffold should produce IDENTICAL
	// output to PR-2/PR-3.
	set, _ := makeProposalSet()
	rules := []parser.ParsedRule{
		{SourceRuleID: "rule-a1", Expr: "vector(1)", Labels: map[string]string{"tenant": "ta"}, Dialect: "prom"},
		{SourceRuleID: "rule-a2", Expr: "vector(1)", Labels: map[string]string{"tenant": "tb"}, Dialect: "prom"},
		{SourceRuleID: "rule-b1", Expr: "vector(2)", Labels: map[string]string{"tenant": "tc"}, Dialect: "prom"},
		{SourceRuleID: "rule-b2", Expr: "vector(2)", Labels: map[string]string{"tenant": "td"}, Dialect: "prom"},
	}
	for i := range set.Proposals {
		set.Proposals[i].VaryingLabelKeys = []string{"tenant"}
	}
	out, err := EmitProposals(EmissionInput{
		ProposalSet: set,
		AllRules:    rules,
		Layout: EmissionLayout{
			ProposalDirs: []string{"a", "b"},
			RootPrefix:   "out",
		},
		// Decisions: nil, EmitDecisionsScaffold: false  ← defaults
	})
	if err != nil {
		t.Fatalf("EmitProposals: %v", err)
	}
	if _, ok := out.Files["out/proposal-decisions.yaml"]; ok {
		t.Errorf("backward-compat: no scaffold file should land when EmitDecisionsScaffold=false")
	}
	// Both proposals' PROPOSAL.md should land.
	for _, p := range []string{"out/a/PROPOSAL.md", "out/b/PROPOSAL.md"} {
		if _, ok := out.Files[p]; !ok {
			t.Errorf("backward-compat: %s should emit when Decisions is nil; files=%v", p, keys(out.Files))
		}
	}
}

// helpers

func anyWarnContains(warns []string, sub string) bool {
	for _, w := range warns {
		if strings.Contains(w, sub) {
			return true
		}
	}
	return false
}

func keys(m map[string][]byte) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
