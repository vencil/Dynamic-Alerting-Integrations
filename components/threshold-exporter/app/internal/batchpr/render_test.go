package batchpr

import (
	"strings"
	"testing"
)

func TestPlanMarkdown_NilPlanSafe(t *testing.T) {
	var p *Plan
	got := p.Markdown()
	if got == "" {
		t.Errorf("Markdown() of nil plan returned empty; want a recognisable placeholder")
	}
}

func TestPlanMarkdown_ContainsAllItems(t *testing.T) {
	plan, err := BuildPlan(PlanInput{
		Proposals: []ProposalRef{
			{MemberTenantIDs: []string{"alpha", "beta"}, Dialect: "prom",
				SharedFor: "5m", SharedLabels: map[string]string{"severity": "warning"}},
		},
		TenantDirs: map[string]string{
			"alpha": "dom-a/r1/alpha",
			"beta":  "dom-b/r1/beta",
		},
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	md := plan.Markdown()

	wantSnippets := []string{
		"# Batch PR Plan",
		"## Summary",
		"## Items (apply order)",
		"## Item details",
		"[Base Infrastructure]",
		"[chunk 1/2]",
		"[chunk 2/2]",
		"alpha", // tenant id appears in a per-item description
		"beta",
		"base_infra", // kind enum literal in summary table
		"prom",       // dialect mentioned in base item table
		"Blocked by", // tenant items reference base PR
	}
	for _, snip := range wantSnippets {
		if !strings.Contains(md, snip) {
			t.Errorf("Markdown missing snippet %q\n--- output ---\n%s", snip, md)
		}
	}
}

func TestPlanMarkdown_WarningsRenderedWhenPresent(t *testing.T) {
	plan, err := BuildPlan(PlanInput{
		Proposals: []ProposalRef{
			{MemberTenantIDs: []string{"known", "missing-dir"}, Dialect: "prom"},
		},
		TenantDirs: map[string]string{
			"known": "dom-a/r1/known",
		},
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	md := plan.Markdown()
	if !strings.Contains(md, "## Warnings") {
		t.Errorf("Markdown missing Warnings section; output:\n%s", md)
	}
	if !strings.Contains(md, "missing-dir") {
		t.Errorf("warning content not rendered:\n%s", md)
	}
}

func TestPlanMarkdown_NoWarningsSectionWhenClean(t *testing.T) {
	plan, err := BuildPlan(PlanInput{
		Proposals: []ProposalRef{
			{MemberTenantIDs: []string{"t1"}, Dialect: "prom"},
		},
		TenantDirs: map[string]string{"t1": "dom-a/r1/t1"},
	})
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	md := plan.Markdown()
	if strings.Contains(md, "## Warnings") {
		t.Errorf("Markdown rendered Warnings section despite none present:\n%s", md)
	}
}
