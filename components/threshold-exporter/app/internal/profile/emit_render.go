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
	out.WriteString(fmt.Sprintf("# Proposal %d\n\n", propIdx))
	out.WriteString(fmt.Sprintf("**Confidence**: %s  \n", prop.Confidence))
	out.WriteString(fmt.Sprintf("**Dialect**: %s  \n", prop.Dialect))
	out.WriteString(fmt.Sprintf("**Members**: %d rules  \n", len(prop.MemberRuleIDs)))
	if tenantKey != "" {
		out.WriteString(fmt.Sprintf("**Tenant key**: `%s`  \n", tenantKey))
	} else {
		out.WriteString("**Tenant key**: _(none — proposal has no varying labels)_  \n")
	}
	out.WriteString(fmt.Sprintf("**Estimated YAML lines saved**: %d  \n\n", prop.EstimatedYAMLLineSavings))

	out.WriteString("## Reason\n\n")
	out.WriteString(prop.Reason)
	if !strings.HasSuffix(prop.Reason, "\n") {
		out.WriteString("\n")
	}
	out.WriteString("\n")

	out.WriteString("## Member rules\n\n")
	for _, rid := range prop.MemberRuleIDs {
		out.WriteString(fmt.Sprintf("- `%s`\n", rid))
	}
	out.WriteString("\n")

	out.WriteString("## Shared structure\n\n")
	out.WriteString(fmt.Sprintf("- Expression template (normalised): `%s`\n", prop.SharedExprTemplate))
	if prop.SharedFor != "" {
		out.WriteString(fmt.Sprintf("- For: `%s`\n", prop.SharedFor))
	}
	if len(prop.SharedLabels) > 0 {
		out.WriteString("- Shared labels:\n")
		for _, k := range sortedKeys(prop.SharedLabels) {
			out.WriteString(fmt.Sprintf("  - `%s`: `%s`\n", k, prop.SharedLabels[k]))
		}
	}
	if len(prop.VaryingLabelKeys) > 0 {
		out.WriteString(fmt.Sprintf("- Varying label keys: %s\n", strings.Join(quoteAll(prop.VaryingLabelKeys), ", ")))
	}

	out.WriteString("\n---\n\n")
	out.WriteString("_PR-2 emission: this artifact tree is intermediate. PR-3 will translate it into the final ADR-018 conf.d/ shape._\n")
	return out.String()
}

// renderTranslatedProposalMarkdown is the PR-3 sibling of
// renderProposalMarkdown — same overall shape, but adds a
// "Translation summary" section that surfaces metric_key, default
// threshold, severity, and per-tenant overrides so a reviewer can
// see the conf.d-shape decisions without opening the YAML files.
func renderTranslatedProposalMarkdown(propIdx int, prop ExtractionProposal, translation *ProposalTranslation, tenantKey string) string {
	out := strings.Builder{}
	out.WriteString(fmt.Sprintf("# Proposal %d (translated to conf.d)\n\n", propIdx))
	out.WriteString(fmt.Sprintf("**Confidence**: %s  \n", prop.Confidence))
	out.WriteString(fmt.Sprintf("**Dialect**: %s  \n", prop.Dialect))
	out.WriteString(fmt.Sprintf("**Translation status**: %s  \n", translation.Status))
	out.WriteString(fmt.Sprintf("**Members**: %d rules  \n", len(prop.MemberRuleIDs)))
	if tenantKey != "" {
		out.WriteString(fmt.Sprintf("**Tenant key**: `%s`  \n", tenantKey))
	}
	out.WriteString("\n")

	out.WriteString("## Translation summary\n\n")
	out.WriteString(fmt.Sprintf("- Metric key: `%s`\n", translation.MetricKey))
	out.WriteString(fmt.Sprintf("- Default threshold (cluster median): `%g`\n", translation.DefaultThreshold))
	if translation.Operator != "" {
		out.WriteString(fmt.Sprintf("- Comparison operator: `%s`\n", translation.Operator))
	}
	if translation.Severity != "" {
		out.WriteString(fmt.Sprintf("- Severity: `%s`\n", translation.Severity))
	}
	if len(translation.PerTenantOverrides) > 0 {
		out.WriteString("- Per-tenant overrides (only tenants whose value differs from default):\n")
		for _, k := range sortedTenantKeys(translation.PerTenantOverrides) {
			out.WriteString(fmt.Sprintf("  - `%s` → `%g`\n", k, translation.PerTenantOverrides[k]))
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
			out.WriteString(fmt.Sprintf("- ✅ `%s` — threshold=`%g`\n", m.SourceRuleID, m.Threshold))
		case TranslationPartial:
			out.WriteString(fmt.Sprintf("- ⚠️ `%s` — threshold=`%g` (partial: %s)\n", m.SourceRuleID, m.Threshold, strings.Join(m.Warnings, "; ")))
		case TranslationSkipped:
			out.WriteString(fmt.Sprintf("- ⏭️ `%s` — skipped (%s)\n", m.SourceRuleID, m.SkipReason))
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
	out.WriteString("_runtime ResolveAt path consumes them via ADR-018 inheritance._\n")
	return out.String()
}

// sortedTenantKeys returns map keys sorted alphabetically (helper
// for renderTranslatedProposalMarkdown so the per-tenant override
// list is deterministic).
