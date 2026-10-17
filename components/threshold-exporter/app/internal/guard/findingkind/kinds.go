package findingkind

// Every da-guard finding kind. Adding one means writing the reason here, in
// the diff. The kind names are the JSON contract.
//
// ⛔ No routing or domain-policy kind (ADR-036 §7 step 3, #2905): the route
// generator is the one judge of routing and domain policy, and da-guard
// reports a tree that holds them as undetermined (exit 4). The routing kinds
// da-guard used to register (unknown_receiver_type … domain_policy_out_of_scope)
// were deleted with their checks; a check that judges routing belongs in the
// generator (scripts/tools/ops/_grar_*.py), not here.

// Schema and redundancy findings (PR-1).
var (
	// FindingMissingRequired (error): a --required-fields path absent or nil
	// in the tenant's effective config.
	FindingMissingRequired = register("missing_required",
		"judges the effective config against --required-fields; a path under a routing key "+
			"there is a caller error (ADR-036 §9, #2904), never judged here")
	// FindingRedundantOverride (warn): a tenant override equal to the value
	// it inherits.
	FindingRedundantOverride = register("redundant_override",
		"judges a tenant override against the defaults value it inherits")
)

// Cardinality findings (PR-3; see internal/guard/cardinality.go).
var (
	FindingCardinalityExceeded = register("cardinality_exceeded",
		"judges a tenant's predicted series count against the cardinality cap")
	FindingCardinalityWarning = register("cardinality_warning",
		"judges a tenant's predicted series count against the cardinality cap")
)

// Threshold-tree findings: what the exporter serves on /metrics from the
// defaults chain and the tenants' threshold keys.
var (
	// FindingRootDefaultsUnwrapped (error, TenantID ""; #2386): the conf.d root
	// defaults carrier has no `defaults:` mapping, and its top level holds keys
	// /effective shows but the exporter's root decode does not read. Field is
	// the file's root-relative path.
	FindingRootDefaultsUnwrapped = register("root_defaults_unwrapped",
		"judges the conf.d root defaults file's `defaults:` wrapper")
	// FindingDefaultsTopLevelIgnored (error, TenantID ""; #2386): a defaults
	// carrier whose `defaults:` is a mapping and whose top level also holds keys
	// the merge would otherwise take (actsWhenMerged). Field is the file's
	// root-relative path.
	FindingDefaultsTopLevelIgnored = register("defaults_toplevel_ignored",
		"judges a defaults file's top-level keys beside its `defaults:` mapping")
	// FindingRootDefaultsCriticalKey (warn, TenantID ""; #2544): a
	// `<metric>_critical` key (either #1231 spelling) under the conf.d root
	// carrier's `defaults:` mapping. It does not become `<metric>`'s
	// severity=critical row, which only a tenant's override map
	// carries (the tenant's own key, or one a subtree `_defaults.yaml`, a root
	// platform `tenants:` entry or a profile hands it). Field is
	// `<file>:defaults.<key>`. Not blocking (owner decision on #2544, option b).
	FindingRootDefaultsCriticalKey = register("root_defaults_critical_key",
		"judges a `<metric>_critical` key in the conf.d root defaults")
	// FindingRootDefaultNullUndeclared (warn; #2518): the tenant's config sets
	// Field, which the conf.d root `_defaults.yaml` writes as null — not
	// declared — so the exporter serves no series for it. Field may also be a
	// `<base>_critical` key whose base the root writes as null: the critical row
	// needs the base in the root defaults.
	FindingRootDefaultNullUndeclared = register("root_default_null_undeclared",
		"judges a tenant threshold the conf.d root defaults write as null")
	// FindingSubtreeDefaultUndeliverable (warn; #1976): the tenant inherits Field,
	// a threshold key (see undeliverableThresholds) a subtree `_defaults.yaml` names and the conf.d root `_defaults.yaml`
	// and `optional_overrides:` do not declare. The exporter serves no series for
	// it. Planned to become an error in the next minor release.
	FindingSubtreeDefaultUndeliverable = register("subtree_default_undeliverable",
		"judges a threshold key a subtree defaults file names and the root does not declare")
	// FindingSubtreeDefaultReservedKey (warn; #2388): a subtree `_defaults.yaml`
	// in the tenant's defaults chain carries Field, a reserved key, in its
	// defaults, whatever its non-null value and whether or not the exporter
	// applies it.
	// Planned to become an error in the next minor release, when the exporter
	// stops applying such keys.
	FindingSubtreeDefaultReservedKey = register("subtree_default_reserved_key",
		"judges a reserved key in a subtree defaults file (pkg/config subtreeDefaultsRefusedKey)")
	// FindingScheduleNullValue (error; #2708): a threshold is written as a
	// schedule with override windows and a null in it. Field is
	// `<file>:<section>.[<owner>.]<key>`; TenantID is the tenant for a
	// `tenants:` entry, empty for a defaults block or a profile.
	FindingScheduleNullValue = register("schedule_null_value",
		"judges a null inside a threshold schedule that has override windows")
	// FindingValueNotServed (error; #2065): the tenant's effective config shows
	// Field with a value /metrics does not serve as written. The message starts
	// with the not_served reason.
	FindingValueNotServed = register("value_not_served",
		"judges a threshold value /metrics does not serve as written")
	// FindingCustomAlertDuplicateSeries (error; #2031, #3112): a `_custom_alerts`
	// entry the exporter drops because an earlier entry already serves its
	// series, or a row of its shape and severity.
	FindingCustomAlertDuplicateSeries = register("custom_alert_duplicate_series",
		"judges `_custom_alerts` entries that put the same series on /metrics")
	// FindingCustomAlertOverCap (error; #3042): a tenant's own `_custom_alerts`
	// asks the exporter to serve more rows than the per-tenant cap.
	FindingCustomAlertOverCap = register("custom_alert_over_cap",
		"judges a tenant's own `_custom_alerts` against the per-tenant cap")
	// FindingMetricsNotGatherable (error; #2031): the exporter's /metrics cannot
	// be gathered for the tree.
	FindingMetricsNotGatherable = register("metrics_not_gatherable",
		"judges whether the exporter's /metrics can be gathered for the whole tree")
)
