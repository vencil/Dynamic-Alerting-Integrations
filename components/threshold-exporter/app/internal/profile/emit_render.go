package profile

// Markdown rendering layer for human-readable PROPOSAL.md artifacts.
// Split out of emit.go in v2.8.0 PR-6.
//
// Concerns covered here:
//   - renderProposalMarkdown — pre-translator artifact summary
//     (member rules, dialect mix, varying labels)
//   - renderTranslatedProposalMarkdown — post-translator C-9 PR-3
//     summary including the structured-key shape that landed in
//     `defaults:` / `tenants:` blocks
//
// Pure functions; no IO, no yaml.v3 dep — just `fmt` + `strings`.
// Outputs are stable string trees suitable for git commit / review.

import (
	"fmt"
	"strings"
)

func renderProposalMarkdown(propIdx int, prop ExtractionProposal, tenantKey string) string {
	out := strings.Builder{}
	fmt.Fprintf(&out, "# Proposal %d\n\n", propIdx)
	fmt.Fprintf(&out, "**Confidence**: %s  \n", prop.Confidence)
	fmt.Fprintf(&out, "**Dialect**: %s  \n", prop.Dialect)
	fmt.Fprintf(&out, "**Members**: %d rules  \n", len(prop.MemberRuleIDs))
	if tenantKey != "" {
		fmt.Fprintf(&out, "**Tenant key**: `%s`  \n", tenantKey)
	} else {
		out.WriteString("**Tenant key**: _(none — proposal has no varying labels)_  \n")
	}
	fmt.Fprintf(&out, "**Estimated YAML lines saved**: %d  \n\n", prop.EstimatedYAMLLineSavings)

	out.WriteString("## Reason\n\n")
	out.WriteString(prop.Reason)
	if !strings.HasSuffix(prop.Reason, "\n") {
		out.WriteString("\n")
	}
	out.WriteString("\n")

	out.WriteString("## Member rules\n\n")
	for _, rid := range prop.MemberRuleIDs {
		fmt.Fprintf(&out, "- `%s`\n", rid)
	}
	out.WriteString("\n")

	out.WriteString("## Shared structure\n\n")
	fmt.Fprintf(&out, "- Expression template (normalised): `%s`\n", prop.SharedExprTemplate)
	if prop.SharedFor != "" {
		fmt.Fprintf(&out, "- For: `%s`\n", prop.SharedFor)
	}
	if len(prop.SharedLabels) > 0 {
		out.WriteString("- Shared labels:\n")
		for _, k := range sortedKeys(prop.SharedLabels) {
			fmt.Fprintf(&out, "  - `%s`: `%s`\n", k, prop.SharedLabels[k])
		}
	}
	if len(prop.VaryingLabelKeys) > 0 {
		fmt.Fprintf(&out, "- Varying label keys: %s\n", strings.Join(quoteAll(prop.VaryingLabelKeys), ", "))
	}

	out.WriteString("\n---\n\n")
	out.WriteString("_PR-2 emission: this artifact tree is intermediate. PR-3 will translate it into the final ADR-017 conf.d/ shape._\n")
	return out.String()
}

// renderTranslatedProposalMarkdown is the PR-3 sibling of
// renderProposalMarkdown — same overall shape, but adds a
// "Translation summary" section that surfaces metric_key, default
// threshold, severity, and per-tenant overrides so a reviewer can
// see the conf.d-shape decisions without opening the YAML files.
func renderTranslatedProposalMarkdown(propIdx int, prop ExtractionProposal, translation *ProposalTranslation, tenantKey string) string {
	out := strings.Builder{}
	fmt.Fprintf(&out, "# Proposal %d (translated to conf.d)\n\n", propIdx)
	fmt.Fprintf(&out, "**Confidence**: %s  \n", prop.Confidence)
	fmt.Fprintf(&out, "**Dialect**: %s  \n", prop.Dialect)
	fmt.Fprintf(&out, "**Translation status**: %s  \n", translation.Status)
	fmt.Fprintf(&out, "**Members**: %d rules  \n", len(prop.MemberRuleIDs))
	if tenantKey != "" {
		fmt.Fprintf(&out, "**Tenant key**: `%s`  \n", tenantKey)
	}
	out.WriteString("\n")

	out.WriteString("## Translation summary\n\n")
	fmt.Fprintf(&out, "- Metric key: `%s`\n", translation.MetricKey)
	fmt.Fprintf(&out, "- Default threshold (cluster median): `%g`\n", translation.DefaultThreshold)
	if translation.Operator != "" {
		fmt.Fprintf(&out, "- Comparison operator: `%s`\n", translation.Operator)
	}
	if translation.Severity != "" {
		fmt.Fprintf(&out, "- Severity: `%s`\n", translation.Severity)
	}
	if len(translation.PerTenantOverrides) > 0 {
		out.WriteString("- Per-tenant overrides (only tenants whose value differs from default):\n")
		for _, k := range sortedTenantKeys(translation.PerTenantOverrides) {
			fmt.Fprintf(&out, "  - `%s` → `%g`\n", k, translation.PerTenantOverrides[k])
		}
	}
	out.WriteString("\n")

	if len(translation.Warnings) > 0 {
		out.WriteString("## Translator warnings\n\n")
		for _, w := range translation.Warnings {
			out.WriteString("- " + w + "\n")
		}
		out.WriteString("\n")
	}

	out.WriteString("## Member rules\n\n")
	for _, m := range translation.MemberStatuses {
		switch m.Status {
		case TranslationOK:
			fmt.Fprintf(&out, "- ✅ `%s` — threshold=`%g`\n", m.SourceRuleID, m.Threshold)
		case TranslationPartial:
			fmt.Fprintf(&out, "- ⚠️ `%s` — threshold=`%g` (partial: %s)\n", m.SourceRuleID, m.Threshold, strings.Join(m.Warnings, "; "))
		case TranslationSkipped:
			fmt.Fprintf(&out, "- ⏭️ `%s` — skipped (%s)\n", m.SourceRuleID, m.SkipReason)
		}
	}
	out.WriteString("\n")

	out.WriteString("## Reason\n\n")
	out.WriteString(prop.Reason)
	if !strings.HasSuffix(prop.Reason, "\n") {
		out.WriteString("\n")
	}
	out.WriteString("\n")

	out.WriteString("---\n\n")
	out.WriteString("_PR-3 emission: this proposal is conf.d-ready (deepMerge-compatible). Review_  \n")
	out.WriteString("_`_defaults.yaml` and per-tenant `<id>.yaml` then commit; the threshold-exporter_  \n")
	out.WriteString("_runtime ResolveAt path consumes them via ADR-017 inheritance._\n")
	return out.String()
}

// sortedTenantKeys returns map keys sorted alphabetically (helper
// for renderTranslatedProposalMarkdown so the per-tenant override
// list is deterministic).
