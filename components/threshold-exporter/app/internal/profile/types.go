// Package profile is the v2.8.0 Phase .c C-9 Profile Builder. It
// consumes ParsedRule records emitted by internal/parser (C-8) and
// proposes how to map them into the conf.d/ Profile-as-Directory-
// Default architecture (ADR-019, in flight): a few `_defaults.yaml`
// files at appropriate directory levels + thin per-tenant overrides,
// instead of N copies of nearly-identical tenant.yaml files (the
// GitOps anti-pattern Phase .c is built to prevent).
//
// PR-1 ships the cluster engine + extraction proposals only — pure
// computation that identifies "these N rules look similar; here's
// what their shared structure is and what varies per tenant". The
// builder reports proposals; it does NOT yet emit conf.d/ YAML
// files. That's PR-2's responsibility once ADR-019 locks down the
// directory-vs-file boundary semantics.
//
// Future PRs in the C-9 family:
//   - PR-2: emit `_defaults.yaml` + tenant.yaml from accepted
//     proposals (interactive accept / reject loop).
//   - PR-3: ADR-019 Profile-as-Directory-Default written + linked.
//   - PR-4: UI surface for "XX tenants will inherit this Profile,
//     est. YY lines saved" semi-automatic accept loop.
//
// PR-1 contract is sufficient for downstream tooling to:
//   - Show humans a deterministic list of "groups we'd extract".
//   - Drive simulation of post-extraction effective config (combined
//     with C-7b /simulate primitive).
//   - Exercise the C-10 Batch PR pipeline against synthetic clusters
//     before customer rules are available.
package profile

// Confidence labels how strong the cluster engine's evidence for
// grouping a set of rules together is. Two passes contribute:
//
//   - high   : ≥ MinClusterSize members share an EXACT strict
//     signature (normalised expression + `for:` + dialect).
//     Auto-emit candidates — no per-tenant decision required.
//   - medium : Strict pass left these rules in sub-MinClusterSize
//     buckets, but the fuzzy pass (PR-5) found
//     ≥ MinClusterSize members under a *duration-canonicalised*
//     signature. Same dialect, same `for:`, same labels-shape
//     as the strict path; the only loosening is that
//     `[5m]` ≡ `[300s]` ≡ `[300000ms]` inside the expression.
//     Surface for human review — the merge is sound but the
//     reviewer should confirm the duration-equivalence isn't
//     hiding a semantically meaningful difference.
//   - low    : reserved for future fuzzy axes (e.g. for-variance,
//     label-key asymmetry). PR-5 does NOT emit ConfidenceLow —
//     each axis we add deserves its own design + test pass.
//
// PR-1 only emitted ConfidenceHigh. PR-5 adds ConfidenceMedium when
// `ClusterOptions.EnableFuzzy=true`. The high/medium boundary is
// stable across runs given the same input + opts, so callers can
// build per-tier review workflows.
type Confidence string

const (
	ConfidenceHigh   Confidence = "high"
	ConfidenceMedium Confidence = "medium"
	ConfidenceLow    Confidence = "low"
)

// ExtractionProposal recommends pulling a set of similar rules into
// a single `_defaults.yaml` (the SharedFields) plus per-tenant
// overrides for the VaryingLabels.
//
// The proposal is descriptive, not prescriptive: it identifies WHAT
// could be extracted but does not commit to a directory placement
// (PR-2's `dir_hint` mechanism handles that). Callers are free to
// accept, reject, or adjust each proposal before it reaches the
// YAML-emission stage.
type ExtractionProposal struct {
	// MemberRuleIDs are the SourceRuleID strings (from C-8 parser)
	// of every rule that contributes to this proposal. Sorted for
	// stable diffs across runs. Length is always ≥ ClusterMinSize
	// (see ClusterOptions).
	MemberRuleIDs []string `json:"member_rule_ids"`

	// SharedExprTemplate is the normalised expression every member
	// rule reduces to under the parser's normalisation rules. This
	// is *not* the raw expr of any single member; it's a template
	// where threshold literals and per-tenant label values have been
	// replaced with placeholders so the cluster signature is stable.
	// Useful as a human-readable "what this group of rules computes"
	// summary when displayed in UI.
	SharedExprTemplate string `json:"shared_expr_template"`

	// SharedFor is the alert `for:` duration shared by every member.
	// Empty when the cluster is recording-rule-only (no `for`).
	SharedFor string `json:"shared_for,omitempty"`

	// SharedLabels are the labels (key→value) identical across every
	// member of the cluster. These become the candidate
	// `_defaults.yaml` entries for the Labels block.
	SharedLabels map[string]string `json:"shared_labels,omitempty"`

	// VaryingLabelKeys names the label keys whose values differ
	// across members. These must remain per-tenant overrides — they
	// can't be hoisted into `_defaults.yaml`. Sorted for stability.
	// Empty for clusters where every label is shared (in which case
	// the per-tenant override is just the rule identity).
	VaryingLabelKeys []string `json:"varying_label_keys,omitempty"`

	// Dialect carries the cluster's classification through from the
	// input ParsedRule (every member shares the same dialect; mixed
	// dialects are NEVER clustered together by PR-1).
	Dialect string `json:"dialect"`

	// EstimatedYAMLLineSavings is a back-of-envelope count of YAML
	// lines the extraction would save vs. the current "N full
	// rules" shape. UI consumers display this; downstream tooling
	// should not gate decisions on this number alone.
	//
	// Formula: (N - 1) × shared_field_lines. Approximate; a
	// real-world count depends on YAML formatting choices in the
	// emission step (PR-2).
	EstimatedYAMLLineSavings int `json:"estimated_yaml_line_savings"`

	// Confidence labels the strength of the grouping evidence. PR-1
	// only ever emits ConfidenceHigh (identical signatures); future
	// fuzzy clustering passes will populate the medium / low tiers.
	Confidence Confidence `json:"confidence"`

	// Reason is a one-line human summary explaining why these rules
	// clustered together. Stable across runs given the same input.
	Reason string `json:"reason"`
}

// ProposalSet is the top-level result of a Profile Builder run.
// Callers iterate Proposals to decide which extractions to apply,
// and inspect Unclustered for rules that didn't qualify under any
// proposal (often: one-off alerts that legitimately need their own
// tenant.yaml).
type ProposalSet struct {
	// Proposals are sorted by MemberRuleIDs[0] for deterministic
	// ordering across runs.
	Proposals []ExtractionProposal `json:"proposals"`

	// Unclustered lists SourceRuleIDs for rules that did not
	// participate in any extraction proposal. They are not lost —
	// downstream YAML emission will place them as standalone tenant
	// rules. PR-2's fuzzier matcher may pull some of these into
	// medium-confidence proposals later.
	Unclustered []string `json:"unclustered,omitempty"`

	// Stats summarises the run for humans. Keep it cheap to compute;
	// UI surfaces these directly without re-walking the proposals.
	Stats ProposalStats `json:"stats"`
}

// ProposalStats is a lightweight summary attached to every
// ProposalSet. PR-1 populates the basic counts; PR-2 will extend
// with timing + confidence breakdowns.
type ProposalStats struct {
	TotalRulesIn     int `json:"total_rules_in"`
	ProposalsEmitted int `json:"proposals_emitted"`
	RulesClustered   int `json:"rules_clustered"`
	RulesUnclustered int `json:"rules_unclustered"`
	TotalLineSavings int `json:"total_estimated_line_savings"`
}

// ClusterOptions tune the cluster engine. Defaults (ClusterOptions{})
// give the conservative PR-1 behaviour: identical-signature only,
// minimum 2 members per cluster, no dialect mixing, no fuzzy pass.
type ClusterOptions struct {
	// MinClusterSize is the smallest number of rules that must share
	// a signature before the engine emits a proposal. Below this,
	// rules go to Unclustered. Default 2 (any pair of identical
	// rules is worth proposing — though high-value clusters are
	// usually 5+).
	//
	// The same threshold applies to BOTH the strict pass (PR-1) and
	// the fuzzy pass (PR-5). Callers wanting different gates per
	// pass should run BuildProposals twice with different options
	// rather than complicating the single-pass contract.
	MinClusterSize int

	// SkipAmbiguous, when true, drops DialectAmbiguous rules from
	// the input set entirely (they go to neither Proposals nor
	// Unclustered, and don't count in Stats). When false (the PR-1
	// default), ambiguous rules pass through to Unclustered so the
	// caller sees them surface for human review.
	SkipAmbiguous bool

	// EnableFuzzy, when true, runs the fuzzy second-pass clustering
	// (PR-5) over the strict-pass residue (rules in sub-MinClusterSize
	// buckets). Fuzzy uses duration-canonicalised signatures so
	// `rate(foo[5m])` and `rate(foo[300s])` collapse into a single
	// ConfidenceMedium proposal that strict mode wouldn't surface.
	//
	// Default false — preserves PR-1 behaviour for existing callers
	// (the `da-tools profile` CLI, simulate-time builders, etc.) so
	// turning it on is an explicit decision. The expected production
	// path is "off by default; on for migration-time customer corpus
	// processing where missing duration-equivalent collapses costs
	// real `_defaults.yaml` line savings".
	EnableFuzzy bool
}
