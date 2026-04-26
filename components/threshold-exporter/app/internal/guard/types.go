// Package guard is the v2.8.0 Phase .c C-12 Dangling Defaults
// Guard. It answers "if I merge this `_defaults.yaml` change, will
// any tenant under it become invalid or carry redundant overrides?"
// before the change reaches the WatchLoop.
//
// The guard exists to defend the contract C-9 / C-10 set up: the
// migration toolkit moves shared structure into directory-level
// `_defaults.yaml` files. That move only stays safe if subsequent
// edits to those defaults can't (a) silently break tenants by
// removing fields they rely on, (b) leave tenants with overrides
// that are exact duplicates of the new defaults — a smell that
// accumulates into the pre-existing-orphan problem flagged in
// risk #16. The guard is the automation that flags both.
//
// Triggers (planning §C-12):
//   - GitHub Actions `on: pull_request` against any
//     `**/_defaults.yaml` change. C-10 PR-2 wires the `apply` mode
//     to require this guard pass before it allows the Base
//     Infrastructure PR to merge.
//   - Customer-side pre-commit hook (the same Go code packaged in
//     a CLI subcommand by C-11 Migration Toolkit).
//
// Pure library — operates on already-merged effective configs
// supplied by the caller, never touches YAML or disk. Checks shipped:
//
//  1. Schema validation (Severity=error, PR-1)
//     For every tenant under the affected scope: required fields
//     must be present and non-nil after merge. Missing fields
//     block merge.
//
//  2. Redundant override (Severity=warn, PR-1)
//     Per planning §C-12 Claude补. When a tenant.yaml field has
//     the same value as the new _defaults.yaml at the same
//     dotted path, the override carries no information — it just
//     duplicates the inherited value. Warning only; the
//     duplication is harmless at runtime.
//
//  3. Routing schema guardrails (PR-2; see routing.go)
//     Five checks against each tenant's `_routing` block:
//     unknown receiver type (error), missing receiver fields
//     (error), empty override matcher (error), duplicate override
//     matcher (warn), redundant override receiver (warn).
//     Note: the planning row originally said "routing tree cycle
//     detection" — the codebase's routing model is a flat
//     per-tenant block with no cross-references, so cycles are
//     structurally impossible. PR-2 ships the checks that
//     actually catch real bugs in this model. See routing.go
//     header for the full rationale.
//
// Future PRs in the C-12 family:
//   - PR-3: Cardinality Guard — post-merge label cardinality must
//     not exceed ADR-003 thresholds (planning §C-12 layer iii).
//   - PR-4: CLI subcommand `da-tools guard defaults-impact` plus
//     YAML parsing convenience layer that runs the actual merge
//     before invoking this library.
//   - PR-5: GitHub Actions wrapper that posts the rendered Markdown
//     report as a PR comment.
//
// PR-1 contract is sufficient for C-10's apply mode (PR-2) to
// invoke the guard programmatically: it merges defaults + tenant
// overrides itself (it already needs that path for the YAML
// emitter), then hands the merged maps to CheckDefaultsImpact.
package guard

// Severity classifies a Finding. Two tiers in PR-1; PR-2/3 may add
// "info" for the routing/cardinality layers if useful.
type Severity string

const (
	// SeverityError — blocks merge. The guard caller (CI, pre-commit
	// hook) returns non-zero exit code when any error is present.
	SeverityError Severity = "error"

	// SeverityWarn — surfaces in the report but doesn't block.
	// Used for redundant-override hints and (future) cosmetic
	// drift signals.
	SeverityWarn Severity = "warn"
)

// FindingKind labels what category of check produced the Finding.
// PR-1 ships two kinds; PR-2/3 will add "routing_cycle",
// "orphaned_route", "cardinality_exceeded".
type FindingKind string

const (
	FindingMissingRequired   FindingKind = "missing_required"
	FindingRedundantOverride FindingKind = "redundant_override"

	// Routing-schema findings (PR-2; see routing.go for rationale on
	// why this is not "routing_cycle"/"orphaned_route").
	FindingUnknownReceiverType       FindingKind = "unknown_receiver_type"
	FindingMissingReceiverField      FindingKind = "missing_receiver_field"
	FindingEmptyOverrideMatcher      FindingKind = "empty_override_matcher"
	FindingDuplicateOverrideMatcher  FindingKind = "duplicate_override_matcher"
	FindingRedundantOverrideReceiver FindingKind = "redundant_override_receiver"
)

// Finding is one issue the guard surfaced. Stable JSON serialisation
// — the GitHub Actions wrapper (PR-5) reads these directly to post
// PR comments + annotations.
type Finding struct {
	Severity Severity    `json:"severity"`
	Kind     FindingKind `json:"kind"`

	// TenantID identifies which tenant the finding applies to.
	// Empty for findings that span the whole defaults change rather
	// than any one tenant (none in PR-1; PR-2/3 may emit some).
	TenantID string `json:"tenant_id,omitempty"`

	// Field is a dotted-path pointer into the merged config map,
	// e.g. `thresholds.cpu_threshold` or
	// `routing._labels.severity`. Empty when the finding isn't
	// scoped to a single field.
	Field string `json:"field,omitempty"`

	// Message is the human-readable explanation. Stable wording
	// across runs — a CI diff against the previous report should
	// only show real changes, not text drift.
	Message string `json:"message"`
}

// GuardReport is the top-level result of one CheckDefaultsImpact
// run. Apply tooling (CI / pre-commit / CLI) decides go/no-go from
// Summary.Errors > 0; the rendered Markdown body comes from
// (*GuardReport).Markdown().
type GuardReport struct {
	// Findings are sorted: errors before warnings, then by
	// (TenantID, Field) within each severity bucket. Stable across
	// runs given the same input.
	Findings []Finding    `json:"findings"`
	Summary  GuardSummary `json:"summary"`
}

// GuardSummary is a cheap-to-display roll-up. Apply tooling shows
// these counts upfront so a reviewer can sanity-check the scope of
// findings before reading the full list.
type GuardSummary struct {
	// TotalTenants is the number of tenants the guard considered
	// (i.e. len(CheckInput.EffectiveConfigs)). Useful to confirm
	// the caller passed the expected scope of impact.
	TotalTenants int `json:"total_tenants"`

	// Errors counts SeverityError findings. > 0 → block merge.
	Errors int `json:"errors"`

	// Warnings counts SeverityWarn findings. Informational.
	Warnings int `json:"warnings"`

	// PassedTenantCount is the number of tenants with zero error-
	// severity findings. (A tenant with warnings but no errors
	// counts as "passed".) Helps reviewers see "92/100 tenants
	// pass; here are the 8 that need attention".
	PassedTenantCount int `json:"passed_tenant_count"`
}

// CheckInput is the contract between the caller and the guard.
// PR-1 deliberately operates on already-merged maps so the guard
// package has zero dependency on YAML parsing or the main
// package's merge engine. The CLI / GitHub Actions wrapper (PR-4 /
// PR-5) is where YAML → map[string]any conversion lives.
type CheckInput struct {
	// EffectiveConfigs maps tenant ID → the post-merge effective
	// config (i.e. new defaults deepMerged with the tenant's
	// override). Caller is responsible for the merge — see package
	// header for why we don't pull a merge engine into this package.
	EffectiveConfigs map[string]map[string]any `json:"effective_configs"`

	// TenantOverrides maps tenant ID → the raw tenant.yaml content
	// pre-merge. Required for the redundant-override check; pass
	// nil to skip that check entirely.
	TenantOverrides map[string]map[string]any `json:"tenant_overrides,omitempty"`

	// NewDefaults is the proposed new `_defaults.yaml` content
	// (already merged with any cascading parent defaults if the
	// affected scope sits below the root). Required for the
	// redundant-override check; pass nil to skip.
	NewDefaults map[string]any `json:"new_defaults,omitempty"`

	// RequiredFields is the dotted-path list the schema validator
	// asserts non-nil presence for in every tenant's effective
	// config. Empty/nil disables the schema check.
	//
	// PR-1 keeps this caller-supplied (no built-in schema). A future
	// PR may add an optional `internal/schema/required.yaml` loader
	// once the v2.8.0 mandatory-fields list lands.
	RequiredFields []string `json:"required_fields,omitempty"`

	// RoutingByTenant maps tenant ID → the parsed `_routing` block
	// for that tenant. Routing schema checks (added in PR-2) run
	// per tenant present in this map; tenants absent from the map
	// have routing checks skipped (no finding emitted, not even a
	// warning — absent routing is a valid configuration).
	//
	// The caller is responsible for parsing the routing payload —
	// `_routing` ships across the wire as a YAML-serialised string
	// inside ScheduledValue.Default, and unwrapping that requires
	// the main package's config types. The guard library deliberately
	// stays YAML-agnostic; the CLI wrapper (deferred PR-4) does the
	// extraction before invoking CheckDefaultsImpact.
	RoutingByTenant map[string]map[string]any `json:"routing_by_tenant,omitempty"`
}
