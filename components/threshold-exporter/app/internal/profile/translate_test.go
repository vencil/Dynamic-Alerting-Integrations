package profile

// translate_test.go — table tests for PR-3 PromRule → conf.d
// translator. Coverage targets:
//
//   - TranslateRule: AST shape recognition for the supported
//     "<expr> {>|>=|<|<=} <number>" form, plus failure modes
//     (parse error, vector comparison, no comparison at all).
//   - resolveMetricKey: ADR-019 §metric-key-resolution order
//     (explicit label → alert/record snake_case → inner metric →
//     skipped).
//   - TranslateProposal: cluster-level aggregation, majority vote,
//     median default, per-tenant overrides.
//   - snakeCaseIdentifier: edge cases for the name-mangling helper.

import (
	"fmt"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/parser"
)

// --- TranslateRule: happy paths --------------------------------------

func TestTranslateRule_ExplicitMetricKeyLabelWins(t *testing.T) {
	r := parser.ParsedRule{
		SourceRuleID: "rules.yaml#g[0].r[0]",
		Alert:        "SomeAlert",
		Expr:         `mysql_global_status_threads_connected > 800`,
		Labels: map[string]string{
			"metric_key": "mysql_connections",
			"severity":   "warning",
		},
	}
	got, err := TranslateRule(r)
	if err != nil {
		t.Fatalf("TranslateRule: %v", err)
	}
	if got.Status != TranslationOK {
		t.Errorf("Status = %q, want ok", got.Status)
	}
	if got.MetricKey != "mysql_connections" {
		t.Errorf("MetricKey = %q, want mysql_connections (explicit label)", got.MetricKey)
	}
	if got.Threshold != 800 {
		t.Errorf("Threshold = %v, want 800", got.Threshold)
	}
	if got.Operator != ">" {
		t.Errorf("Operator = %q, want >", got.Operator)
	}
	if got.Severity != "warning" {
		t.Errorf("Severity = %q, want warning", got.Severity)
	}
	if len(got.Warnings) != 0 {
		t.Errorf("explicit label should produce zero warnings; got %v", got.Warnings)
	}
}

func TestTranslateRule_AlertNameSnakeCaseFallback(t *testing.T) {
	r := parser.ParsedRule{
		SourceRuleID: "rules.yaml#g[0].r[1]",
		Alert:        "MySQLHighConnections",
		Expr:         `count(mysql_threads) > 800`,
		Labels:       map[string]string{"severity": "critical"},
	}
	got, err := TranslateRule(r)
	if err != nil {
		t.Fatalf("TranslateRule: %v", err)
	}
	if got.Status != TranslationPartial {
		t.Errorf("Status = %q, want partial (heuristic key)", got.Status)
	}
	wantKey := "my_sql_high_connections"
	if got.MetricKey != wantKey {
		t.Errorf("MetricKey = %q, want %q (snake-case of alert name)", got.MetricKey, wantKey)
	}
	if got.Threshold != 800 {
		t.Errorf("Threshold = %v, want 800", got.Threshold)
	}
	if got.Severity != "critical" {
		t.Errorf("Severity = %q, want critical", got.Severity)
	}
	if len(got.Warnings) == 0 {
		t.Error("heuristic key should produce a warning")
	}
}

func TestTranslateRule_RecordRuleNoSeverity(t *testing.T) {
	r := parser.ParsedRule{
		SourceRuleID: "rules.yaml#g[0].r[2]",
		Record:       "instance:cpu_usage:rate5m",
		Expr:         `rate(node_cpu_seconds_total[5m]) > 0.85`,
	}
	got, err := TranslateRule(r)
	if err != nil {
		t.Fatalf("TranslateRule: %v", err)
	}
	if got.Status != TranslationPartial {
		t.Errorf("Status = %q, want partial", got.Status)
	}
	if got.Severity != "" {
		t.Errorf("recording rules should have empty severity; got %q", got.Severity)
	}
	if got.Threshold != 0.85 {
		t.Errorf("Threshold = %v, want 0.85", got.Threshold)
	}
}

func TestTranslateRule_InvertedComparisonGetsFlipped(t *testing.T) {
	// `0.85 < rate(...)` is semantically the same as `rate(...) > 0.85`.
	// Translator should normalise to "metric op threshold" form.
	r := parser.ParsedRule{
		SourceRuleID: "rules.yaml#g[0].r[3]",
		Alert:        "InvertedAlert",
		Expr:         `0.85 < rate(node_cpu_seconds_total[5m])`,
		Labels:       map[string]string{"metric_key": "cpu_rate_5m"},
	}
	got, _ := TranslateRule(r)
	if got.Threshold != 0.85 {
		t.Errorf("Threshold = %v, want 0.85", got.Threshold)
	}
	if got.Operator != ">" {
		t.Errorf("Operator = %q, want > (flipped from <)", got.Operator)
	}
}

func TestTranslateRule_AllOperators(t *testing.T) {
	cases := []struct {
		op      string
		want    string
		flipped string // when threshold is on the LEFT
	}{
		{">", ">", "<"},
		{">=", ">=", "<="},
		{"<", "<", ">"},
		{"<=", "<=", ">="},
	}
	for _, c := range cases {
		t.Run("rhs_"+c.op, func(t *testing.T) {
			r := parser.ParsedRule{
				SourceRuleID: "x", Alert: "X",
				Expr: fmt.Sprintf("metric_x %s 100", c.op),
			}
			got, _ := TranslateRule(r)
			if got.Operator != c.want {
				t.Errorf("Operator = %q, want %q", got.Operator, c.want)
			}
		})
		t.Run("lhs_"+c.op, func(t *testing.T) {
			r := parser.ParsedRule{
				SourceRuleID: "x", Alert: "X",
				Expr: fmt.Sprintf("100 %s metric_x", c.op),
			}
			got, _ := TranslateRule(r)
			if got.Operator != c.flipped {
				t.Errorf("Operator = %q, want %q (flipped)", got.Operator, c.flipped)
			}
		})
	}
}

// --- TranslateRule: skip paths ---------------------------------------

func TestTranslateRule_NoComparisonSkipped(t *testing.T) {
	r := parser.ParsedRule{
		SourceRuleID: "x",
		Record:       "rec_no_comparison",
		Expr:         `rate(node_cpu_seconds_total[5m])`,
	}
	got, _ := TranslateRule(r)
	if got.Status != TranslationSkipped {
		t.Errorf("Status = %q, want skipped (recording rule, no comparison)", got.Status)
	}
	if !strings.Contains(got.SkipReason, "no top-level numeric comparison") {
		t.Errorf("SkipReason = %q, want explanation", got.SkipReason)
	}
}

func TestTranslateRule_VectorComparisonSkipped(t *testing.T) {
	// `metric_a > metric_b` — both sides are vector exprs; no
	// numeric scalar to extract.
	r := parser.ParsedRule{
		SourceRuleID: "x", Alert: "X",
		Expr: `metric_a > on(tenant) metric_b`,
	}
	got, _ := TranslateRule(r)
	if got.Status != TranslationSkipped {
		t.Errorf("Status = %q, want skipped (vector comparison)", got.Status)
	}
}

func TestTranslateRule_ParseErrorSkipped(t *testing.T) {
	r := parser.ParsedRule{
		SourceRuleID: "x", Alert: "X",
		Expr: `this is not !! valid PromQL >`,
	}
	got, _ := TranslateRule(r)
	if got.Status != TranslationSkipped {
		t.Errorf("Status = %q, want skipped on parse error", got.Status)
	}
	if !strings.Contains(got.SkipReason, "metricsql parse error") {
		t.Errorf("SkipReason should call out parse error; got %q", got.SkipReason)
	}
}

func TestTranslateRule_EmptyExprErrors(t *testing.T) {
	_, err := TranslateRule(parser.ParsedRule{SourceRuleID: "x", Alert: "X"})
	if err == nil {
		t.Fatal("expected error on empty Expr; got nil")
	}
}

func TestTranslateRule_EqualityOperatorSkipped(t *testing.T) {
	// `==` is intentionally not supported (ADR-019 §non-goals).
	r := parser.ParsedRule{
		SourceRuleID: "x", Alert: "X",
		Expr: `metric_x == 0`,
	}
	got, _ := TranslateRule(r)
	if got.Status != TranslationSkipped {
		t.Errorf("Status = %q, want skipped for equality", got.Status)
	}
}

// --- resolveMetricKey: name resolution -------------------------------

func TestResolveMetricKey_ExplicitLabelTakesPrecedence(t *testing.T) {
	r := parser.ParsedRule{
		Alert:  "FallbackName",
		Labels: map[string]string{"metric_key": "explicit_choice"},
	}
	key, status, warnings := resolveMetricKey(r, nil)
	if key != "explicit_choice" || status != TranslationOK || len(warnings) != 0 {
		t.Errorf("got (%q, %q, %v), want (explicit_choice, ok, [])", key, status, warnings)
	}
}

func TestResolveMetricKey_AlertNameWhenNoLabel(t *testing.T) {
	r := parser.ParsedRule{Alert: "HighDiskIO"}
	key, status, warnings := resolveMetricKey(r, nil)
	if key != "high_disk_io" {
		t.Errorf("key = %q, want high_disk_io", key)
	}
	if status != TranslationPartial || len(warnings) == 0 {
		t.Errorf("expected partial+warning; got status=%q warnings=%v", status, warnings)
	}
}

func TestResolveMetricKey_RecordWhenNoAlert(t *testing.T) {
	r := parser.ParsedRule{Record: "instance:cpu:rate"}
	key, status, _ := resolveMetricKey(r, nil)
	if key == "" || status != TranslationPartial {
		t.Errorf("got (%q, %q), want non-empty + partial", key, status)
	}
}

func TestResolveMetricKey_AllSourcesEmptyReturnsSkipped(t *testing.T) {
	r := parser.ParsedRule{}
	key, status, warnings := resolveMetricKey(r, nil)
	if key != "" || status != TranslationSkipped {
		t.Errorf("got (%q, %q), want ('', skipped)", key, status)
	}
	if len(warnings) == 0 {
		t.Error("skipped resolution should emit a warning explaining why")
	}
}

// --- snakeCaseIdentifier ---------------------------------------------

func TestSnakeCaseIdentifier(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"", ""},
		{"foo", "foo"},
		{"FooBar", "foo_bar"},
		{"MySQLHighConnections", "my_sql_high_connections"},
		{"already_snake_case", "already_snake_case"},
		{"with-dashes-here", "with_dashes_here"},
		{"with spaces", "with_spaces"},
		{"trailing__underscores__", "trailing_underscores"},
		{"path/with.dots", "path_with_dots"},
		{"123StartsWithDigit", "123_starts_with_digit"},
		{"all-CAPS-WORDS", "all_caps_words"},
		{"!@#$%", ""},
	}
	for _, c := range cases {
		t.Run(c.in, func(t *testing.T) {
			got := snakeCaseIdentifier(c.in)
			if got != c.want {
				t.Errorf("snakeCaseIdentifier(%q) = %q, want %q", c.in, got, c.want)
			}
		})
	}
}

// --- TranslateProposal: cluster aggregation --------------------------

// makeClusterMember builds a parsed rule for tests with an explicit
// metric_key + tenant label for predictable cluster aggregation.
func makeClusterMember(idx int, tenantID string, threshold float64) parser.ParsedRule {
	return parser.ParsedRule{
		SourceRuleID: fmt.Sprintf("rules.yaml#g[0].r[%d]", idx),
		Alert:        "ClusterAlert",
		Expr:         fmt.Sprintf(`metric_x > %g`, threshold),
		Labels: map[string]string{
			"metric_key": "metric_x",
			"severity":   "warning",
			"tenant":     tenantID,
		},
	}
}

func TestTranslateProposal_HappyPath_PerTenantOverridesOnDivergence(t *testing.T) {
	members := []parser.ParsedRule{
		makeClusterMember(0, "tenant-a", 80),
		makeClusterMember(1, "tenant-b", 80), // same as default → no override
		makeClusterMember(2, "tenant-c", 95), // diverges → override
	}
	prop := ExtractionProposal{
		MemberRuleIDs:    []string{"rules.yaml#g[0].r[0]", "rules.yaml#g[0].r[1]", "rules.yaml#g[0].r[2]"},
		Dialect:          "prom",
		VaryingLabelKeys: []string{"tenant"},
	}

	got, err := TranslateProposal(prop, members, "tenant")
	if err != nil {
		t.Fatalf("TranslateProposal: %v", err)
	}
	if got.Status != TranslationOK {
		t.Errorf("Status = %q, want ok (all members translated cleanly)", got.Status)
	}
	if got.MetricKey != "metric_x" {
		t.Errorf("MetricKey = %q, want metric_x", got.MetricKey)
	}
	if got.DefaultThreshold != 80 {
		t.Errorf("DefaultThreshold = %v, want 80 (median)", got.DefaultThreshold)
	}
	if got.Operator != ">" {
		t.Errorf("Operator = %q, want >", got.Operator)
	}
	if got.Severity != "warning" {
		t.Errorf("Severity = %q, want warning", got.Severity)
	}
	if v, ok := got.PerTenantOverrides["tenant-c"]; !ok || v != 95 {
		t.Errorf("PerTenantOverrides[tenant-c] = (%v, %v), want (95, true)", v, ok)
	}
	if _, ok := got.PerTenantOverrides["tenant-a"]; ok {
		t.Errorf("tenant-a matches default → should NOT have override; got %v", got.PerTenantOverrides)
	}
}

func TestTranslateProposal_MetricKeyDissentMajorityWins(t *testing.T) {
	members := []parser.ParsedRule{
		// Two members vote for "metric_x"
		makeClusterMember(0, "tenant-a", 80),
		makeClusterMember(1, "tenant-b", 80),
		// One member dissents
		{
			SourceRuleID: "rules.yaml#g[0].r[2]",
			Alert:        "ClusterAlert",
			Expr:         `metric_y > 80`,
			Labels: map[string]string{
				"metric_key": "metric_y",
				"severity":   "warning",
				"tenant":     "tenant-c",
			},
		},
	}
	prop := ExtractionProposal{
		MemberRuleIDs:    []string{"rules.yaml#g[0].r[0]", "rules.yaml#g[0].r[1]", "rules.yaml#g[0].r[2]"},
		VaryingLabelKeys: []string{"tenant"},
	}
	got, err := TranslateProposal(prop, members, "tenant")
	if err != nil {
		t.Fatalf("TranslateProposal: %v", err)
	}
	if got.MetricKey != "metric_x" {
		t.Errorf("MetricKey = %q, want metric_x (majority 2/3)", got.MetricKey)
	}
	if got.Status != TranslationPartial {
		t.Errorf("Status = %q, want partial (dissent)", got.Status)
	}
	dissentWarn := false
	for _, w := range got.Warnings {
		if strings.Contains(w, "metric_key not unanimous") {
			dissentWarn = true
		}
	}
	if !dissentWarn {
		t.Errorf("expected metric_key dissent warning; got %v", got.Warnings)
	}
}

func TestTranslateProposal_MedianResistsOutliers(t *testing.T) {
	// Three members with thresholds 50, 80, 5000. Mean would be
	// 1710 (terrible); median is 80 (right answer).
	members := []parser.ParsedRule{
		makeClusterMember(0, "tenant-a", 50),
		makeClusterMember(1, "tenant-b", 80),
		makeClusterMember(2, "tenant-c", 5000),
	}
	prop := ExtractionProposal{
		MemberRuleIDs:    []string{"rules.yaml#g[0].r[0]", "rules.yaml#g[0].r[1]", "rules.yaml#g[0].r[2]"},
		VaryingLabelKeys: []string{"tenant"},
	}
	got, err := TranslateProposal(prop, members, "tenant")
	if err != nil {
		t.Fatalf("TranslateProposal: %v", err)
	}
	if got.DefaultThreshold != 80 {
		t.Errorf("DefaultThreshold = %v, want 80 (median resists outlier)", got.DefaultThreshold)
	}
	// Both non-default members should appear in overrides.
	if got.PerTenantOverrides["tenant-a"] != 50 {
		t.Errorf("tenant-a override = %v, want 50", got.PerTenantOverrides["tenant-a"])
	}
	if got.PerTenantOverrides["tenant-c"] != 5000 {
		t.Errorf("tenant-c override = %v, want 5000", got.PerTenantOverrides["tenant-c"])
	}
}

func TestTranslateProposal_AllMembersSkippedReturnsSkipped(t *testing.T) {
	members := []parser.ParsedRule{
		{SourceRuleID: "x", Alert: "X", Expr: `rate(foo[5m])`}, // no comparison
		{SourceRuleID: "y", Alert: "Y", Expr: `count(bar)`},    // no comparison
	}
	prop := ExtractionProposal{
		MemberRuleIDs: []string{"x", "y"},
	}
	got, err := TranslateProposal(prop, members, "tenant")
	if err != nil {
		t.Fatalf("TranslateProposal: %v", err)
	}
	if got.Status != TranslationSkipped {
		t.Errorf("Status = %q, want skipped (zero translations)", got.Status)
	}
	if got.MetricKey != "" {
		t.Errorf("MetricKey should be empty when skipped; got %q", got.MetricKey)
	}
}

func TestTranslateProposal_PartialMembersDowngradeStatus(t *testing.T) {
	members := []parser.ParsedRule{
		makeClusterMember(0, "tenant-a", 80),
		// One un-translatable member doesn't sink the proposal but
		// downgrades to Partial.
		{SourceRuleID: "x", Alert: "X", Expr: `rate(foo[5m])`},
	}
	prop := ExtractionProposal{
		MemberRuleIDs:    []string{"rules.yaml#g[0].r[0]", "x"},
		VaryingLabelKeys: []string{"tenant"},
	}
	got, err := TranslateProposal(prop, members, "tenant")
	if err != nil {
		t.Fatalf("TranslateProposal: %v", err)
	}
	if got.Status != TranslationPartial {
		t.Errorf("Status = %q, want partial (1 member skipped)", got.Status)
	}
	skippedWarn := false
	for _, w := range got.Warnings {
		if strings.Contains(w, "1 of 2 members were skipped") {
			skippedWarn = true
		}
	}
	if !skippedWarn {
		t.Errorf("expected skip-count warning; got %v", got.Warnings)
	}
}

func TestTranslateProposal_EmptyMembersErrors(t *testing.T) {
	_, err := TranslateProposal(ExtractionProposal{}, nil, "tenant")
	if err == nil {
		t.Fatal("expected error on zero members; got nil")
	}
}

// --- pickMajority + median --- one-line determinism checks -----------

func TestPickMajority_TieBrokenAlphabetically(t *testing.T) {
	got := pickMajority(map[string]int{"b": 2, "a": 2})
	if got != "a" {
		t.Errorf("got %q, want a (alphabetical tie-break)", got)
	}
}

func TestMedian_OddAndEven(t *testing.T) {
	if got := median([]float64{50, 80, 5000}); got != 80 {
		t.Errorf("odd-len median = %v, want 80", got)
	}
	if got := median([]float64{50, 80, 100, 5000}); got != 80 {
		t.Errorf("even-len median = %v, want 80 (lower-middle)", got)
	}
	if got := median(nil); got != 0 {
		t.Errorf("empty median = %v, want 0", got)
	}
}

// --- formatVotes is part of dissent warning message; pin output ------

func TestFormatVotes_StableSortedOutput(t *testing.T) {
	got := formatVotes(map[string]int{"b": 2, "a": 1, "c": 3})
	want := `"a"=1, "b"=2, "c"=3`
	if got != want {
		t.Errorf("formatVotes = %q, want %q (alphabetical key order)", got, want)
	}
}
