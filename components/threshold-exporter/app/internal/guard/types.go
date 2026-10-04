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
//  3. Routing schema guardrails (PR-2; see routing.go)
//     Checks against each tenant's `_routing` block: unknown
//     receiver type (error), missing receiver fields (error),
//     override matcher contract — exactly one of alertname/
//     metric_group, so both empty (error) and both set (error) are
//     blocked — duplicate override matcher (warn), redundant
//     override receiver (warn). Since #2280 the block checked is the
//     RESOLVED routing (`_routing_defaults` → routing profile → the
//     tenant's `_routing`), its ADR-007 `routes` entries are checked too
//     (invalid_route_entry + the same receiver checks), and every
//     receiver type is judged against the domain policies
//     (domain_policy_violation).
//     Note: the planning row originally said "routing tree cycle
//     detection" — the codebase's routing model is a flat
//     per-tenant block with no cross-references, so cycles are
//     structurally impossible. PR-2 ships the checks that
//     actually catch real bugs in this model. See routing.go
//     header for the full rationale.
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
	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
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

// FindingKind labels what category of check produced the Finding.
// PR-1 ships two kinds; PR-2/3 will add "routing_cycle",
// "orphaned_route", "cardinality_exceeded".
type FindingKind string

const (
	FindingMissingRequired   FindingKind = "missing_required"
	FindingRedundantOverride FindingKind = "redundant_override"
)

// Routing-schema findings (PR-2; see routing.go for rationale on
// why this is not "routing_cycle"/"orphaned_route").
const (
	FindingUnknownReceiverType        FindingKind = "unknown_receiver_type"
	FindingMissingReceiverField       FindingKind = "missing_receiver_field"
	FindingConflictingReceiverField   FindingKind = "conflicting_receiver_field"
	FindingInvalidReceiverField       FindingKind = "invalid_receiver_field"
	FindingEmptyOverrideMatcher       FindingKind = "empty_override_matcher"
	FindingConflictingOverrideMatcher FindingKind = "conflicting_override_matcher"
	FindingDuplicateOverrideMatcher   FindingKind = "duplicate_override_matcher"
	FindingRedundantOverrideReceiver  FindingKind = "redundant_override_receiver"
)

// Routing-resolution and domain-policy findings (#2280; see routing.go).
const (
	// FindingInvalidRouteEntry: a `routes` value the route generator does
	// not render — `routes` not a list, or an entry that is not a mapping,
	// carries an unsupported key, or has a missing / malformed `match`.
	FindingInvalidRouteEntry FindingKind = "invalid_route_entry"
	// FindingRoutingValueNotString (error; #2431): a matcher value of the
	// resolved routing — `routes[i].match.<label>` or `overrides[i].
	// alertname` / `metric_group` (Field) — that the route generator's
	// PyYAML does not read as a string (`yes`, `1:30`, `2001-12-15`, `~`, a
	// `!!int` tag). The generator refuses it under --strict; quote it.
	FindingRoutingValueNotString FindingKind = "routing_value_not_string"
	// FindingRoutingGroupByInvalid (error; #2503): a `group_by` element of
	// the resolved routing (Field `group_by[i]`, `overrides[i].group_by[j]`,
	// `routes[i].group_by[j]`) that, as the route generator's PyYAML reads
	// it, is not a string (`8`, `on`), is empty, repeats an earlier label
	// other than `...`,
	// or is `...` alongside other labels. Alertmanager refuses the last
	// three and groups by a label named after the text of the first. The
	// generator refuses it under --strict; quote or remove it. The same
	// element of a `_routing_enforced` route the generator renders is this
	// kind too, with an empty TenantID (it is a platform file's) and Field
	// `<root file>:_routing_enforced.group_by[i]`, or
	// `<root file>:_routing_enforced (<tenant>).group_by[i]` for the
	// `{{tenant}}` shape, judged per tenant after substitution.
	FindingRoutingGroupByInvalid FindingKind = "routing_group_by_invalid"
	// FindingDomainPolicyViolation: a receiver type an ADR-007 domain
	// policy forbids, or leaves out of its allowed list.
	FindingDomainPolicyViolation FindingKind = "domain_policy_violation"
	// FindingCriticalEscalationMissing (error; #2325): a domain policy sets
	// `require_critical_escalation: true` and severity=critical alerts reach
	// no pagerduty receiver — the main receiver is not one, and no rendered
	// `routes` entry matching `severity: critical` sends to one.
	FindingCriticalEscalationMissing FindingKind = "critical_escalation_missing"
	// FindingCriticalEscalationLeak (warn; #2325): the tenant escalates, but
	// this non-pagerduty destination (Field `<ref>.receiver.type`) still
	// receives some severity=critical alerts before any pagerduty receiver
	// does. Never blocks, as in the route generator.
	FindingCriticalEscalationLeak FindingKind = "critical_escalation_leak"
	// FindingUnknownRoutingProfile (warn): `_routing_profile` names a
	// profile no `_routing_profiles.yaml` defines; nothing is merged.
	FindingUnknownRoutingProfile FindingKind = "unknown_routing_profile"
	// FindingDomainPolicyUnusable (TenantID ""): a `_domain_policy.yaml`
	// structure the policy check cannot use, so it is not enforced.
	FindingDomainPolicyUnusable FindingKind = "domain_policy_unusable"
	// FindingRoutingProfilesUnusable (warn, TenantID ""): a
	// `routing_profiles:` block that cannot be read; no profile is applied.
	FindingRoutingProfilesUnusable FindingKind = "routing_profiles_unusable"
	// FindingRoutingDefaultsRoutesIgnored (error, TenantID ""): a
	// `_routing_defaults` carrying `routes`, which the route generator drops
	// with a blocking WARN (`--validate` fails) — so it blocks here too.
	FindingRoutingDefaultsRoutesIgnored FindingKind = "routing_defaults_routes_ignored"
	// FindingRoutingInUnreadLocation (error, TenantID ""; #2291): a
	// `_routing` / `_routing_*` key where the route generator never reads
	// it — a defaults block, the top level of an unwrapped defaults file,
	// a threshold profile. Field is `<file>:<key path>`.
	FindingRoutingInUnreadLocation FindingKind = "routing_in_unread_location"
	// FindingRoutingNotMapping (error; #2341): the tenant's `_routing`, as
	// the route generator's PyYAML reads it, is neither a mapping nor a
	// disabling string (`"slack"`, a list, null, an unquoted `false` / `off`
	// / `no`, which is a YAML boolean). The generator renders no route for
	// the tenant and refuses it (`--validate`, `--strict`). Field `_routing`.
	FindingRoutingNotMapping FindingKind = "routing_not_mapping"
	// FindingRoutingDefaultsNotMapping (error, TenantID ""; #2341): a
	// `_routing_defaults` (root platform file, or a subdirectory's defaults
	// carrier) that is neither a mapping nor null; the level contributes
	// nothing and the generator refuses it. Field `<file>:_routing_defaults`.
	FindingRoutingDefaultsNotMapping FindingKind = "routing_defaults_not_mapping"
	// FindingInvalidTenantID (error; #2341, ADR-035): a declared tenant id
	// the tenant-id rule refuses (routingpolicy.IsValidTenantID, a DNS-1123
	// label); the route generator refuses the whole tree. TenantID is the id
	// (it may be empty, so the finding is told from a platform one by its
	// kind), Field `<tenant file>:tenants.<id>`.
	FindingInvalidTenantID FindingKind = "invalid_tenant_id"
)

// Routing-tree findings (#2326, ADR-017 / ADR-007 "Amendment 2026-09-28"):
// the routing plane reads the whole conf.d, and these tree shapes are what
// the route generator refuses (exit 2, every mode) or — the out-of-scope
// policy entry — does not enforce. All errors, TenantID "", Field
// `<file>:<key path>`; the kind names equal routingpolicy's Problem kinds.
const (
	FindingRoutingEnforcedBelowRoot     FindingKind = "routing_enforced_below_root"
	FindingRoutingDefaultsNullBelowRoot FindingKind = "routing_defaults_null_below_root"
	FindingRoutingProfileDuplicate      FindingKind = "routing_profile_duplicate"
	FindingDuplicateTenant              FindingKind = "duplicate_tenant"
	FindingDomainPolicyOutOfScope       FindingKind = "domain_policy_out_of_scope"
)

// Cardinality findings (PR-3; see cardinality.go).
const (
	FindingCardinalityExceeded FindingKind = "cardinality_exceeded"
	FindingCardinalityWarning  FindingKind = "cardinality_warning"
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
	// config. Empty/nil disables the schema check.
	//
	// #2291: `_routing` and `_routing.<path>` are the exception — they are
	// judged against RoutingByTenant (the resolved routing the route
	// generator renders), not the effective config.
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

	// RoutingProvenance maps tenant ID → which layer each top-level key of
	// its RoutingByTenant entry came from (#2280): the tenant's `_routing`,
	// a routing profile, or `_routing_defaults`. Used in messages only;
	// a tenant absent here reads as "the tenant's _routing".
	RoutingProvenance map[string]routingpolicy.Provenance `json:"-"`

	// RoutingDisabled holds the tenants whose routing is turned off by a
	// disabling `_routing` string (`_routing: disable`). They are absent from
	// RoutingByTenant; a required `_routing*` field names the opt-out instead
	// of reading as an omission (#2291).
	RoutingDisabled map[string]bool `json:"-"`

	// RoutingNotMapping maps tenant ID → its `_routing` value (as PyYAML
	// reads it) when that is neither a mapping nor a disabling string
	// (#2341, routingpolicy.RoutingNotMapping). Such tenants are absent from
	// RoutingByTenant: the generator renders no route for them.
	RoutingNotMapping map[string]any `json:"-"`

	// InvalidTenantIDs maps each declared tenant id routingpolicy.IsValidTenantID
	// refuses to the tenant file that declares it (#2341). Such tenants are
	// absent from RoutingByTenant.
	InvalidTenantIDs map[string]string `json:"-"`

	// UnloadedTenants maps each tenant id the route generator does not load
	// to the tenant file that declares it (#2519): its body there is not a
	// mapping as PyYAML reads it (a null body — the exporter still serves the
	// tenant, so it is in EffectiveConfigs; routingpolicy.
	// PyYAMLTenantBodyNotMapping) and no root platform file's entry gives it
	// one (routingpolicy.Layers.PlatformBodies). Such a tenant is not in the
	// generator's tenant set, so a `{{tenant}}` `_routing_enforced` renders no
	// route for it and its group_by is not judged for it.
	UnloadedTenants map[string]string `json:"-"`

	// UnknownRoutingProfiles maps tenant ID → the `_routing_profile` it
	// references that no profile file defines (warn finding).
	UnknownRoutingProfiles map[string]string `json:"-"`

	// DomainPolicies are the ADR-007 receiver-type policies, checked against
	// every tenant in RoutingByTenant. nil disables the check.
	DomainPolicies []routingpolicy.Policy `json:"-"`

	// PlatformProblems are the platform-file structures the routing checks
	// could not use (routingpolicy.LoadRoot). Each becomes one finding with
	// an empty TenantID; the checks that do not depend on it still run.
	PlatformProblems []routingpolicy.Problem `json:"-"`

	// RoutingEnforced is the root's `_routing_enforced` block the route
	// generator renders from (routingpolicy.Tree.Enforced; nil: none). Only
	// its group_by is judged (#2503), the `{{tenant}}` shape over every
	// valid tenant id of EffectiveConfigs and RoutingByTenant — routed or not,
	// as the generator expands it (#2519) — less UnloadedTenants.
	RoutingEnforced *routingpolicy.Enforced `json:"-"`

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
}
