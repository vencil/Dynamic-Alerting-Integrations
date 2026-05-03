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
	"fmt"
	"path"
	"sort"
	"strings"

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

	// Decisions, when non-nil, filters which proposals get emitted
	// based on per-proposal accept/reject decisions captured in a
	// reviewer-edited YAML file (see decisions.go). nil = emit all
	// proposals (PR-2/PR-3 backward-compat).
	//
	// Filter semantics:
	//   - decision="accept" → emit
	//   - decision="reject" → skip (no warning — reviewer's intent)
	//   - decision="pending" / unknown / not-recorded
	//     → controlled by Decisions.UndecidedPolicy
	//       (default "emit" matches PR-2/PR-3 behaviour)
	//
	// Mismatched member_rule_ids (recorded vs. live cluster) and
	// stale keys (decision references a cluster no longer in the
	// ProposalSet) surface as Warnings without overriding the
	// reviewer's verdict — see decisions.go::applyDecisions.
	Decisions *ProposalDecisions `json:"decisions,omitempty"`

	// EmitDecisionsScaffold, when true, asks EmitProposals to emit
	// a `proposal-decisions.yaml` file at RootPrefix's root (one
	// per emit run, NOT per-proposal). The file lists every
	// proposal in the live ProposalSet with decision="pending" so
	// reviewers have a starter file to edit.
	//
	// false (default) skips scaffold emission — useful for the
	// "I already have decisions, just emit the accepted set" call.
	EmitDecisionsScaffold bool `json:"emit_decisions_scaffold,omitempty"`
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

	// Decisions filter (PR-4). When Decisions is nil, emitIdx is
	// every proposal index — backward-compatible behaviour. When
	// non-nil, only the accepted (and policy-permitted-pending) set
	// passes through; rejected and skip-policy clusters drop out.
	emitIdx, decisionWarnings := applyDecisions(props, input.Decisions)
	out.Warnings = append(out.Warnings, decisionWarnings...)

	emitSet := make(map[int]struct{}, len(emitIdx))
	for _, i := range emitIdx {
		emitSet[i] = struct{}{}
	}

	for i, prop := range props {
		if _, ok := emitSet[i]; !ok {
			continue
		}
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

	// PR-4 scaffold emission. Lives at RootPrefix's root (not under
	// any proposal dir) so reviewers see a single file to edit
	// regardless of how many proposals the run produced.
	if input.EmitDecisionsScaffold {
		scaffold := ScaffoldDecisions(input.ProposalSet)
		if scaffoldBytes, err := EncodeDecisions(scaffold); err == nil {
			out.Files[joinClean(rootPrefix, "proposal-decisions.yaml")] = scaffoldBytes
		} else {
			out.Warnings = append(out.Warnings, fmt.Sprintf(
				"failed to emit proposal-decisions.yaml scaffold: %v", err))
		}
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
