// Package batchpr is the v2.8.0 Phase .c C-10 Batch PR Pipeline. It
// turns a profile.ProposalSet (C-9 output) into an ordered Plan of
// pull requests that, when applied in sequence, import a customer's
// rule corpus into the conf.d/ Profile-as-Directory-Default
// architecture without leaving the repo in a half-migrated state
// between merges.
//
// The Hierarchy-Aware chunking rule (planning §C-10 + risk #13) is
// the heart of this package:
//
//  1. ALL `_defaults.yaml` changes go into a single
//     `[Base Infrastructure PR]`. Cascading defaults inside the
//     base PR are ordered outer→inner (root → domain → region →
//     env) as separate commits so a reviewer can step through.
//  2. Per-tenant changes are chunked by domain (default), region,
//     or fixed count. Each tenant PR carries an explicit
//     `Blocked by: <base>` marker so reviewers can't merge a
//     tenant PR before its underlying defaults landed.
//
// PR-1 ships the planner only — pure computation that produces a
// Plan struct. NO git operations, NO GitHub API calls, NO writes
// to disk. The Plan is rendered to markdown for human review and to
// JSON for downstream tooling consumption. PR-2 will pick up the
// Plan and execute it against a real Git remote (push branches,
// open PRs via the GitHub API, link Blocked-by metadata).
//
// Future PRs in the C-10 family:
//   - PR-2: `apply` mode — push branches + open PRs via GitHub API.
//   - PR-3: `refresh --base-merged` — rebase open tenant PRs onto
//     the merged base; emit semantic-drift report (planning §B4).
//   - PR-4: `refresh --source-rule-ids` — data-layer hot-fix mode
//     (planning §B5); reuses C-8 provenance.source_rule_id index.
//   - PR-5: CLI subcommands `da-tools batch-pr {plan,apply,refresh}`.
//
// PR-1 contract is sufficient for downstream tooling to:
//   - Validate the chunking strategy against synthetic ProposalSets
//     before any real git ops happen.
//   - Show humans a deterministic "this is what we'd do" markdown
//     report that they can sign off on.
//   - Drive C-9 PR-2 emission decisions (which file paths land in
//     which chunk affects how the YAML emitter lays files out).
package batchpr

// ChunkBy selects the grouping strategy for tenant PRs. Each value
// produces a different number of chunks for the same input.
type ChunkBy string

const (
	// ChunkByDomain (default) — group by the first path segment of
	// each tenant's directory. Conservative: keeps domain-level
	// review boundaries intact, gives reviewers a "this PR touches
	// only domain-a" mental model.
	ChunkByDomain ChunkBy = "domain"

	// ChunkByRegion — group by the first two path segments
	// (domain/region). Finer-grained; produces more PRs. Useful
	// when a single domain's tenant count blows past comfortable
	// review size.
	ChunkByRegion ChunkBy = "region"

	// ChunkByCount — fixed-size chunks of N tenants each. Used as a
	// last resort when domain/region grouping produces lopsided
	// chunks (one giant + many tiny). Caller specifies size via
	// PlanInput.ChunkSize.
	ChunkByCount ChunkBy = "count"
)

// PlanInput is the contract between C-9 (Profile Builder) and C-10
// (Batch PR Pipeline). The caller provides:
//
//   - The proposals C-9 wants to materialise (typically the full
//     ProposalSet.Proposals slice from a Profile Builder run).
//   - A `tenant → directory` mapping that pins each tenant to its
//     conf.d/ location. The planner doesn't infer locations from
//     the filesystem; it relies on the caller to have chosen them
//     (manually, via UI, or via a future C-9 PR-2 layout function).
//   - The chunking strategy + size.
//
// Empty fields use sensible defaults: ChunkBy=domain, ChunkSize=25.
type PlanInput struct {
	// Proposals are the ExtractionProposals from C-9. The planner
	// treats each proposal as one unit of work that may produce a
	// `_defaults.yaml` (in the Base PR) and zero-or-more per-tenant
	// override files (in tenant PRs). Empty input is an error.
	Proposals []ProposalRef `json:"proposals"`

	// TenantDirs maps tenant ID → conf.d/-relative directory
	// (e.g. `domain-a/region-1/tenant-foo`). Used to bucket tenant
	// changes by chunk strategy. Tenants present in a proposal but
	// missing from this map go to Plan.Warnings.
	//
	// An empty-string value (tenant present in the map but with
	// path `""`) is a degenerate case the planner accepts but
	// surfaces under the synthetic `<unassigned>` ChunkKey so the
	// chunk doesn't vanish from the plan. Callers should treat that
	// bucket as a hint to fix their TenantDirs input.
	TenantDirs map[string]string `json:"tenant_dirs"`

	// ChunkBy selects the tenant-PR grouping strategy. Empty value
	// defaults to ChunkByDomain (the safest pick for first-time
	// imports — review boundaries match domain ownership).
	ChunkBy ChunkBy `json:"chunk_by,omitempty"`

	// ChunkSize is the per-PR tenant cap. Required when
	// ChunkBy=ChunkByCount; otherwise treated as a soft cap that
	// splits an oversized domain/region chunk into sub-chunks of
	// at most ChunkSize tenants. Default 25 — large enough to
	// matter for 1000-tenant imports, small enough to keep PR
	// review tractable.
	ChunkSize int `json:"chunk_size,omitempty"`
}

// ProposalRef is the slice of profile.ExtractionProposal data the
// planner needs. Defined locally to avoid coupling this package to
// profile.ExtractionProposal's full surface — callers can build
// ProposalRefs from any source (C-9 ProposalSet, manual input, or
// a future fuzzy-matcher pass).
//
// Field naming + JSON tags match profile.ExtractionProposal so
// callers passing data through encoding/json don't need a mapping
// step.
type ProposalRef struct {
	// MemberRuleIDs identify which input rules this proposal covers.
	// Forwarded verbatim from C-9; the planner stores them for the
	// Base PR description but does NOT use them for chunking
	// decisions (chunking is driven by MemberTenantIDs + TenantDirs).
	MemberRuleIDs []string `json:"member_rule_ids"`

	// MemberTenantIDs is the post-clustering view of which tenants
	// this proposal applies to. Drives chunk assignment — the
	// planner walks every proposal's MemberTenantIDs to build the
	// universe of tenants that need PRs. Required because rule IDs
	// don't directly encode tenant identity; the caller (C-9 PR-2
	// or a UI accept loop) is the right place to compute the
	// tenant list from the underlying ParsedRule.Labels.
	MemberTenantIDs []string `json:"member_tenant_ids"`

	// SharedFor / SharedLabels / VaryingLabelKeys are forwarded
	// from the C-9 proposal verbatim. The planner uses them to
	// build descriptive PR titles + Plan.Summary stats; it does
	// NOT use them to (re-)derive groupings.
	SharedFor        string            `json:"shared_for,omitempty"`
	SharedLabels     map[string]string `json:"shared_labels,omitempty"`
	VaryingLabelKeys []string          `json:"varying_label_keys,omitempty"`

	// Dialect is forwarded from C-9. Determines whether the Base
	// PR title should signal `[prom]` vs `[metricsql]` so reviewers
	// can route the change to the right SME.
	Dialect string `json:"dialect"`
}

// PlanItemKind discriminates Base Infrastructure PRs from tenant
// PRs in the rendered Plan. Renderers and downstream apply tooling
// branch on this.
type PlanItemKind string

const (
	PlanItemBase   PlanItemKind = "base_infra"
	PlanItemTenant PlanItemKind = "tenant"
)

// PlanItem is one PR's worth of work in the eventual rollout. Items
// are ordered: Base PR first, tenant PRs after, in deterministic
// chunk order. Apply tooling (PR-2) walks this slice in order.
type PlanItem struct {
	Kind PlanItemKind `json:"kind"`

	// Title is the proposed PR title. Stable across runs given the
	// same input. Format:
	//   - Base:   `[Base Infrastructure] Import N profiles (<dialect>)`
	//   - Tenant: `[chunk i/N] Import PromRules to <chunk-key>`
	Title string `json:"title"`

	// Description is the Markdown body the apply tooling will paste
	// into the PR. Includes the Blocked-by reference for tenant PRs
	// + a per-item-detail table.
	Description string `json:"description"`

	// BlockedBy is the chunk index of the Base PR a tenant PR
	// depends on, or empty for the Base PR itself. PR-2 will
	// translate this into a `Blocked by: #<actual-pr-num>` once
	// the Base PR has been opened.
	BlockedBy string `json:"blocked_by,omitempty"`

	// SourceProposalIndices index into PlanInput.Proposals. Lets
	// downstream tooling map a PlanItem back to the C-9 proposal
	// that motivated it (for refresh / rollback flows).
	SourceProposalIndices []int `json:"source_proposal_indices"`

	// TenantIDs (tenant PRs only) lists which tenants this chunk
	// touches. Sorted for stability. Empty for Base PRs.
	TenantIDs []string `json:"tenant_ids,omitempty"`

	// ChunkKey (tenant PRs only) is the bucketing key — domain name,
	// `domain/region`, or a synthetic count-bucket label. Empty
	// for Base PRs.
	ChunkKey string `json:"chunk_key,omitempty"`
}

// Plan is the top-level result of a planner run. Apply tooling walks
// Items in order; humans review Markdown(); the JSON serialisation
// is the audit artifact saved alongside any actual apply.
type Plan struct {
	Items    []PlanItem  `json:"items"`
	Summary  PlanSummary `json:"summary"`
	Warnings []string    `json:"warnings,omitempty"`
}

// PlanSummary is the cheap-to-display roll-up. Apply tooling shows
// these counts upfront to confirm scale before any push.
type PlanSummary struct {
	TotalProposals     int     `json:"total_proposals"`
	BasePRCount        int     `json:"base_pr_count"` // 0 or 1
	TenantPRCount      int     `json:"tenant_pr_count"`
	TotalTenants       int     `json:"total_tenants"`
	ChunkBy            ChunkBy `json:"chunk_by"`
	EffectiveChunkSize int     `json:"effective_chunk_size"`
}
