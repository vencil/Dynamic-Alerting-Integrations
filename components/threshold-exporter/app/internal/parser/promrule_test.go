package parser

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const testGeneratedBy = "da-tools@tools-v2.8.0 parser@test"

func mustReadTestdata(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatalf("read %s: %v", name, err)
	}
	return b
}

func TestParsePromRules_BasicWrappedShape(t *testing.T) {
	src := mustReadTestdata(t, "promrule_basic.yaml")
	res, err := ParsePromRules(src, "promrule_basic.yaml", testGeneratedBy)
	if err != nil {
		t.Fatalf("ParsePromRules: %v", err)
	}
	if len(res.Rules) != 2 {
		t.Fatalf("got %d rules, want 2", len(res.Rules))
	}
	if len(res.Warnings) != 0 {
		t.Errorf("warnings = %v, want none", res.Warnings)
	}

	r0 := res.Rules[0]
	if r0.Alert != "HighCPU" || r0.Record != "" {
		t.Errorf("rule[0] alert/record = (%q, %q)", r0.Alert, r0.Record)
	}
	if r0.Dialect != DialectProm {
		t.Errorf("rule[0] dialect = %q, want prom", r0.Dialect)
	}
	if !r0.PromPortable {
		t.Errorf("rule[0] PromPortable = false, want true")
	}
	if r0.For != "5m" {
		t.Errorf("rule[0] for = %q, want 5m", r0.For)
	}
	if r0.Labels["severity"] != "warning" {
		t.Errorf("rule[0] severity = %q, want warning", r0.Labels["severity"])
	}
	if !strings.Contains(r0.SourceRuleID, "groups[0].rules[0]") {
		t.Errorf("rule[0] SourceRuleID = %q, want groups[0].rules[0] suffix", r0.SourceRuleID)
	}

	r1 := res.Rules[1]
	if r1.Record != "job:cpu:rate5m" || r1.Alert != "" {
		t.Errorf("rule[1] alert/record = (%q, %q)", r1.Alert, r1.Record)
	}
	if r1.Dialect != DialectProm {
		t.Errorf("rule[1] dialect = %q, want prom", r1.Dialect)
	}
}

func TestParsePromRules_VMOnlyDialectClassified(t *testing.T) {
	src := mustReadTestdata(t, "promrule_metricsql.yaml")
	res, err := ParsePromRules(src, "promrule_metricsql.yaml", testGeneratedBy)
	if err != nil {
		t.Fatalf("ParsePromRules: %v", err)
	}
	if len(res.Rules) != 3 {
		t.Fatalf("got %d rules, want 3", len(res.Rules))
	}
	for i, r := range res.Rules {
		if r.Dialect != DialectMetricsQL {
			t.Errorf("rule[%d] dialect = %q, want metricsql", i, r.Dialect)
		}
		if r.PromPortable {
			t.Errorf("rule[%d] PromPortable = true, want false (uses VM-only fn)", i)
		}
		if len(r.VMOnlyFunctions) == 0 {
			t.Errorf("rule[%d] VMOnlyFunctions empty, want at least one entry", i)
		}
	}
}

func TestParsePromRules_MixedDialectsPreserveIndividual(t *testing.T) {
	src := mustReadTestdata(t, "promrule_mixed.yaml")
	res, err := ParsePromRules(src, "promrule_mixed.yaml", testGeneratedBy)
	if err != nil {
		t.Fatalf("ParsePromRules: %v", err)
	}
	if len(res.Rules) != 4 {
		t.Fatalf("got %d rules, want 4", len(res.Rules))
	}
	// Order in fixture: portable / portable / vm / portable
	wantDialects := []Dialect{DialectProm, DialectProm, DialectMetricsQL, DialectProm}
	for i, want := range wantDialects {
		if res.Rules[i].Dialect != want {
			t.Errorf("rule[%d] dialect = %q, want %q", i, res.Rules[i].Dialect, want)
		}
	}
	// SourceRuleID disambiguates groups: the VM rule lives in
	// groups[1], not groups[0].
	if !strings.Contains(res.Rules[2].SourceRuleID, "groups[1].rules[0]") {
		t.Errorf("rule[2] SourceRuleID = %q, want groups[1].rules[0] suffix", res.Rules[2].SourceRuleID)
	}
}

func TestParsePromRules_UnwrappedShape(t *testing.T) {
	src := mustReadTestdata(t, "promrule_unwrapped.yaml")
	res, err := ParsePromRules(src, "promrule_unwrapped.yaml", testGeneratedBy)
	if err != nil {
		t.Fatalf("ParsePromRules: %v", err)
	}
	if len(res.Rules) != 1 {
		t.Fatalf("got %d rules, want 1", len(res.Rules))
	}
	if res.Rules[0].Alert != "DiskFull" {
		t.Errorf("alert = %q, want DiskFull", res.Rules[0].Alert)
	}
}

func TestParsePromRules_AmbiguousAndMissingNameWarning(t *testing.T) {
	src := mustReadTestdata(t, "promrule_ambiguous.yaml")
	res, err := ParsePromRules(src, "promrule_ambiguous.yaml", testGeneratedBy)
	if err != nil {
		t.Fatalf("ParsePromRules: %v", err)
	}
	if len(res.Rules) != 2 {
		t.Fatalf("got %d rules, want 2", len(res.Rules))
	}
	r0 := res.Rules[0]
	if r0.Dialect != DialectAmbiguous {
		t.Errorf("rule[0] dialect = %q, want ambiguous", r0.Dialect)
	}
	if r0.AnalyzeError == "" {
		t.Errorf("rule[0] AnalyzeError empty, want metricsql parse error message")
	}
	if r0.PromPortable {
		t.Errorf("rule[0] PromPortable = true, want false for ambiguous")
	}

	r1 := res.Rules[1]
	if r1.Alert != "" || r1.Record != "" {
		t.Errorf("rule[1] alert/record = (%q, %q), want both empty (intentional fixture)", r1.Alert, r1.Record)
	}
	// Expression itself parses.
	if r1.Dialect != DialectProm {
		t.Errorf("rule[1] dialect = %q, want prom (expr is `up == 0`)", r1.Dialect)
	}
	// Missing-name warning must be present.
	foundWarning := false
	for _, w := range res.Warnings {
		if strings.Contains(w, "groups[0].rules[1]") && strings.Contains(w, "neither") {
			foundWarning = true
			break
		}
	}
	if !foundWarning {
		t.Errorf("expected a warning about missing alert/record name; got %v", res.Warnings)
	}
}

func TestParsePromRules_EmptyInputErrors(t *testing.T) {
	_, err := ParsePromRules(nil, "x.yaml", testGeneratedBy)
	if err == nil {
		t.Error("err = nil for empty input, want error")
	}
}

func TestParsePromRules_MalformedYAMLErrors(t *testing.T) {
	_, err := ParsePromRules([]byte("groups:\n  - [unclosed\n"), "broken.yaml", testGeneratedBy)
	if err == nil {
		t.Error("err = nil for malformed YAML, want fatal error")
	}
}

func TestParsePromRules_WrongCRDShapeYieldsWarningNotFatal(t *testing.T) {
	// A valid YAML document with no `groups:` (operator pasted the
	// wrong CRD shape) should surface as a warning, not a fatal
	// error — partial-batch tolerance is a hard contract for C-10.
	src := []byte("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: not-a-rule\n")
	res, err := ParsePromRules(src, "wrong.yaml", testGeneratedBy)
	if err != nil {
		t.Fatalf("err = %v, want nil (warning path, not fatal)", err)
	}
	if len(res.Rules) != 0 {
		t.Errorf("rules = %d, want 0", len(res.Rules))
	}
	if len(res.Warnings) != 1 {
		t.Errorf("warnings = %d, want 1; got %v", len(res.Warnings), res.Warnings)
	} else if !strings.Contains(res.Warnings[0], "no `groups`") {
		t.Errorf("warning text = %q, want mention of missing `groups`", res.Warnings[0])
	}
}

func TestParsePromRules_LegitimateEmptyGroupReturnsEmpty(t *testing.T) {
	// `groups: [{name: foo, rules: []}]` is a valid CRD shape with
	// no rules to emit — caller may legitimately submit this between
	// migration phases. Parser must return an empty ParseResult, not
	// an error.
	src := []byte("groups:\n  - name: empty\n    rules: []\n")
	res, err := ParsePromRules(src, "empty.yaml", testGeneratedBy)
	if err != nil {
		t.Fatalf("err = %v, want nil for empty-rules group", err)
	}
	if len(res.Rules) != 0 {
		t.Errorf("rules = %d, want 0", len(res.Rules))
	}
	if len(res.Warnings) != 0 {
		t.Errorf("warnings = %v, want none for legitimate empty input", res.Warnings)
	}
}

func TestParsePromRules_ProvenanceStamped(t *testing.T) {
	src := mustReadTestdata(t, "promrule_basic.yaml")
	res, err := ParsePromRules(src, "promrule_basic.yaml", testGeneratedBy)
	if err != nil {
		t.Fatalf("ParsePromRules: %v", err)
	}

	if res.Provenance.GeneratedBy != testGeneratedBy {
		t.Errorf("GeneratedBy = %q", res.Provenance.GeneratedBy)
	}
	if res.Provenance.SourceFile != "promrule_basic.yaml" {
		t.Errorf("SourceFile = %q", res.Provenance.SourceFile)
	}
	// Checksum equals SHA-256(src) hex.
	want := sha256.Sum256(src)
	if res.Provenance.SourceChecksum != hex.EncodeToString(want[:]) {
		t.Errorf("SourceChecksum mismatch")
	}
	// ParsedAt should round-trip through RFC 3339.
	if _, err := time.Parse(time.RFC3339, res.Provenance.ParsedAt); err != nil {
		t.Errorf("ParsedAt %q not RFC 3339: %v", res.Provenance.ParsedAt, err)
	}
}
