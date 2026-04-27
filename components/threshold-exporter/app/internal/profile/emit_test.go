package profile

import (
	"strings"
	"testing"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/internal/parser"
)

// fixtureProposalSetForEmit returns a 2-proposal ProposalSet with
// matching ParsedRule corpus. The proposals share these traits:
//
//   - Proposal 0: 2 members, dialect prom, varying tenant + team
//     labels, shared severity label.
//   - Proposal 1: 2 members, dialect metricsql, varying tenant only.
//
// Used by every emit test that needs a non-trivial input.
func fixtureProposalSetForEmit() (*ProposalSet, []parser.ParsedRule) {
	rules := []parser.ParsedRule{
		{
			SourceRuleID: "src.yaml#groups[0].rules[0]",
			Alert:        "HighCPU",
			Expr:         "avg(rate(node_cpu_seconds_total{tenant=\"tenant-a\"}[5m])) > 0.85",
			For:          "5m",
			Labels:       map[string]string{"tenant": "tenant-a", "team": "backend", "severity": "warning"},
			Dialect:      parser.DialectProm,
		},
		{
			SourceRuleID: "src.yaml#groups[0].rules[1]",
			Alert:        "HighCPU",
			Expr:         "avg(rate(node_cpu_seconds_total{tenant=\"tenant-b\"}[5m])) > 0.95",
			For:          "5m",
			Labels:       map[string]string{"tenant": "tenant-b", "team": "payments", "severity": "warning"},
			Dialect:      parser.DialectProm,
		},
		{
			SourceRuleID: "src.yaml#groups[1].rules[0]",
			Alert:        "RollupHigh",
			Expr:         "rollup_rate(metric{tenant=\"tenant-a\"}[5m])",
			Labels:       map[string]string{"tenant": "tenant-a"},
			Dialect:      parser.DialectMetricsQL,
		},
		{
			SourceRuleID: "src.yaml#groups[1].rules[1]",
			Alert:        "RollupHigh",
			Expr:         "rollup_rate(metric{tenant=\"tenant-b\"}[5m])",
			Labels:       map[string]string{"tenant": "tenant-b"},
			Dialect:      parser.DialectMetricsQL,
		},
	}
	ps := &ProposalSet{
		Proposals: []ExtractionProposal{
			{
				MemberRuleIDs:            []string{"src.yaml#groups[0].rules[0]", "src.yaml#groups[0].rules[1]"},
				SharedExprTemplate:       `avg(rate(node_cpu_seconds_total{tenant="<STR>"}[<NUM>m]))><NUM>`,
				SharedFor:                "5m",
				SharedLabels:             map[string]string{"severity": "warning"},
				VaryingLabelKeys:         []string{"team", "tenant"},
				Dialect:                  string(parser.DialectProm),
				EstimatedYAMLLineSavings: 4,
				Confidence:               ConfidenceHigh,
				Reason:                   "2 rules share the same expression template, dialect=prom, for=\"5m\"",
			},
			{
				MemberRuleIDs:            []string{"src.yaml#groups[1].rules[0]", "src.yaml#groups[1].rules[1]"},
				SharedExprTemplate:       `rollup_rate(metric{tenant="<STR>"}[<NUM>m])`,
				VaryingLabelKeys:         []string{"tenant"},
				Dialect:                  string(parser.DialectMetricsQL),
				EstimatedYAMLLineSavings: 1,
				Confidence:               ConfidenceHigh,
				Reason:                   "2 rules share the same expression template, dialect=metricsql, for=\"\"",
			},
		},
	}
	return ps, rules
}

// --- input validation -----------------------------------------------

func TestEmit_ErrorOnNilProposalSet(t *testing.T) {
	_, err := EmitProposals(EmissionInput{})
	if err == nil {
		t.Fatal("err = nil for nil ProposalSet, want error")
	}
}

func TestEmit_ErrorOnEmptyProposals(t *testing.T) {
	_, err := EmitProposals(EmissionInput{ProposalSet: &ProposalSet{}})
	if err == nil {
		t.Fatal("err = nil for empty Proposals, want error")
	}
}

func TestEmit_ErrorOnLayoutLengthMismatch(t *testing.T) {
	ps, rules := fixtureProposalSetForEmit()
	_, err := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"only-one-dir"}}, // ps has 2
	})
	if err == nil {
		t.Fatal("err = nil for length mismatch, want error")
	}
	if !strings.Contains(err.Error(), "length") {
		t.Errorf("err = %v, want mention of length mismatch", err)
	}
}

// --- happy path ----------------------------------------------------

func TestEmit_BasicHappyPath(t *testing.T) {
	ps, rules := fixtureProposalSetForEmit()
	got, err := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout: EmissionLayout{
			ProposalDirs: []string{"dom-a", "dom-b"},
			RootPrefix:   "conf.d/",
		},
	})
	if err != nil {
		t.Fatalf("EmitProposals: %v", err)
	}
	// Per proposal: 1 _defaults.yaml + 2 tenant.yaml + 1 PROPOSAL.md
	// = 4 files × 2 proposals = 8 total.
	wantFiles := []string{
		"conf.d/dom-a/_defaults.yaml",
		"conf.d/dom-a/tenant-a.yaml",
		"conf.d/dom-a/tenant-b.yaml",
		"conf.d/dom-a/PROPOSAL.md",
		"conf.d/dom-b/_defaults.yaml",
		"conf.d/dom-b/tenant-a.yaml",
		"conf.d/dom-b/tenant-b.yaml",
		"conf.d/dom-b/PROPOSAL.md",
	}
	if len(got.Files) != len(wantFiles) {
		t.Errorf("got %d files, want %d (got: %v)", len(got.Files), len(wantFiles), keysOfFiles(got.Files))
	}
	for _, p := range wantFiles {
		if _, ok := got.Files[p]; !ok {
			t.Errorf("missing file %q", p)
		}
	}
	if len(got.Warnings) != 0 {
		t.Errorf("got warnings: %v", got.Warnings)
	}
}

func TestEmit_DefaultsYAMLContents(t *testing.T) {
	ps, rules := fixtureProposalSetForEmit()
	got, _ := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"a", "b"}},
	})
	defaults := got.Files["a/_defaults.yaml"]
	if defaults == nil {
		t.Fatal("missing a/_defaults.yaml")
	}
	var doc map[string]any
	if err := yaml.Unmarshal(defaults, &doc); err != nil {
		t.Fatalf("unmarshal _defaults.yaml: %v", err)
	}
	if doc["dialect"] != "prom" {
		t.Errorf("dialect = %v, want prom", doc["dialect"])
	}
	if doc["prom_portable"] != true {
		t.Errorf("prom_portable = %v, want true (dialect=prom)", doc["prom_portable"])
	}
	if doc["shared_for"] != "5m" {
		t.Errorf("shared_for = %v, want 5m", doc["shared_for"])
	}
	if doc["confidence"] != "high" {
		t.Errorf("confidence = %v, want high", doc["confidence"])
	}
	labels, _ := doc["shared_labels"].(map[string]any)
	if labels["severity"] != "warning" {
		t.Errorf("shared_labels.severity = %v, want warning", labels["severity"])
	}
}

func TestEmit_TenantYAMLContents(t *testing.T) {
	ps, rules := fixtureProposalSetForEmit()
	got, _ := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"a", "b"}},
	})
	tenantYAML := got.Files["a/tenant-a.yaml"]
	if tenantYAML == nil {
		t.Fatal("missing a/tenant-a.yaml")
	}
	var doc map[string]any
	if err := yaml.Unmarshal(tenantYAML, &doc); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	tenants, _ := doc["tenants"].(map[string]any)
	if tenants == nil {
		t.Fatal("missing `tenants:` wrapper")
	}
	tenantA, _ := tenants["tenant-a"].(map[string]any)
	if tenantA == nil {
		t.Fatal("missing tenant-a block")
	}
	if tenantA["alert"] != "HighCPU" {
		t.Errorf("alert = %v, want HighCPU", tenantA["alert"])
	}
	if tenantA["source_rule_id"] != "src.yaml#groups[0].rules[0]" {
		t.Errorf("source_rule_id = %v", tenantA["source_rule_id"])
	}
	// Severity label is shared → must NOT appear in tenant override.
	labels, _ := tenantA["labels"].(map[string]any)
	if _, present := labels["severity"]; present {
		t.Errorf("tenant override should NOT carry shared label severity; got labels=%v", labels)
	}
	// tenant + team are varying → should appear.
	if labels["tenant"] != "tenant-a" {
		t.Errorf("labels.tenant = %v, want tenant-a", labels["tenant"])
	}
	if labels["team"] != "backend" {
		t.Errorf("labels.team = %v, want backend", labels["team"])
	}
}

func TestEmit_TenantKeyHeuristic_PrefersTenantLabel(t *testing.T) {
	ps, rules := fixtureProposalSetForEmit()
	got, _ := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"a", "b"}},
	})
	// Proposal 0's VaryingLabelKeys = ["team", "tenant"] sorted.
	// Heuristic picks "tenant" because it's in the list (preferred
	// over alphabetic first). Verify by checking the tenant id used
	// in the file name maps to the rule's `tenant` label.
	if _, ok := got.Files["a/tenant-a.yaml"]; !ok {
		t.Errorf("expected file keyed on tenant=tenant-a; files: %v", keysOfFiles(got.Files))
	}
	if _, ok := got.Files["a/tenant-b.yaml"]; !ok {
		t.Errorf("expected file keyed on tenant=tenant-b; files: %v", keysOfFiles(got.Files))
	}
	// And NOT keyed on team.
	if _, ok := got.Files["a/backend.yaml"]; ok {
		t.Errorf("file unexpectedly keyed on team label: backend.yaml present")
	}
}

func TestEmit_TenantKeyHeuristic_FallsBackToFirstVaryingKey(t *testing.T) {
	// A proposal whose VaryingLabelKeys doesn't contain "tenant" —
	// emitter should fall back to the first (alphabetical) varying
	// key. Build a minimal ProposalSet/rules that exercises this.
	rules := []parser.ParsedRule{
		{
			SourceRuleID: "x#0",
			Alert:        "TeamAlert",
			Expr:         "metric > 1",
			Labels:       map[string]string{"team": "alpha", "stage": "prod"},
			Dialect:      parser.DialectProm,
		},
		{
			SourceRuleID: "x#1",
			Alert:        "TeamAlert",
			Expr:         "metric > 1",
			Labels:       map[string]string{"team": "beta", "stage": "prod"},
			Dialect:      parser.DialectProm,
		},
	}
	ps := &ProposalSet{
		Proposals: []ExtractionProposal{
			{
				MemberRuleIDs:    []string{"x#0", "x#1"},
				VaryingLabelKeys: []string{"team"}, // no `tenant`
				SharedLabels:     map[string]string{"stage": "prod"},
				Dialect:          string(parser.DialectProm),
				Confidence:       ConfidenceHigh,
				Reason:           "fixture",
			},
		},
	}
	got, err := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"d"}},
	})
	if err != nil {
		t.Fatalf("EmitProposals: %v", err)
	}
	if _, ok := got.Files["d/alpha.yaml"]; !ok {
		t.Errorf("expected fallback to team label → alpha.yaml; files: %v", keysOfFiles(got.Files))
	}
	if _, ok := got.Files["d/beta.yaml"]; !ok {
		t.Errorf("expected beta.yaml; files: %v", keysOfFiles(got.Files))
	}
}

// --- warning paths -------------------------------------------------

func TestEmit_EmptyDirEntryGoesToWarnings(t *testing.T) {
	ps, rules := fixtureProposalSetForEmit()
	got, err := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"", "ok-dir"}}, // first empty
	})
	if err != nil {
		t.Fatalf("EmitProposals: %v", err)
	}
	if len(got.Warnings) != 1 {
		t.Errorf("warnings = %d, want 1 (empty dir entry)", len(got.Warnings))
	}
	// Proposal 0 emits nothing; proposal 1 still emits 4 files.
	for k := range got.Files {
		if strings.HasPrefix(k, "/") || strings.HasPrefix(k, ".") {
			t.Errorf("emitted file %q for empty-dir proposal; should have skipped", k)
		}
	}
}

func TestEmit_MissingMemberRuleGoesToWarnings(t *testing.T) {
	ps, rules := fixtureProposalSetForEmit()
	// Pop one rule so a MemberRuleID can't be looked up.
	rules = rules[1:] // drops src.yaml#groups[0].rules[0]
	got, err := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"a", "b"}},
	})
	if err != nil {
		t.Fatalf("EmitProposals: %v", err)
	}
	foundWarning := false
	for _, w := range got.Warnings {
		if strings.Contains(w, "src.yaml#groups[0].rules[0]") && strings.Contains(w, "not found") {
			foundWarning = true
			break
		}
	}
	if !foundWarning {
		t.Errorf("expected warning about missing rule; got %v", got.Warnings)
	}
	// Other members still emit.
	if _, ok := got.Files["a/tenant-b.yaml"]; !ok {
		t.Error("missing rule shouldn't break the rest of the proposal")
	}
}

func TestEmit_NoVaryingLabels_OneExplanatoryWarning(t *testing.T) {
	// Pathological cluster: 2 structurally-identical rules with
	// nothing varying between them. Should emit _defaults.yaml +
	// PROPOSAL.md but NO tenant files, with a SINGLE explanatory
	// warning (not N spammy "looked for label \"\"" messages).
	rules := []parser.ParsedRule{
		{SourceRuleID: "x#0", Alert: "A", Expr: "m > 1", Labels: map[string]string{"sev": "warn"}, Dialect: parser.DialectProm},
		{SourceRuleID: "x#1", Alert: "A", Expr: "m > 1", Labels: map[string]string{"sev": "warn"}, Dialect: parser.DialectProm},
	}
	ps := &ProposalSet{
		Proposals: []ExtractionProposal{{
			MemberRuleIDs:    []string{"x#0", "x#1"},
			SharedLabels:     map[string]string{"sev": "warn"},
			VaryingLabelKeys: nil, // no varying labels — pathological case
			Dialect:          string(parser.DialectProm),
			Confidence:       ConfidenceHigh,
		}},
	}
	got, err := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"d"}},
	})
	if err != nil {
		t.Fatalf("EmitProposals: %v", err)
	}
	if len(got.Warnings) != 1 {
		t.Errorf("got %d warnings, want exactly 1 (no spam): %v", len(got.Warnings), got.Warnings)
	}
	if len(got.Warnings) > 0 && !strings.Contains(got.Warnings[0], "structurally identical") {
		t.Errorf("warning text = %q, want explanatory message", got.Warnings[0])
	}
	// _defaults.yaml + PROPOSAL.md should still emit; no tenant files.
	wantFiles := []string{"d/_defaults.yaml", "d/PROPOSAL.md"}
	if len(got.Files) != len(wantFiles) {
		t.Errorf("got %d files, want %d (no tenant files): %v", len(got.Files), len(wantFiles), keysOfFiles(got.Files))
	}
	for _, p := range wantFiles {
		if _, ok := got.Files[p]; !ok {
			t.Errorf("missing %q", p)
		}
	}
}

func TestEmit_RuleWithoutTenantLabel_GoesToWarnings(t *testing.T) {
	rules := []parser.ParsedRule{
		{
			SourceRuleID: "x#0",
			Alert:        "A",
			Expr:         "m > 1",
			Labels:       map[string]string{}, // no `tenant` label
			Dialect:      parser.DialectProm,
		},
		{
			SourceRuleID: "x#1",
			Alert:        "A",
			Expr:         "m > 1",
			Labels:       map[string]string{"tenant": "t1"},
			Dialect:      parser.DialectProm,
		},
	}
	ps := &ProposalSet{
		Proposals: []ExtractionProposal{
			{
				MemberRuleIDs:    []string{"x#0", "x#1"},
				VaryingLabelKeys: []string{"tenant"},
				Dialect:          string(parser.DialectProm),
				Confidence:       ConfidenceHigh,
			},
		},
	}
	got, err := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"d"}},
	})
	if err != nil {
		t.Fatalf("EmitProposals: %v", err)
	}
	if len(got.Warnings) != 1 {
		t.Fatalf("warnings = %d, want 1 (rule without tenant label)", len(got.Warnings))
	}
	// Only the second rule (with tenant) gets a tenant.yaml.
	if _, ok := got.Files["d/t1.yaml"]; !ok {
		t.Errorf("expected d/t1.yaml; files: %v", keysOfFiles(got.Files))
	}
}

// --- determinism + safe filenames ----------------------------------

func TestEmit_DeterministicOutput(t *testing.T) {
	ps, rules := fixtureProposalSetForEmit()
	r1, _ := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"a", "b"}, RootPrefix: "conf.d/"},
	})
	r2, _ := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"a", "b"}, RootPrefix: "conf.d/"},
	})
	for path, b1 := range r1.Files {
		b2, ok := r2.Files[path]
		if !ok {
			t.Errorf("file %q missing in r2", path)
			continue
		}
		if string(b1) != string(b2) {
			t.Errorf("file %q non-deterministic:\nrun 1:\n%s\nrun 2:\n%s", path, b1, b2)
		}
	}
	if len(r1.Files) != len(r2.Files) {
		t.Errorf("file count drift: %d vs %d", len(r1.Files), len(r2.Files))
	}
}

func TestEmit_SafeFilename_Slashes(t *testing.T) {
	rules := []parser.ParsedRule{
		{SourceRuleID: "x#0", Alert: "A", Expr: "m", Labels: map[string]string{"tenant": "team/payments/db"}, Dialect: parser.DialectProm},
		{SourceRuleID: "x#1", Alert: "A", Expr: "m", Labels: map[string]string{"tenant": "team/checkout/db"}, Dialect: parser.DialectProm},
	}
	ps := &ProposalSet{
		Proposals: []ExtractionProposal{{
			MemberRuleIDs:    []string{"x#0", "x#1"},
			VaryingLabelKeys: []string{"tenant"},
			Dialect:          string(parser.DialectProm),
			Confidence:       ConfidenceHigh,
		}},
	}
	got, err := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"d"}},
	})
	if err != nil {
		t.Fatalf("EmitProposals: %v", err)
	}
	// Slashes in tenant id should be replaced with `-`.
	if _, ok := got.Files["d/team-payments-db.yaml"]; !ok {
		t.Errorf("expected slashes sanitised → team-payments-db.yaml; files: %v", keysOfFiles(got.Files))
	}
}

func TestEmit_SafeFilename_HiddenFileGuard(t *testing.T) {
	// A leading `.` in the tenant id would create a hidden file.
	rules := []parser.ParsedRule{
		{SourceRuleID: "x#0", Alert: "A", Expr: "m", Labels: map[string]string{"tenant": ".hidden-tenant"}, Dialect: parser.DialectProm},
		{SourceRuleID: "x#1", Alert: "A", Expr: "m", Labels: map[string]string{"tenant": "ok"}, Dialect: parser.DialectProm},
	}
	ps := &ProposalSet{
		Proposals: []ExtractionProposal{{
			MemberRuleIDs:    []string{"x#0", "x#1"},
			VaryingLabelKeys: []string{"tenant"},
			Dialect:          string(parser.DialectProm),
			Confidence:       ConfidenceHigh,
		}},
	}
	got, _ := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"d"}},
	})
	for path := range got.Files {
		if strings.HasPrefix(path, "d/.") {
			t.Errorf("emitted hidden file %q", path)
		}
	}
}

// --- Markdown render -----------------------------------------------

func TestEmit_PROPOSAL_md_ContainsKeyFields(t *testing.T) {
	ps, rules := fixtureProposalSetForEmit()
	got, _ := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"a", "b"}},
	})
	md := string(got.Files["a/PROPOSAL.md"])
	wantSnippets := []string{
		"# Proposal 0",
		"high",                        // confidence
		"prom",                        // dialect
		"5m",                          // shared_for
		"src.yaml#groups[0].rules[0]", // member id
		"## Member rules",
		"## Shared structure",
		"intermediate", // scope-disclaimer footer
	}
	for _, snip := range wantSnippets {
		if !strings.Contains(md, snip) {
			t.Errorf("PROPOSAL.md missing snippet %q\n--- output ---\n%s", snip, md)
		}
	}
}

// --- PR-3 translated-emit path -------------------------------------

// fixtureTranslatableProposalSet builds a proposal where every member
// has a top-level numeric comparison + explicit metric_key label —
// i.e. the translator should produce TranslationOK for the cluster.
func fixtureTranslatableProposalSet() (*ProposalSet, []parser.ParsedRule) {
	rules := []parser.ParsedRule{
		{
			SourceRuleID: "src.yaml#g[0].r[0]",
			Alert:        "MySQLHighConnections",
			Expr:         `mysql_global_status_threads_connected{tenant="tenant-a"} > 800`,
			Labels: map[string]string{
				"tenant":     "tenant-a",
				"severity":   "warning",
				"metric_key": "mysql_connections",
			},
		},
		{
			SourceRuleID: "src.yaml#g[0].r[1]",
			Alert:        "MySQLHighConnections",
			Expr:         `mysql_global_status_threads_connected{tenant="tenant-b"} > 800`,
			Labels: map[string]string{
				"tenant":     "tenant-b",
				"severity":   "warning",
				"metric_key": "mysql_connections",
			},
		},
		{
			SourceRuleID: "src.yaml#g[0].r[2]",
			Alert:        "MySQLHighConnections",
			Expr:         `mysql_global_status_threads_connected{tenant="tenant-c"} > 1500`, // outlier
			Labels: map[string]string{
				"tenant":     "tenant-c",
				"severity":   "warning",
				"metric_key": "mysql_connections",
			},
		},
	}
	ps := &ProposalSet{
		Proposals: []ExtractionProposal{
			{
				MemberRuleIDs:    []string{"src.yaml#g[0].r[0]", "src.yaml#g[0].r[1]", "src.yaml#g[0].r[2]"},
				SharedLabels:     map[string]string{"severity": "warning", "metric_key": "mysql_connections"},
				VaryingLabelKeys: []string{"tenant"},
				Dialect:          string(parser.DialectProm),
				Confidence:       ConfidenceHigh,
				Reason:           "3 rules share the same comparison shape",
			},
		},
	}
	return ps, rules
}

func TestEmit_Translated_HappyPathProducesConfDShape(t *testing.T) {
	ps, rules := fixtureTranslatableProposalSet()
	got, err := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"db/"}},
		Translate:   true,
	})
	if err != nil {
		t.Fatalf("EmitProposals: %v", err)
	}
	defaultsBody := string(got.Files["db/_defaults.yaml"])
	// Conf.d shape: top-level `defaults:` with metric_key → numeric.
	if !strings.Contains(defaultsBody, "defaults:") {
		t.Errorf("_defaults.yaml should contain `defaults:` block; got:\n%s", defaultsBody)
	}
	if !strings.Contains(defaultsBody, "mysql_connections: 800") {
		t.Errorf("_defaults.yaml should carry median threshold 800; got:\n%s", defaultsBody)
	}
	// Header comment should call out PR-3 + dialect + status.
	if !strings.Contains(defaultsBody, "PR-3") || !strings.Contains(defaultsBody, "prom") {
		t.Errorf("_defaults.yaml header should mention PR-3 + dialect; got:\n%s", defaultsBody)
	}
	// tenant-c diverges (1500 vs default 800) → must have its own
	// override file. tenant-a/b match default → no file.
	if _, ok := got.Files["db/tenant-c.yaml"]; !ok {
		t.Errorf("expected db/tenant-c.yaml (override 1500); got files: %v", keysOfFiles(got.Files))
	}
	if _, ok := got.Files["db/tenant-a.yaml"]; ok {
		t.Errorf("tenant-a matches default → should NOT have override file; got %v", keysOfFiles(got.Files))
	}
	if _, ok := got.Files["db/tenant-b.yaml"]; ok {
		t.Errorf("tenant-b matches default → should NOT have override file")
	}
	// Override file must use the `tenants:` wrapper + string value.
	tcBody := string(got.Files["db/tenant-c.yaml"])
	if !strings.Contains(tcBody, "tenants:") {
		t.Errorf("tenant-c.yaml missing `tenants:` wrapper; got:\n%s", tcBody)
	}
	if !strings.Contains(tcBody, `mysql_connections: "1500"`) {
		t.Errorf("tenant-c.yaml should carry override 1500 as quoted string; got:\n%s", tcBody)
	}
}

func TestEmit_Translated_PROPOSAL_md_HasTranslationSummary(t *testing.T) {
	ps, rules := fixtureTranslatableProposalSet()
	got, _ := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"db/"}},
		Translate:   true,
	})
	md := string(got.Files["db/PROPOSAL.md"])
	wantSnippets := []string{
		"translated to conf.d", // header
		"## Translation summary",
		"mysql_connections", // metric_key
		"800",               // default threshold
		"tenant-c",          // override list
		"1500",              // override value
	}
	for _, snip := range wantSnippets {
		if !strings.Contains(md, snip) {
			t.Errorf("translated PROPOSAL.md missing snippet %q\n--- output ---\n%s", snip, md)
		}
	}
}

// Falling-back behaviour: a proposal whose translator returns
// Skipped should NOT poison the batch — emit goes back to the
// PR-2 intermediate format for that one and surfaces a warning.
func TestEmit_Translated_FallsBackToIntermediateOnSkip(t *testing.T) {
	// Use the original fixture which has rollup_rate (no top-level
	// comparison) — translator skips this cluster.
	ps, rules := fixtureProposalSetForEmit()
	got, err := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"dom-a", "dom-b"}},
		Translate:   true,
	})
	if err != nil {
		t.Fatalf("EmitProposals: %v", err)
	}
	// Proposal 0 (HighCPU > 0.85): comparison present → translated.
	defaults0 := string(got.Files["dom-a/_defaults.yaml"])
	if !strings.Contains(defaults0, "defaults:") {
		t.Errorf("dom-a/_defaults.yaml should be conf.d-shape (translated); got:\n%s", defaults0)
	}
	// Proposal 1 (rollup_rate, no comparison): translator skipped →
	// intermediate format, which has shared_expr_template.
	defaults1 := string(got.Files["dom-b/_defaults.yaml"])
	if !strings.Contains(defaults1, "shared_expr_template") {
		t.Errorf("dom-b/_defaults.yaml should be intermediate-shape (translator skipped); got:\n%s", defaults1)
	}
	// Warning explaining the fall-back must appear.
	fallBackWarn := false
	for _, w := range got.Warnings {
		if strings.Contains(w, "falling back to intermediate emission") {
			fallBackWarn = true
		}
	}
	if !fallBackWarn {
		t.Errorf("expected fall-back warning; got %v", got.Warnings)
	}
}

func TestEmit_Translated_DefaultsYAMLIsValidYAML(t *testing.T) {
	// The translated _defaults.yaml prepends a comment header to the
	// yaml.Marshal output. Verify the result still round-trips
	// through yaml.Unmarshal — header comments must not break parse.
	ps, rules := fixtureTranslatableProposalSet()
	got, _ := EmitProposals(EmissionInput{
		ProposalSet: ps,
		AllRules:    rules,
		Layout:      EmissionLayout{ProposalDirs: []string{"db/"}},
		Translate:   true,
	})
	body := got.Files["db/_defaults.yaml"]
	var parsed struct {
		Defaults map[string]float64 `yaml:"defaults"`
	}
	if err := yaml.Unmarshal(body, &parsed); err != nil {
		t.Fatalf("translated _defaults.yaml does not parse: %v\nbody:\n%s", err, string(body))
	}
	if v, ok := parsed.Defaults["mysql_connections"]; !ok || v != 800 {
		t.Errorf("parsed defaults = %v, want mysql_connections=800", parsed.Defaults)
	}
}

// formatThresholdString covers float→string conversion rules
// matter (avoid "80.000000" creeping into tenant.yaml).
func TestFormatThresholdString(t *testing.T) {
	cases := []struct {
		in   float64
		want string
	}{
		{80, "80"},
		{0, "0"},
		{0.85, "0.85"},
		{1500, "1500"},
		{-1.5, "-1.5"},
	}
	for _, c := range cases {
		got := formatThresholdString(c.in)
		if got != c.want {
			t.Errorf("formatThresholdString(%v) = %q, want %q", c.in, got, c.want)
		}
	}
}

// --- helper --------------------------------------------------------

func keysOfFiles(m map[string][]byte) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
