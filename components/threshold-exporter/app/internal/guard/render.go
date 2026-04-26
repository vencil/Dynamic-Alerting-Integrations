package guard

// Markdown renderer for posting GuardReports as PR comments.
//
// Same flavour as batchpr.Plan.Markdown — GitHub-Flavored Markdown
// so the GitHub Actions wrapper (PR-5) can post the output verbatim
// and reviewers see properly-rendered tables.
//
// Layout (designed for the "scroll-once and decide" reviewer
// experience):
//
//   ## Dangling Defaults Guard
//   ## Summary  (errors / warnings / passed counts)
//   ## Errors   (table — only when present)
//   ## Warnings (table — only when present)
//   ## All clear (single line — only when zero findings)
//
// We keep two separate severity tables rather than one combined
// table with a Severity column because GitHub PR comments truncate
// at ~65 KiB and reviewers tend to scan errors first; surfacing
// them in their own pre-pinned table makes the truncation point
// (warnings) the right thing to lose.

import (
	"fmt"
	"strings"
)

// Markdown returns the report body, ready for an action runner to
// drop into a PR comment unchanged.
//
// Stable across runs given the same Report (which is itself
// deterministic per CheckDefaultsImpact's contract).
func (r *GuardReport) Markdown() string {
	if r == nil {
		return "_(no guard report)_\n"
	}

	out := strings.Builder{}
	out.WriteString("## Dangling Defaults Guard\n\n")
	out.WriteString("### Summary\n\n")
	out.WriteString(fmt.Sprintf("- Tenants in scope: **%d**\n", r.Summary.TotalTenants))
	out.WriteString(fmt.Sprintf("- Errors: **%d**\n", r.Summary.Errors))
	out.WriteString(fmt.Sprintf("- Warnings: **%d**\n", r.Summary.Warnings))
	out.WriteString(fmt.Sprintf("- Tenants passing (zero errors): **%d**\n", r.Summary.PassedTenantCount))
	out.WriteString("\n")

	if len(r.Findings) == 0 {
		out.WriteString("✅ No findings — defaults change is safe to merge.\n")
		return out.String()
	}

	errorRows := filterFindings(r.Findings, SeverityError)
	warnRows := filterFindings(r.Findings, SeverityWarn)

	if len(errorRows) > 0 {
		out.WriteString("### Errors (block merge)\n\n")
		writeFindingsTable(&out, errorRows)
		out.WriteString("\n")
	}
	if len(warnRows) > 0 {
		out.WriteString("### Warnings (informational)\n\n")
		writeFindingsTable(&out, warnRows)
		out.WriteString("\n")
	}
	return out.String()
}

func filterFindings(in []Finding, sev Severity) []Finding {
	var out []Finding
	for _, f := range in {
		if f.Severity == sev {
			out = append(out, f)
		}
	}
	return out
}

func writeFindingsTable(out *strings.Builder, findings []Finding) {
	out.WriteString("| Tenant | Field | Kind | Message |\n")
	out.WriteString("|--------|-------|------|---------|\n")
	for _, f := range findings {
		out.WriteString(fmt.Sprintf("| %s | %s | %s | %s |\n",
			emptyOrDash(f.TenantID),
			emptyOrDash(f.Field),
			f.Kind,
			escapeMarkdownTableCell(f.Message),
		))
	}
}

func emptyOrDash(s string) string {
	if s == "" {
		return "—"
	}
	return s
}

// escapeMarkdownTableCell replaces characters that would otherwise
// break a Markdown table row. The two real problems are:
//   - Pipe (`|`): table column separator. Escape with `\|`.
//   - Newline: silently truncates the rest of the row. Replace
//     with a literal `\n` so the message stays readable.
//
// We don't go further (HTML escaping etc.) because GitHub's
// Markdown renderer is forgiving with everything else.
func escapeMarkdownTableCell(s string) string {
	s = strings.ReplaceAll(s, "|", `\|`)
	s = strings.ReplaceAll(s, "\n", `\n`)
	return s
}
