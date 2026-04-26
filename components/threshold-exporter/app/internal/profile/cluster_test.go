package profile

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"

	"github.com/vencil/threshold-exporter/internal/parser"
)

const testGeneratedBy = "da-tools@tools-v2.8.0 profile-test"

// loadFixtureRules parses a testdata YAML through the C-8 parser
// and returns the rules. Failures here are fixture errors, not
// profile-builder bugs — fail the test loudly.
func loadFixtureRules(t *testing.T, name string) []parser.ParsedRule {
	t.Helper()
	path := filepath.Join("testdata", name)
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read fixture %s: %v", path, err)
	}
	res, err := parser.ParsePromRules(b, name, testGeneratedBy)
	if err != nil {
		t.Fatalf("parse fixture %s: %v", path, err)
	}
	return res.Rules
}

func TestBuildProposals_BasicClusterAndUnclustered(t *testing.T) {
	rules := loadFixtureRules(t, "cluster_basic.yaml")
	got, err := BuildProposals(rules, ClusterOptions{})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}

	if len(got.Proposals) != 1 {
		t.Fatalf("got %d proposals, want 1 (HighCPU group)", len(got.Proposals))
	}
	if len(got.Unclustered) != 2 {
		t.Errorf("got %d unclustered, want 2 (DiskSpaceLow + MemoryPressure)", len(got.Unclustered))
	}

	p := got.Proposals[0]
	if len(p.MemberRuleIDs) != 3 {
		t.Errorf("HighCPU proposal: %d members, want 3", len(p.MemberRuleIDs))
	}
	if p.SharedFor != "5m" {
		t.Errorf("SharedFor = %q, want 5m", p.SharedFor)
	}
	if p.SharedLabels["severity"] != "warning" {
		t.Errorf("SharedLabels[severity] = %q, want warning", p.SharedLabels["severity"])
	}
	if !contains(p.VaryingLabelKeys, "tenant") {
		t.Errorf("VaryingLabelKeys = %v, want to contain `tenant`", p.VaryingLabelKeys)
	}
	if p.Confidence != ConfidenceHigh {
		t.Errorf("Confidence = %q, want high", p.Confidence)
	}
	// Stats consistency.
	if got.Stats.RulesClustered+got.Stats.RulesUnclustered != got.Stats.TotalRulesIn {
		t.Errorf("stats: clustered (%d) + unclustered (%d) ≠ total (%d)",
			got.Stats.RulesClustered, got.Stats.RulesUnclustered, got.Stats.TotalRulesIn)
	}
}

func TestBuildProposals_DialectSplitsClusters(t *testing.T) {
	rules := loadFixtureRules(t, "cluster_dialect_split.yaml")
	got, err := BuildProposals(rules, ClusterOptions{})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	// Two proposals expected — one per dialect — even though the
	// surrounding expr shape is similar.
	if len(got.Proposals) != 2 {
		t.Fatalf("got %d proposals, want 2 (one per dialect)", len(got.Proposals))
	}
	dialects := make(map[string]int)
	for _, p := range got.Proposals {
		dialects[p.Dialect]++
	}
	if dialects["prom"] != 1 || dialects["metricsql"] != 1 {
		t.Errorf("dialect distribution = %v, want one prom + one metricsql", dialects)
	}
	// Each cluster has 2 members.
	for i, p := range got.Proposals {
		if len(p.MemberRuleIDs) != 2 {
			t.Errorf("proposal[%d] has %d members, want 2", i, len(p.MemberRuleIDs))
		}
	}
}

func TestBuildProposals_LabelPartitioning(t *testing.T) {
	rules := loadFixtureRules(t, "cluster_label_partition.yaml")
	got, err := BuildProposals(rules, ClusterOptions{})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	if len(got.Proposals) != 1 {
		t.Fatalf("got %d proposals, want 1", len(got.Proposals))
	}
	p := got.Proposals[0]
	// `severity` is identical (warning) → shared.
	if p.SharedLabels["severity"] != "warning" {
		t.Errorf("severity not partitioned to shared: %v", p.SharedLabels)
	}
	// `tenant` differs → varying.
	if !contains(p.VaryingLabelKeys, "tenant") {
		t.Errorf("tenant missing from VaryingLabelKeys: %v", p.VaryingLabelKeys)
	}
	// `team` differs (backend vs payments vs backend) → varying.
	if !contains(p.VaryingLabelKeys, "team") {
		t.Errorf("team missing from VaryingLabelKeys: %v", p.VaryingLabelKeys)
	}
	// VaryingLabelKeys must be sorted.
	if !sort.StringsAreSorted(p.VaryingLabelKeys) {
		t.Errorf("VaryingLabelKeys not sorted: %v", p.VaryingLabelKeys)
	}
}

func TestBuildProposals_MinClusterSizeRespected(t *testing.T) {
	rules := loadFixtureRules(t, "cluster_basic.yaml")
	// Set min=4; the HighCPU group has only 3 members so it must
	// drop to Unclustered.
	got, err := BuildProposals(rules, ClusterOptions{MinClusterSize: 4})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	if len(got.Proposals) != 0 {
		t.Errorf("got %d proposals, want 0 (HighCPU group below min)", len(got.Proposals))
	}
	if got.Stats.RulesUnclustered != got.Stats.TotalRulesIn {
		t.Errorf("expected all rules unclustered when min not met")
	}
}

func TestBuildProposals_EmptyInputErrors(t *testing.T) {
	_, err := BuildProposals(nil, ClusterOptions{})
	if err == nil {
		t.Error("err = nil for empty input, want error")
	}
}

func TestBuildProposals_AmbiguousRulesGoToUnclustered(t *testing.T) {
	// Fabricate an ambiguous rule (parser couldn't classify) — it
	// has no usable signature, so the engine must surface it as
	// Unclustered (default behaviour) rather than dropping silently.
	rules := []parser.ParsedRule{
		{
			SourceRuleID: "fixture#rules[0]",
			Alert:        "BrokenAlert",
			Expr:         "", // empty — yields empty signature
			Dialect:      parser.DialectAmbiguous,
		},
		{
			SourceRuleID: "fixture#rules[1]",
			Alert:        "PortableAlert",
			Expr:         "up == 0",
			For:          "1m",
			Dialect:      parser.DialectProm,
			Labels:       map[string]string{"severity": "critical"},
		},
		{
			SourceRuleID: "fixture#rules[2]",
			Alert:        "PortableAlert",
			Expr:         "up == 0",
			For:          "1m",
			Dialect:      parser.DialectProm,
			Labels:       map[string]string{"severity": "critical"},
		},
	}
	got, err := BuildProposals(rules, ClusterOptions{})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	if len(got.Proposals) != 1 {
		t.Errorf("got %d proposals, want 1 (PortableAlert pair)", len(got.Proposals))
	}
	if !contains(got.Unclustered, "fixture#rules[0]") {
		t.Errorf("ambiguous rule missing from Unclustered: %v", got.Unclustered)
	}
}

func TestBuildProposals_SkipAmbiguousDropsThemEntirely(t *testing.T) {
	rules := []parser.ParsedRule{
		{SourceRuleID: "x#0", Expr: "", Dialect: parser.DialectAmbiguous},
		{SourceRuleID: "x#1", Expr: "up", Dialect: parser.DialectProm},
		{SourceRuleID: "x#2", Expr: "up", Dialect: parser.DialectProm},
	}
	got, err := BuildProposals(rules, ClusterOptions{SkipAmbiguous: true})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	if contains(got.Unclustered, "x#0") {
		t.Errorf("ambiguous rule leaked to Unclustered despite SkipAmbiguous=true")
	}
	// Stats.TotalRulesIn still reflects the original input — the
	// counter measures what the caller passed, not what survived
	// filtering.
	if got.Stats.TotalRulesIn != 3 {
		t.Errorf("Stats.TotalRulesIn = %d, want 3", got.Stats.TotalRulesIn)
	}
}

func TestBuildProposals_SavingsEstimate(t *testing.T) {
	// 3 rules in cluster_basic share severity + for=5m + (alert in
	// the cluster always shares severity but tenant varies).
	// Shared field count for the HighCPU cluster:
	//   - For (5m) → 1
	//   - severity → 1
	//   total = 2
	// Members = 3 → savings = (3 - 1) × 2 = 4 lines.
	rules := loadFixtureRules(t, "cluster_basic.yaml")
	got, err := BuildProposals(rules, ClusterOptions{})
	if err != nil {
		t.Fatalf("BuildProposals: %v", err)
	}
	p := got.Proposals[0]
	if p.EstimatedYAMLLineSavings != 4 {
		t.Errorf("EstimatedYAMLLineSavings = %d, want 4 (3 rules × 2 shared fields × (3-1) factor)",
			p.EstimatedYAMLLineSavings)
	}
}

func TestBuildProposals_DeterministicOutput(t *testing.T) {
	// Run the same input through BuildProposals twice and assert
	// the JSON serialisation is byte-identical. Catches the common
	// "map iteration order leaked into output" regression.
	rules := loadFixtureRules(t, "cluster_basic.yaml")
	r1, err := BuildProposals(rules, ClusterOptions{})
	if err != nil {
		t.Fatalf("BuildProposals run 1: %v", err)
	}
	r2, err := BuildProposals(rules, ClusterOptions{})
	if err != nil {
		t.Fatalf("BuildProposals run 2: %v", err)
	}
	j1, _ := json.Marshal(r1)
	j2, _ := json.Marshal(r2)
	if string(j1) != string(j2) {
		t.Errorf("non-deterministic output:\nrun 1: %s\nrun 2: %s", j1, j2)
	}

	// Also assert MemberRuleIDs is sorted within each proposal.
	for _, p := range r1.Proposals {
		if !sort.StringsAreSorted(p.MemberRuleIDs) {
			t.Errorf("MemberRuleIDs not sorted: %v", p.MemberRuleIDs)
		}
	}
}

func TestBuildProposals_OnlyEmptyExprsErrorsFromUpstream(t *testing.T) {
	// Edge case: the caller hands in only ambiguous rules and asks
	// to skip them. Result is an empty ProposalSet but no fatal
	// error from BuildProposals (the upstream parser may legitimately
	// have produced a batch where every rule was unparseable).
	rules := []parser.ParsedRule{
		{SourceRuleID: "x#0", Expr: "", Dialect: parser.DialectAmbiguous},
		{SourceRuleID: "x#1", Expr: "", Dialect: parser.DialectAmbiguous},
	}
	got, err := BuildProposals(rules, ClusterOptions{SkipAmbiguous: true})
	if err != nil {
		t.Fatalf("BuildProposals: %v, want nil for all-ambiguous-skipped input", err)
	}
	if len(got.Proposals) != 0 || len(got.Unclustered) != 0 {
		t.Errorf("expected fully empty result; got %d proposals + %d unclustered",
			len(got.Proposals), len(got.Unclustered))
	}
	if got.Stats.TotalRulesIn != 2 {
		t.Errorf("Stats.TotalRulesIn = %d, want 2", got.Stats.TotalRulesIn)
	}
}

// TestSignatureFor_DialectIncluded keeps the dialect-in-signature
// invariant unit-testable in case the structural test (DialectSplits)
// regresses without the fixture being updated.
func TestSignatureFor_DialectIncluded(t *testing.T) {
	a := parser.ParsedRule{Expr: "up == 0", For: "1m", Dialect: parser.DialectProm}
	b := parser.ParsedRule{Expr: "up == 0", For: "1m", Dialect: parser.DialectMetricsQL}
	if signatureFor(a) == signatureFor(b) {
		t.Errorf("same expr/for but different dialect must yield different signatures:\n  prom: %q\n  mql:  %q",
			signatureFor(a), signatureFor(b))
	}
}

// helpers below ------------------------------------------------------

func contains(haystack []string, needle string) bool {
	for _, s := range haystack {
		if s == needle {
			return true
		}
	}
	return false
}
