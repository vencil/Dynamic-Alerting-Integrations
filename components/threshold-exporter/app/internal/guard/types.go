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
// supplied by the caller, never touches disk; the one YAML decode is
// of the root defaults carrier's bytes, also supplied by the caller
// (check 5). Checks shipped:
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
//  3. Routing schema guardrails (PR-2) — deleted (ADR-036 §7 step 3, #2905).
//     The route generator (`da-tools generate-routes --validate --strict`)
//     is the one judge of routing and domain policy; the CLI names a tree
//     that holds them as undetermined (cmd/da-guard/undetermined.go, exit 4).
//     The number is kept so the check numbers below stay as cited.
//
//  4. Cardinality guard (PR-3; see cardinality.go)
//     Predicts each tenant's post-merge metric count and flags
//     tenants approaching or exceeding the configured per-tenant
//     ceiling. Conservative upper-bound counter (doesn't model
//     dimensional expansion); intent is to catch "this defaults
//     change blows past the runtime truncation threshold" before
//     it lands. Two tiers: SeverityWarn at WarnRatio×Limit (80%
//     by default), SeverityError above Limit.
//
//  5. Defaults wrapper (#2386; see rootdefaults.go), both errors:
//     the conf.d root `_defaults.yaml` with no `defaults:` mapping
//     (its top-level thresholds are not served on /metrics, while the
//     merged effective configs show them), and a `_defaults.yaml` at
//     any level whose `defaults:` mapping leaves top-level keys out of
//     the defaults merge.
//
//  6. Root `_critical` keys (#2544; see rootdefaults.go), warn: a
//     `<metric>_critical` key under the conf.d root `defaults:` is served
//     as a threshold of its own, never as `<metric>`'s critical row, so it is
//     no fallback for that row and check 2 does not judge a tenant's key
//     against it (config.EffectiveConfig.MergedDefaults leaves it out).
//
//  7. Undeliverable subtree defaults (#1976; see subtree.go), warn for now:
//     a tenant inherits a threshold key only a subtree `_defaults.yaml`
//     names, which the exporter serves no series for. The set is the
//     exporter's build's own (CheckInput.UndeliverableInherited), filtered
//     by pkg/config's undeliverableThresholds, not re-derived here.
//
//  8. Reserved keys in subtree defaults (#2388; see subtree_reserved.go), warn for
//     now: a subtree `_defaults.yaml` in a tenant's chain carries a reserved
//     key (`_state_*`, `_silent_mode`, …) in its defaults. The set is read
//     off the exporter's build's chain (CheckInput.SubtreeReservedKeys).
//
//  9. Root defaults written as null (#2518; see rootnull.go), warn: the
//     tenant's config sets a threshold the conf.d root `_defaults.yaml`
//     writes as null, which declares nothing, so the exporter serves no
//     series for it. The set is the build's (CheckInput.RootNullUndeclared).
//
//  10. Nulls inside a schedule with override windows (#2708; see
//     schedule_null.go), error: a window's `value: null`, or a null
//     `default:` beside windows. The set is pkg/config's
//     (CheckInput.ScheduleNulls).
//
//  11. Values /metrics does not serve as written (#2065; see
//     value_not_served.go), error: a tenant value that is not a number the
//     exporter reads, a schedule window it does not accept, or a subtree
//     defaults value it refuses. The set is the effective config's
//     NotServed (CheckInput.ValuesNotServed), the table `da-guard
//     effective` prints as not_served.
//
//  12. Custom alerts serving one series twice (#2031; see
//     custom_alert_duplicate.go), error: a `_custom_alerts` entry the
//     exporter drops because an earlier entry already serves its series. The
//     set is the exporter's own (CheckInput.CustomAlertDuplicates).
//
//  13. A tree whose /metrics cannot be gathered (#2031; see
//     metrics_not_gatherable.go), error: some series is produced twice, so
//     the exporter's scrape fails as a whole (CheckInput.MetricsNotGatherable),
//     over the whole tree.
//
// Future PRs in the C-12 family:
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

import (
	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/pkg/config"
)

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

// Finding is one issue the guard surfaced. Stable JSON serialisation
// — the GitHub Actions wrapper (PR-5) reads these directly to post
// PR comments + annotations.
type Finding struct {
	Severity Severity         `json:"severity"`
	Kind     findingkind.Kind `json:"kind"`

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
//
// Do not embed GuardReport anonymously; use a named field, since its
// MarshalJSON would replace the outer struct's encoding.
type GuardReport struct {
	// Findings are sorted: errors before warnings, then by
	// (TenantID, Field) within each severity bucket. Stable across
	// runs given the same input.
	Findings []Finding    `json:"findings"`
	Summary  GuardSummary `json:"summary"`

	// unregistered counts the findings sealed when the report was built
	// (seal.go); UnregisteredKinds reads it.
	unregistered int
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
	// redundant-override check WHEN every tenant in scope shares
	// one merged-defaults map (the simple "single _defaults.yaml
	// at level X" case). Pass nil to skip the check.
	//
	// When tenants under the scope inherit *different* merged
	// defaults (cascading L0/L1/L2 trees with multiple defaults
	// files at different depths), use NewDefaultsByTenant instead
	// — the per-tenant variant is checked first and falls back to
	// NewDefaults only when a tenant lacks a per-tenant entry.
	NewDefaults map[string]any `json:"new_defaults,omitempty"`

	// NewDefaultsByTenant maps tenant ID → the merged defaults map
	// that THAT tenant inherits before its own override is applied.
	// PR-5 (v2.8.0) extension: the C-12 PR-4 CLI populates this
	// from `pkg/config.EffectiveConfig.MergedDefaults` so the
	// redundant-override check correctly compares each tenant's
	// override against ITS chain of cascading defaults rather
	// than a single global defaults map.
	//
	// Resolution rule per tenant: NewDefaultsByTenant[id] wins when
	// present; otherwise fall back to NewDefaults (preserves the
	// PR-1 single-map API for callers that haven't migrated).
	// Tenants absent from both have the redundant-override check
	// skipped silently — no finding emitted.
	NewDefaultsByTenant map[string]map[string]any `json:"new_defaults_by_tenant,omitempty"`

	// RequiredFields is the dotted-path list the schema validator
	// asserts non-nil presence for in every tenant's effective
	// config. Empty/nil disables the schema check. A path under a
	// routing key is the CLI's caller error (exit 2, ADR-036 §9): the
	// route generator judges routing, and the effective config is not
	// what it renders (#2291).
	//
	// PR-1 keeps this caller-supplied (no built-in schema). A future
	// PR may add an optional `internal/schema/required.yaml` loader
	// once the v2.8.0 mandatory-fields list lands.
	RequiredFields []string `json:"required_fields,omitempty"`

	// CardinalityLimit is the per-tenant ceiling the cardinality
	// check (PR-3) compares each tenant's predicted metric count
	// against. ≤ 0 disables the cardinality check entirely.
	//
	// Should equal the cap the exporter enforces for the tree:
	// config.RootMaxMetricsPerTenant (the ROOT `_defaults.yaml`'s
	// `max_metrics_per_tenant`, unset = DefaultMaxMetricsPerTenant),
	// which is what cmd/da-guard passes when --cardinality-limit is
	// omitted (#2043). A negative cap (no truncation) maps to 0 here.
	CardinalityLimit int `json:"cardinality_limit,omitempty"`

	// CardinalityWarnRatio is the fraction of CardinalityLimit that
	// triggers a Severity=warn finding (the error finding fires at
	// 100% + 1). Out of [0, 1] or zero defaults to 0.8 — early
	// warning at 80% gives operators a buffer to act before runtime
	// truncation kicks in. Set to 1.0 to disable the warning tier
	// (errors only).
	CardinalityWarnRatio float64 `json:"cardinality_warn_ratio,omitempty"`

	// DefaultsFiles are the defaults carriers of the scan
	// (config.ScopedTenants.DefaultsFiles), checked for the `defaults:`
	// wrapper shapes (#2386, rootdefaults.go); nil skips the check.
	DefaultsFiles []config.DefaultsFile `json:"-"`

	// ParseFailed are the files the exporter drops
	// (config.ScopedTenants.ParseFailed); the wrapper check skips them, so a
	// broken file is named once, by exit 3.
	ParseFailed []string `json:"-"`

	// UndeliverableInherited maps tenant ID → the threshold keys it inherits from a
	// subtree `_defaults.yaml` that the exporter's build cannot deliver
	// (config.ScopedTenants.Undeliverable, i.e. FlatBuild.Unreachable). Each
	// becomes a subtree_default_undeliverable warning for a tenant in
	// EffectiveConfigs (#1976); nil skips the check.
	UndeliverableInherited map[string][]string `json:"-"`

	// SubtreeReservedKeys maps tenant ID → reserved key → the subtree
	// `_defaults.yaml` files (root-relative) in the tenant's defaults chain
	// whose defaults carry that key (config.ScopedTenants.SubtreeReserved).
	// Each (tenant, key) becomes a subtree_default_reserved_key warning for a
	// tenant in EffectiveConfigs (#2388); nil skips the check.
	SubtreeReservedKeys map[string]map[string][]string `json:"-"`

	// DeclaredStateFilters is the root `state_filters:` names
	// (config.ScopedTenants.DeclaredStateFilters); the
	// subtree_default_reserved_key fix for `_state_<f>` depends on whether f
	// is declared (#2388 r2). nil = none declared.
	DeclaredStateFilters map[string]bool `json:"-"`

	// SubtreeRefusedVerdicts is config.ScopedTenants.SubtreeRefusedVerdicts:
	// for every tenant of the tree (not only EffectiveConfigs), key → the
	// exporter subtree overlay's verdict. The subtree_default_reserved_key fix
	// for a recognised key depends on it (#2388 A); absent = not applied.
	SubtreeRefusedVerdicts map[string]map[string]config.SubtreeRefusedVerdict `json:"-"`
	// RootNullUndeclared maps tenant ID → the threshold keys its config sets
	// that are not served because the conf.d root `_defaults.yaml` writes
	// them (or a `_critical` key's base) as null — not declared (config.ScopedTenants.RootNullUndeclared, i.e.
	// FlatBuild.RootNullUndeclared). Each becomes a
	// root_default_null_undeclared warning for a tenant in EffectiveConfigs
	// (#2518); nil skips the check.
	RootNullUndeclared map[string][]config.RootNullKey `json:"-"`

	// ScheduleNulls are the thresholds, in the files the exporter reads that
	// bear on the scope, written as a schedule with override windows and a
	// null in it (config.ScopedTenants.ScheduleNulls). Each becomes a
	// schedule_null_value error (#2708); nil skips the check.
	ScheduleNulls []config.ScheduleNull `json:"-"`

	// ValuesNotServed maps tenant ID → key → why /metrics does not serve
	// that key as the tenant's effective config shows it
	// (config.EffectiveConfig.NotServed, filled by ScopeEffective). The
	// reasons value_unparsed, value_unparsed_dropped, window_invalid,
	// value_rejected and spelling_duplicate (#2031;
	// config.ValueNotServedReasons) become value_not_served errors for a
	// tenant in EffectiveConfigs (#2065); nil skips the check.
	ValuesNotServed map[string]map[string]config.NotServedKey `json:"-"`

	// CustomAlertDuplicates maps tenant ID → the `_custom_alerts` entries
	// the exporter drops because an earlier entry of the list already serves
	// their series (config.CustomAlertDuplicates over the exporter's build).
	// Each becomes a custom_alert_duplicate_series error for a tenant in
	// EffectiveConfigs (#2031); nil skips the check.
	CustomAlertDuplicates map[string][]config.CustomAlertDuplicate `json:"-"`

	// CustomAlertsOverCap maps tenant ID → the custom-alert rows the exporter
	// serves it from its OWN `_custom_alerts` (not a profile's), for each
	// tenant over config.MaxCustomRecipesDefault. Each becomes a
	// custom_alert_over_cap error for a tenant in EffectiveConfigs (#3042);
	// nil skips the check.
	CustomAlertsOverCap map[string]int `json:"-"`

	// MetricsNotGatherable is why the exporter's /metrics cannot be gathered
	// for the whole tree:
	// two keys giving one series fail the scrape for every tenant (HTTP 500).
	// Non-empty becomes one metrics_not_gatherable error (#2031); "" skips.
	MetricsNotGatherable string `json:"-"`
}
