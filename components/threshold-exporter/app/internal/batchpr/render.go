package batchpr

// Markdown rendering for human review of a Plan.
//
// Two consumers in mind:
//   - The future CLI subcommand `da-tools batch-pr plan` prints
//     this to stdout so a maintainer can sign off before
//     `batch-pr apply` (PR-2) opens any PRs.
//   - The C-3 / C-4 UI surface embeds this in the "review before
//     apply" pane.
//
// The output is GitHub-Flavored Markdown — same flavour the future
// PR descriptions will use — so a reviewer copy-pasting a planned
// PR description into a GitHub draft sees identical rendering.

import (
	"fmt"
	"strings"
)

// Markdown returns a human-readable summary of the Plan, suitable
// for printing to a CLI or rendering in a UI preview pane.
//
// Stable across runs given the same Plan (the underlying Plan is
// already deterministic per BuildPlan's contract).
func (p *Plan) Markdown() string {
	if p == nil {
		return "_(empty plan)_\n"
	}

	out := strings.Builder{}
	out.WriteString("# Batch PR Plan\n\n")

	// Summary block — one-line counts so a reviewer can sanity-check
	// scale before reading the per-item list.
	out.WriteString("## Summary\n\n")
	out.WriteString(fmt.Sprintf("- Total proposals: **%d**\n", p.Summary.TotalProposals))
	out.WriteString(fmt.Sprintf("- Base PRs: **%d**\n", p.Summary.BasePRCount))
	out.WriteString(fmt.Sprintf("- Tenant PRs: **%d**\n", p.Summary.TenantPRCount))
	out.WriteString(fmt.Sprintf("- Total tenants: **%d**\n", p.Summary.TotalTenants))
	out.WriteString(fmt.Sprintf("- Chunk strategy: `%s`\n", p.Summary.ChunkBy))
	out.WriteString(fmt.Sprintf("- Effective chunk size: %d\n", p.Summary.EffectiveChunkSize))
	out.WriteString("\n")

	if len(p.Warnings) > 0 {
		out.WriteString("## Warnings\n\n")
		for _, w := range p.Warnings {
			out.WriteString(fmt.Sprintf("- ⚠ %s\n", w))
		}
		out.WriteString("\n")
	}

	// Per-item table — one row per planned PR. Apply tooling will
	// walk this same order.
	out.WriteString("## Items (apply order)\n\n")
	out.WriteString("| # | Kind | Title | Blocked-by | Tenants |\n")
	out.WriteString("|---|------|-------|------------|---------|\n")
	for i, item := range p.Items {
		blocked := item.BlockedBy
		if blocked == "" {
			blocked = "—"
		}
		tenantCol := "—"
		if item.Kind == PlanItemTenant {
			tenantCol = fmt.Sprintf("%d", len(item.TenantIDs))
		}
		out.WriteString(fmt.Sprintf("| %d | %s | %s | %s | %s |\n",
			i+1, item.Kind, item.Title, blocked, tenantCol))
	}
	out.WriteString("\n")

	// Per-item details — full PR description for each. Apply
	// tooling pastes these into the actual PR body.
	out.WriteString("## Item details\n\n")
	for i, item := range p.Items {
		out.WriteString(fmt.Sprintf("### %d. %s\n\n", i+1, item.Title))
		out.WriteString(item.Description)
		if !strings.HasSuffix(item.Description, "\n") {
			out.WriteString("\n")
		}
		out.WriteString("\n")
	}
	return out.String()
}
