package profile

// Emission — turn ProposalSet (from PR-1's BuildProposals) plus the
// original ParsedRule corpus into a directory tree of YAML artifacts
// ready for human review and (eventually) Git commit.
//
// SCOPE NOTE — INTERMEDIATE FORMAT, NOT YET conf.d-READY
// -------------------------------------------------------
// PR-2 emits a "structured proposal artifact" tree, not the final
// conf.d/ shape that ADR-018's deepMerge engine consumes. The gap:
//
//   - PromRule expressions look like
//     `avg(rate(node_cpu_seconds_total{tenant="t"}[5m])) > 0.85`.
//     The threshold-exporter conf.d format expects structured
//     scalar fields like `cpu_avg_rate_5m: 0.85`. Bridging the two
//     needs a PromRule→threshold translator that can extract
//     metric_name + threshold_value from the AST.
//
//   - That translator is its own design problem (PR-3 + ADR-019).
//     PR-2 ships the artifact emission layer that the translator
//     will plug into without re-doing the emission code.
//
// The artifacts ARE useful pre-translator:
//
//   - Humans can review proposals before they become real conf.d
//     files (catch clustering mistakes, mis-attributed tenants,
//     dialect mix-ups).
//   - C-10 PR-2 apply mode can open Draft PRs containing the
//     artifacts so reviewers see the proposed structure before any
//     conf.d conversion lands.
//   - Future PR-3 has a stable input format to bolt the translator
//     onto — the artifacts already carry shared/varying split,
//     dialect, provenance, and member rule IDs.
//
// Emission contract (PR-2):
//
//   For each proposal i, emits files under
//   `<RootPrefix>/<ProposalDirs[i]>/`:
//
//     _defaults.yaml        — shared structure (template, labels,
//                             for, dialect, provenance)
//     <tenant_id>.yaml      — per member-tenant variation
//                             (one file per MemberRuleID)
//     PROPOSAL.md           — human-readable summary
//
// Pure function — emits `map[path][]byte`. Caller writes to disk
// (or stages in git, or pipes to stdout). Same pattern as the C-7a
// InMemoryConfigSource and C-10 batchpr.Plan: keep IO at the edges.

import (
	"errors"
	"fmt"
	"path"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/internal/parser"
)

// EmissionInput is the contract for a single emit run.
type EmissionInput struct {
	// ProposalSet from BuildProposals (C-9 PR-1). Required; len 0
	// returns an error so callers don't accidentally emit empty
	// trees.
	ProposalSet *ProposalSet `json:"proposal_set"`

	// AllRules is the full ParsedRule corpus (parser output). The
	// emitter looks up each MemberRuleID against this slice to
	// build the per-tenant override files. Required.
	AllRules []parser.ParsedRule `json:"all_rules"`

	// Layout pins each proposal to a target directory.
	Layout EmissionLayout `json:"layout"`

	// Translate flips each proposal's emission shape from the PR-2
	// intermediate format to the PR-3 conf.d-ready format
	// (`defaults: {<metric_key>: <threshold>}` /
	// `tenants: {<id>: {<metric_key>: "<value>"}}`) when the
	// translator can pull a numeric threshold out of every member
	// rule.
	//
	// Per-proposal dispatch (NOT batch all-or-nothing): each cluster
	// runs TranslateProposal independently. TranslationOK / Partial
	// → conf.d-shape. TranslationSkipped → fall back to PR-2
	// intermediate. A mixed easy/hard customer corpus still gets
	// maximum value — translatable clusters land conf.d-ready,
	// others surface for human review without sinking the batch.
	//
	// false (default) preserves PR-2 backwards-compat for tooling
	// already integrated against intermediate format. The cross-
	// cutting "Profile-as-Directory-Default" principle that the
	// conf.d-shape emission realises is documented in ADR-019;
	// translator heuristics + cluster aggregation rules + status
	// semantics live in the `translate.go` package header.
	Translate bool `json:"translate,omitempty"`
}

// EmissionLayout maps proposals to target directories. Caller-
// supplied; PR-2 doesn't infer directory structure (that's PR-3's
// ADR-019 job).
type EmissionLayout struct {
	// ProposalDirs[i] is the directory (relative to RootPrefix) the
	// emitter will write proposal i's artifacts into. Length must
	// equal len(ProposalSet.Proposals); a mismatch is an error.
	//
	// Empty entries are permitted but go to Warnings — the emitter
	// won't write anything for that proposal but won't fail the
	// whole batch either, so callers can iteratively refine
	// placement.
	ProposalDirs []string `json:"proposal_dirs"`

	// RootPrefix is prepended to every emitted file path
	// (e.g. "conf.d/"). Empty is allowed — files emit at the
	// proposal-dir level. Trailing slashes are normalised away.
	RootPrefix string `json:"root_prefix,omitempty"`
}

// EmissionOutput is the result of one emit run. The Files map keys
// are POSIX-style paths suitable for git commit; values are the
// YAML / Markdown bytes ready to write.
type EmissionOutput struct {
	// Files maps relative path → file contents. Iteration order is
	// non-deterministic (Go map), but contents are stable across
	// runs given the same input — TestEmit_DeterministicOutput
	// asserts byte-identical bytes for two runs.
	Files map[string][]byte `json:"files"`

	// Warnings collects non-fatal issues: empty ProposalDirs entry,
	// MemberRuleID not found in AllRules, etc. The emit still
	// returns a partial Files map so callers can iterate.
	Warnings []string `json:"warnings,omitempty"`
}

// EmitProposals runs the emission and returns the file tree.
//
// Errors:
//   - input.ProposalSet == nil                  → fmt error
//   - len(input.ProposalSet.Proposals) == 0     → fmt error
//   - len(input.Layout.ProposalDirs) != len(input.ProposalSet.Proposals)
//     → fmt error (caller almost always wants to be told the
//     layout is mis-sized rather than have a partial emission)
//
// Otherwise returns the EmissionOutput with whatever it could
// build. Per-proposal issues (empty layout entry, missing rule
// in AllRules) become Warnings, not errors.
func EmitProposals(input EmissionInput) (*EmissionOutput, error) {
	if input.ProposalSet == nil {
		return nil, fmt.Errorf("emit: ProposalSet is nil")
	}
	props := input.ProposalSet.Proposals
	if len(props) == 0 {
		return nil, fmt.Errorf("emit: ProposalSet has no proposals")
	}
	if len(input.Layout.ProposalDirs) != len(props) {
		return nil, fmt.Errorf("emit: Layout.ProposalDirs length (%d) does not match Proposals length (%d)",
			len(input.Layout.ProposalDirs), len(props))
	}

	// Index AllRules by SourceRuleID for per-proposal lookup.
	ruleIndex := make(map[string]parser.ParsedRule, len(input.AllRules))
	for _, r := range input.AllRules {
		ruleIndex[r.SourceRuleID] = r
	}

	rootPrefix := strings.TrimRight(input.Layout.RootPrefix, "/")
	out := &EmissionOutput{
		Files: make(map[string][]byte),
	}

	for i, prop := range props {
		dir := input.Layout.ProposalDirs[i]
		if dir == "" {
			out.Warnings = append(out.Warnings,
				fmt.Sprintf("proposal[%d] (members=%d): empty Layout.ProposalDirs entry; skipped from emission",
					i, len(prop.MemberRuleIDs)))
			continue
		}
		warnings := emitOneProposal(out.Files, rootPrefix, dir, prop, ruleIndex, i, input.Translate)
		out.Warnings = append(out.Warnings, warnings...)
	}
	return out, nil
}

// emitOneProposal writes the three artifact files for proposal i
// into the shared Files map. Returns per-proposal warnings.
//
// When `translate` is true the emitter first runs TranslateProposal
// on the cluster; on success it emits the conf.d-ready shape
// (`defaults: {key: scalar}` + `tenants: {id: {key: "string"}}`).
// On TranslationSkipped it falls back to the PR-2 intermediate
// shape so a single un-translatable proposal doesn't sink the
// whole batch.
func emitOneProposal(
	files map[string][]byte,
	rootPrefix, dir string,
	prop ExtractionProposal,
	ruleIndex map[string]parser.ParsedRule,
	propIdx int,
	translate bool,
) []string {
	var warnings []string
	pathFor := func(name string) string {
		return joinClean(rootPrefix, dir, name)
	}

	tenantKey := pickTenantLabelKey(prop)

	// PR-3 translated path: when caller opts in AND the cluster
	// produces a TranslationOK / Partial result, emit conf.d-ready
	// YAML. Skip-status (no member translatable) falls through to
	// the intermediate path so the caller's batch still gets
	// useful artifacts.
	if translate {
		members := membersForProposal(prop, ruleIndex)
		translation, terr := TranslateProposal(prop, members, tenantKey)
		if terr != nil {
			warnings = append(warnings, fmt.Sprintf(
				"proposal[%d]: translator hard-error: %v (falling back to intermediate emission)",
				propIdx, terr))
		} else if translation.Status != TranslationSkipped {
			tw := emitTranslatedProposal(files, pathFor, prop, translation, ruleIndex, tenantKey, propIdx)
			warnings = append(warnings, tw...)
			return warnings
		} else {
			warnings = append(warnings, fmt.Sprintf(
				"proposal[%d]: TranslateProposal returned skipped (no translatable members); falling back to intermediate emission",
				propIdx))
			for _, w := range translation.Warnings {
				warnings = append(warnings,
					fmt.Sprintf("proposal[%d]: %s", propIdx, w))
			}
		}
	}

	// 1. _defaults.yaml — shared structure across all members.
	defaultsBytes, err := marshalDefaults(prop)
	if err != nil {
		warnings = append(warnings, fmt.Sprintf(
			"proposal[%d]: failed to marshal _defaults.yaml: %v", propIdx, err))
	} else {
		files[pathFor("_defaults.yaml")] = defaultsBytes
	}

	// 2. Per-tenant override files. One per MemberRuleID; the
	// tenant identity is taken from the rule's Labels (heuristic:
	// the first label key whose value differs across members is
	// the tenant key, picked from VaryingLabelKeys).
	//
	// If the proposal has NO varying labels (rare but possible: a
	// cluster of structurally identical rules), there's no
	// meaningful per-tenant axis to emit on. Skip the per-tenant
	// loop entirely with one explanatory warning rather than
	// spamming N "looked for label \"\"" messages.
	if tenantKey == "" {
		warnings = append(warnings, fmt.Sprintf(
			"proposal[%d]: no varying label keys — cluster members are structurally identical; skipped per-tenant file emission",
			propIdx))
	} else {
		for _, rid := range prop.MemberRuleIDs {
			rule, ok := ruleIndex[rid]
			if !ok {
				warnings = append(warnings, fmt.Sprintf(
					"proposal[%d]: MemberRuleID %q not found in AllRules; skipped tenant file",
					propIdx, rid))
				continue
			}
			tenantID := tenantIDForRule(rule, tenantKey)
			if tenantID == "" {
				warnings = append(warnings, fmt.Sprintf(
					"proposal[%d]: rule %q has no value for tenant key %q; skipped tenant file",
					propIdx, rid, tenantKey))
				continue
			}
			tenantBytes, err := marshalTenantOverride(tenantID, rule, prop)
			if err != nil {
				warnings = append(warnings, fmt.Sprintf(
					"proposal[%d]: failed to marshal tenant %q file: %v", propIdx, tenantID, err))
				continue
			}
			files[pathFor(safeFilename(tenantID)+".yaml")] = tenantBytes
		}
	}

	// 3. PROPOSAL.md — human-readable summary.
	files[pathFor("PROPOSAL.md")] = []byte(renderProposalMarkdown(propIdx, prop, tenantKey))
	return warnings
}

// membersForProposal extracts the ParsedRule slice corresponding to
// a proposal's MemberRuleIDs (preserving order so cluster
// translation behaviour is deterministic). Missing IDs are dropped
// silently here; the caller surfaces them as warnings via the
// regular emission path.
func membersForProposal(prop ExtractionProposal, ruleIndex map[string]parser.ParsedRule) []parser.ParsedRule {
	out := make([]parser.ParsedRule, 0, len(prop.MemberRuleIDs))
	for _, rid := range prop.MemberRuleIDs {
		if r, ok := ruleIndex[rid]; ok {
			out = append(out, r)
		}
	}
	return out
}

// emitTranslatedProposal writes the conf.d-ready artifact tree:
//
//   - `_defaults.yaml` carries `defaults: {<metric_key>: <threshold>}`
//     plus a comment header with provenance / dialect / translator
//     warnings so reviewers see the soft spots.
//   - per-tenant `<id>.yaml` carries `tenants: {<id>: {<metric_key>:
//     "<value>"}}` only when the tenant's threshold differs from the
//     cluster default — keeping tenant.yaml minimal per ADR-019's
//     "Profile-as-Directory-Default" goal.
//   - `PROPOSAL.md` is unchanged from the intermediate path; it
//     summarises the cluster for reviewers.
func emitTranslatedProposal(
	files map[string][]byte,
	pathFor func(string) string,
	prop ExtractionProposal,
	translation *ProposalTranslation,
	ruleIndex map[string]parser.ParsedRule,
	tenantKey string,
	propIdx int,
) []string {
	var warnings []string

	defaultsDoc := map[string]any{
		"defaults": map[string]any{
			translation.MetricKey: translation.DefaultThreshold,
		},
	}
	if defaultsBytes, err := yamlMarshalCanonical(defaultsDoc); err == nil {
		files[pathFor("_defaults.yaml")] = withTranslatedHeader(defaultsBytes, prop, translation)
	} else {
		warnings = append(warnings, fmt.Sprintf(
			"proposal[%d]: failed to marshal translated _defaults.yaml: %v", propIdx, err))
	}

	if tenantKey == "" {
		warnings = append(warnings, fmt.Sprintf(
			"proposal[%d]: translated emit — no varying label keys; skipped per-tenant files (override values land in PROPOSAL.md only)",
			propIdx))
	} else {
		for _, rid := range prop.MemberRuleIDs {
			rule, ok := ruleIndex[rid]
			if !ok {
				warnings = append(warnings, fmt.Sprintf(
					"proposal[%d]: translated emit — MemberRuleID %q not found in AllRules; skipped tenant file",
					propIdx, rid))
				continue
			}
			tenantID := tenantIDForRule(rule, tenantKey)
			if tenantID == "" {
				warnings = append(warnings, fmt.Sprintf(
					"proposal[%d]: translated emit — rule %q has no value for tenant key %q; skipped tenant file",
					propIdx, rid, tenantKey))
				continue
			}
			override, hasOverride := translation.PerTenantOverrides[tenantID]
			if !hasOverride {
				// Tenant matches the default → no tenant.yaml needed
				// (deepMerge falls through to _defaults.yaml). This is
				// the GitOps anti-pattern fix that ADR-019 §1
				// motivated.
				continue
			}
			tenantDoc := map[string]any{
				"tenants": map[string]any{
					tenantID: map[string]any{
						// Threshold-exporter tenant overrides are
						// strings (supports "value:severity" suffix
						// per config_resolve.go::ResolveAt). Translator
						// emits the bare numeric string; severity
						// override would need explicit translator
						// extension and is ADR-019 §non-goals for PR-3.
						translation.MetricKey: formatThresholdString(override),
					},
				},
			}
			if tenantBytes, err := yamlMarshalCanonical(tenantDoc); err == nil {
				files[pathFor(safeFilename(tenantID)+".yaml")] = tenantBytes
			} else {
				warnings = append(warnings, fmt.Sprintf(
					"proposal[%d]: translated emit — failed to marshal tenant %q: %v",
					propIdx, tenantID, err))
			}
		}
	}

	files[pathFor("PROPOSAL.md")] = []byte(renderTranslatedProposalMarkdown(propIdx, prop, translation, tenantKey))
	return warnings
}

// withTranslatedHeader prepends a YAML comment block to the
// _defaults.yaml body so reviewers see provenance + warnings
// directly in the file (helpful when reviewing a Git diff without
// PROPOSAL.md context).
func withTranslatedHeader(body []byte, prop ExtractionProposal, translation *ProposalTranslation) []byte {
	header := strings.Builder{}
	header.WriteString("# Generated by C-9 Profile Builder PR-3 (PromRule → conf.d translator).\n")
	header.WriteString(fmt.Sprintf("# Cluster: %d member rules; dialect=%s; confidence=%s; translation=%s.\n",
		len(prop.MemberRuleIDs), prop.Dialect, prop.Confidence, translation.Status))
	if translation.Operator != "" {
		header.WriteString(fmt.Sprintf("# Comparison operator: `%s` (recorded for ADR-019 §emit-direction follow-ups).\n",
			translation.Operator))
	}
	if translation.Severity != "" {
		header.WriteString(fmt.Sprintf("# Severity: `%s`.\n", translation.Severity))
	}
	for _, w := range translation.Warnings {
		header.WriteString("# WARN: " + w + "\n")
	}
	header.WriteString("# See PROPOSAL.md for member listing + per-tenant overrides.\n\n")
	return append([]byte(header.String()), body...)
}

// formatThresholdString renders a float64 threshold value into the
// quoted-string form ResolveAt expects in tenant overrides. Drops
// trailing zeros so common values like 80.0 emit as "80".
func formatThresholdString(v float64) string {
	if v == float64(int64(v)) {
		return fmt.Sprintf("%d", int64(v))
	}
	return strings.TrimRight(strings.TrimRight(fmt.Sprintf("%f", v), "0"), ".")
}

// marshalDefaults builds the YAML content for `_defaults.yaml`.
// The shape carries the proposal-level shared structure plus
// metadata (dialect, provenance) that PR-3's translator will
// consume to produce the final conf.d/-ready dict.
func marshalDefaults(prop ExtractionProposal) ([]byte, error) {
	doc := map[string]any{
		"shared_expr_template": prop.SharedExprTemplate,
		"dialect":              prop.Dialect,
		"prom_portable":        prop.Dialect == string(parser.DialectProm),
		"member_count":         len(prop.MemberRuleIDs),
		"confidence":           string(prop.Confidence),
	}
	if prop.SharedFor != "" {
		doc["shared_for"] = prop.SharedFor
	}
	if len(prop.SharedLabels) > 0 {
		doc["shared_labels"] = sortedStringMap(prop.SharedLabels)
	}
	if len(prop.VaryingLabelKeys) > 0 {
		doc["varying_label_keys"] = append([]string(nil), prop.VaryingLabelKeys...)
	}
	return yamlMarshalCanonical(doc)
}

// marshalTenantOverride builds the per-tenant file. Wraps under
// `tenants:` so the existing extractTenantRaw() in
// config_inheritance.go can parse it (forward-compatibility — once
// the translator lands, this shape becomes the actual tenant.yaml).
func marshalTenantOverride(tenantID string, rule parser.ParsedRule, prop ExtractionProposal) ([]byte, error) {
	tenantBlock := map[string]any{
		"source_rule_id": rule.SourceRuleID,
		"expr":           rule.Expr,
	}
	if rule.Alert != "" {
		tenantBlock["alert"] = rule.Alert
	}
	if rule.Record != "" {
		tenantBlock["record"] = rule.Record
	}
	if rule.For != "" && rule.For != prop.SharedFor {
		tenantBlock["for"] = rule.For
	}
	// Only emit the labels that vary across the cluster — the
	// shared ones live in _defaults.yaml.
	varyingLabels := pickVaryingLabels(rule.Labels, prop)
	if len(varyingLabels) > 0 {
		tenantBlock["labels"] = sortedStringMap(varyingLabels)
	}
	if len(rule.Annotations) > 0 {
		// Annotations don't participate in clustering (they're
		// human text); keep them per-tenant verbatim.
		tenantBlock["annotations"] = sortedStringMap(rule.Annotations)
	}

	doc := map[string]any{
		"tenants": map[string]any{
			tenantID: tenantBlock,
		},
	}
	return yamlMarshalCanonical(doc)
}

// pickTenantLabelKey heuristically picks the label key that
// distinguishes members of a proposal — typically `tenant`, but
// the cluster may use `team` or other axes. Returns "" when no
// varying labels exist (caller falls back to a synthetic id).
//
// PR-2 heuristic: prefer "tenant" if present in VaryingLabelKeys,
// else pick the first VaryingLabelKey alphabetically. Stable.
func pickTenantLabelKey(prop ExtractionProposal) string {
	for _, k := range prop.VaryingLabelKeys {
		if k == "tenant" {
			return "tenant"
		}
	}
	if len(prop.VaryingLabelKeys) > 0 {
		// VaryingLabelKeys is already sorted (PR-1 contract).
		return prop.VaryingLabelKeys[0]
	}
	return ""
}

// tenantIDForRule extracts the tenant identifier from a rule's
// Labels using the picked tenant key. Falls back to "" when the
// rule has no value for that key.
func tenantIDForRule(rule parser.ParsedRule, tenantKey string) string {
	if tenantKey == "" {
		return ""
	}
	v, ok := rule.Labels[tenantKey]
	if !ok {
		return ""
	}
	return v
}

// pickVaryingLabels returns the subset of `labels` whose keys are
// in VaryingLabelKeys. The returned map is freshly allocated.
func pickVaryingLabels(labels map[string]string, prop ExtractionProposal) map[string]string {
	if len(labels) == 0 || len(prop.VaryingLabelKeys) == 0 {
		return nil
	}
	varyingSet := make(map[string]struct{}, len(prop.VaryingLabelKeys))
	for _, k := range prop.VaryingLabelKeys {
		varyingSet[k] = struct{}{}
	}
	out := make(map[string]string)
	for k, v := range labels {
		if _, ok := varyingSet[k]; ok {
			out[k] = v
		}
	}
	return out
}

// sortedStringMap converts a map[string]string into a
// yaml.MapSlice-equivalent representation. Returning a plain
// map[string]any is enough — yaml.v3 sorts keys alphabetically by
// default for map[string]any, so output is deterministic.
func sortedStringMap(in map[string]string) map[string]any {
	out := make(map[string]any, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

// renderProposalMarkdown produces the PROPOSAL.md body — a human
// summary so a reviewer can decide accept / reject without diving
// into the YAML files.
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
func sortedTenantKeys(m map[string]float64) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// sortedKeys returns map keys alphabetically. Local copy so emit.go
// doesn't depend on cluster.go's helper (which is unexported).
func sortedKeys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// quoteAll wraps each string in backticks for Markdown rendering.
func quoteAll(in []string) []string {
	out := make([]string, len(in))
	for i, s := range in {
		out[i] = "`" + s + "`"
	}
	return out
}

// joinClean joins path segments POSIX-style and strips empty
// components. Deterministic and OS-independent — emission paths
// are git-relative, never filesystem-native.
func joinClean(parts ...string) string {
	var keep []string
	for _, p := range parts {
		if p != "" {
			keep = append(keep, p)
		}
	}
	return path.Join(keep...)
}

// safeFilename strips characters that can't appear in a POSIX
// filename. Per-tenant ids in the wild are usually plain
// `tenant-a` style, but a defensive sanitiser saves us from the
// occasional `team/payments` style identifier.
//
// PR-2 takes the conservative route: replace `/` and `\` with `-`,
// strip leading dots (no hidden files), pass everything else
// through. Future PR-3 may need stricter rules once ADR-019
// pins the tenant-id grammar.
func safeFilename(s string) string {
	if s == "" {
		return "_unknown"
	}
	out := strings.NewReplacer("/", "-", "\\", "-").Replace(s)
	out = strings.TrimLeft(out, ".")
	if out == "" {
		return "_unknown"
	}
	return out
}

// yamlMarshalCanonical wraps yaml.v3 Marshal for stable output.
// yaml.v3 already sorts map[string]any keys alphabetically — this
// helper exists to:
//   - centralise the marshal call (single place to add header
//     comments later if PR-3 wants a `# generated by ...` line).
//   - turn yaml errors into a sentinel for the warnings collector.
func yamlMarshalCanonical(doc map[string]any) ([]byte, error) {
	if doc == nil {
		return nil, errors.New("yaml marshal: nil doc")
	}
	return yaml.Marshal(doc)
}
