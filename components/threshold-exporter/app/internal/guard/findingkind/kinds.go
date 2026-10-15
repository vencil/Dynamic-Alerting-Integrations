package findingkind

// Every da-guard finding kind. Adding one means choosing its plane and
// writing the reason here, in the diff; a routing kind is then red in
// tests/shared/test_routing_policy_parity.py until the parity matrix pins
// the route generator against it. The kind names are the JSON contract.

// Schema and redundancy findings (PR-1).
var (
	// FindingMissingRequired (error): a --required-fields path absent or nil
	// in the tenant's effective config.
	FindingMissingRequired = register("missing_required", PlaneThreshold,
		"judges the effective config against --required-fields; a `_routing.*` "+
			"path there is judged against the resolved routing, but ADR-036 §9 makes such a path an "+
			"outright error (second half of #2904) instead of moving it to the generator, so what "+
			"is left is a threshold check")
	// FindingRedundantOverride (warn): a tenant override equal to the value
	// it inherits.
	FindingRedundantOverride = register("redundant_override", PlaneThreshold,
		"judges a tenant override against the defaults value it inherits")
)

// Routing-schema findings (PR-2; see internal/guard/routing.go for why this
// is not "routing_cycle" / "orphaned_route").
var (
	FindingUnknownReceiverType = register("unknown_receiver_type", PlaneRouting,
		"judges a receiver's `type` (receiver contract)")
	FindingMissingReceiverField = register("missing_receiver_field", PlaneRouting,
		"judges a receiver's required fields (receiver contract)")
	FindingConflictingReceiverField = register("conflicting_receiver_field", PlaneRouting,
		"judges receiver fields that exclude each other (receiver contract)")
	FindingInvalidReceiverField = register("invalid_receiver_field", PlaneRouting,
		"judges a receiver field's value (receiver contract)")
	FindingEmptyOverrideMatcher = register("empty_override_matcher", PlaneRouting,
		"judges an `_routing.overrides` matcher")
	FindingConflictingOverrideMatcher = register("conflicting_override_matcher", PlaneRouting,
		"judges an `_routing.overrides` matcher")
	FindingDuplicateOverrideMatcher = register("duplicate_override_matcher", PlaneRouting,
		"judges `_routing.overrides` matchers against each other")
	FindingRedundantOverrideReceiver = register("redundant_override_receiver", PlaneRouting,
		"judges an override receiver against the main receiver")
)

// Routing-resolution and domain-policy findings (#2280; see
// internal/guard/routing.go).
var (
	// FindingInvalidRouteEntry: a `routes` value the route generator does
	// not render — `routes` not a list, or an entry that is not a mapping,
	// carries an unsupported key, or has a missing / malformed `match`.
	FindingInvalidRouteEntry = register("invalid_route_entry", PlaneRouting,
		"judges an `_routing.routes` entry the generator renders")
	// FindingRoutingValueNotString (error; #2431): a matcher value of the
	// resolved routing — `routes[i].match.<label>` or `overrides[i].
	// alertname` / `metric_group` (Field) — that the route generator's
	// PyYAML does not read as a string (`yes`, `1:30`, `2001-12-15`, `~`, a
	// `!!int` tag). The generator refuses it under --strict; quote it.
	FindingRoutingValueNotString = register("routing_value_not_string", PlaneRouting,
		"judges a matcher value of the resolved routing")
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
	FindingRoutingGroupByInvalid = register("routing_group_by_invalid", PlaneRouting,
		"judges a `group_by` element of the resolved or enforced routing")
	// FindingDomainPolicyViolation: a receiver type an ADR-007 domain
	// policy forbids, or leaves out of its allowed list.
	FindingDomainPolicyViolation = register("domain_policy_violation", PlaneRouting,
		"judges a receiver type against an ADR-007 domain policy")
	// FindingCriticalEscalationMissing (error; #2325): a domain policy sets
	// `require_critical_escalation: true` and severity=critical alerts reach
	// no pagerduty receiver — the main receiver is not one, and no rendered
	// `routes` entry matching `severity: critical` sends to one.
	FindingCriticalEscalationMissing = register("critical_escalation_missing", PlaneRouting,
		"judges a domain policy's require_critical_escalation")
	// FindingCriticalEscalationLeak (warn; #2325): the tenant escalates, but
	// this non-pagerduty destination (Field `<ref>.receiver.type`) still
	// receives some severity=critical alerts before any pagerduty receiver
	// does. Never blocks, as in the route generator.
	FindingCriticalEscalationLeak = register("critical_escalation_leak", PlaneRouting,
		"judges a domain policy's require_critical_escalation")
	// FindingUnknownRoutingProfile (warn): `_routing_profile` names a
	// profile no `_routing_profiles.yaml` defines; nothing is merged.
	FindingUnknownRoutingProfile = register("unknown_routing_profile", PlaneRouting,
		"judges `_routing_profile` against the routing profiles")
	// FindingDomainPolicyUnusable (TenantID ""): a `_domain_policy.yaml`
	// structure the policy check cannot use, so it is not enforced.
	FindingDomainPolicyUnusable = register("domain_policy_unusable", PlaneRouting,
		"judges the shape of a `_domain_policy.yaml`")
	// FindingRoutingProfilesUnusable (warn, TenantID ""): a
	// `routing_profiles:` block that cannot be read; no profile is applied.
	FindingRoutingProfilesUnusable = register("routing_profiles_unusable", PlaneRouting,
		"judges the shape of a `routing_profiles:` block")
	// FindingRoutingDefaultsRoutesIgnored (error, TenantID ""): a
	// `_routing_defaults` carrying `routes`, which the route generator drops
	// with a blocking WARN (`--validate` fails) — so it blocks here too.
	FindingRoutingDefaultsRoutesIgnored = register("routing_defaults_routes_ignored", PlaneRouting,
		"judges `_routing_defaults.routes`, which the generator drops")
	// FindingRoutingInUnreadLocation (error, TenantID ""; #2291): a
	// `_routing` / `_routing_*` key where the route generator never reads
	// it — a defaults block, a threshold profile (there: every key
	// config.IsRoutingLayerKey accepts, #2914). Field is `<file>:<key path>`.
	FindingRoutingInUnreadLocation = register("routing_in_unread_location", PlaneRouting,
		"judges where a routing-layer key (`_routing*`, `_severity_dedup`) sits relative to what the generator reads")
	// FindingRoutingKeyIgnored (error; #2960): a key in a tenant `_routing`,
	// a routing profile, `_routing_defaults` or `_routing_enforced` (or in a
	// receiver / `overrides` entry under them) the route generator does not
	// read — `continue`, `mute_time_intervals`, a typo. The block renders as
	// if the key were absent; the generator blocks `--validate` on it. Field
	// is the path relative to the tenant's `_routing` (TenantID set), else
	// `<file>:<key path>`.
	FindingRoutingKeyIgnored = register("routing_key_ignored", PlaneRouting,
		"judges which keys of a routing block the generator reads")
	// FindingRoutingNotMapping (error; #2341): the tenant's `_routing`, as
	// the route generator's PyYAML reads it, is neither a mapping nor a
	// disabling string (`"slack"`, a list, null, an unquoted `false` / `off`
	// / `no`, which is a YAML boolean). The generator renders no route for
	// the tenant and refuses it (`--validate`, `--strict`). Field `_routing`.
	FindingRoutingNotMapping = register("routing_not_mapping", PlaneRouting,
		"judges the shape of a tenant's `_routing`")
	// FindingRoutingDefaultsNotMapping (error, TenantID ""; #2341): a
	// `_routing_defaults` (root platform file, or a subdirectory's defaults
	// carrier) that is neither a mapping nor null; the level contributes
	// nothing and the generator refuses it. Field `<file>:_routing_defaults`.
	FindingRoutingDefaultsNotMapping = register("routing_defaults_not_mapping", PlaneRouting,
		"judges the shape of `_routing_defaults`")
	// FindingRoutingEnforcedMatchInvalid (error, TenantID ""; #2989): an
	// enabled root `_routing_enforced` whose `match` is written but is not a
	// list of matcher strings (`{severity: critical}`, the `routes[i].match`
	// spelling). The generator renders no enforced route from that block and
	// blocks `--validate` on it; before, it dropped the `match` and the NOC
	// route received every alert. Field `<file>:_routing_enforced.match`.
	FindingRoutingEnforcedMatchInvalid = register("routing_enforced_match_invalid", PlaneRouting,
		"judges the shape of `_routing_enforced.match`")
	// FindingInvalidTenantID (error; #2341, ADR-035): a declared tenant id
	// the tenant-id rule refuses (tenantid.Valid, a DNS-1123
	// label); the route generator refuses the whole tree. TenantID is the id
	// (it may be empty, so the finding is told from a platform one by its
	// kind), Field `<tenant file>:tenants.<id>`.
	FindingInvalidTenantID = register("invalid_tenant_id", PlaneRouting,
		"judges which tenants the route generator renders (it refuses the tree)")
)

// Routing-tree findings (#2326, ADR-017 Decision 9 / ADR-007 "Amendment 2026-09-28"):
// the routing plane reads the whole conf.d, and these tree shapes are what
// the route generator refuses (exit 2, every mode) or — the out-of-scope
// policy entry — does not enforce. All errors, TenantID "", Field
// `<file>:<key path>`; the kind names equal routingpolicy's Problem kinds.
var (
	FindingRoutingEnforcedBelowRoot = register("routing_enforced_below_root", PlaneRouting,
		"judges the routing tree: `_routing_enforced` outside the root")
	FindingRoutingDefaultsNullBelowRoot = register("routing_defaults_null_below_root", PlaneRouting,
		"judges the routing tree: a null `_routing_defaults` below the root")
	FindingRoutingProfileDuplicate = register("routing_profile_duplicate", PlaneRouting,
		"judges the routing tree: a routing profile defined twice")
	FindingDuplicateTenant = register("duplicate_tenant", PlaneRouting,
		"judges which tenants the route generator renders (one id, two files)")
	FindingDomainPolicyOutOfScope = register("domain_policy_out_of_scope", PlaneRouting,
		"judges the routing tree: a domain policy entry the generator does not enforce")
)

// Cardinality findings (PR-3; see internal/guard/cardinality.go).
var (
	FindingCardinalityExceeded = register("cardinality_exceeded", PlaneThreshold,
		"judges a tenant's predicted series count against the cardinality cap")
	FindingCardinalityWarning = register("cardinality_warning", PlaneThreshold,
		"judges a tenant's predicted series count against the cardinality cap")
)

// Threshold-tree findings: what the exporter serves on /metrics from the
// defaults chain and the tenants' threshold keys.
var (
	// FindingRootDefaultsUnwrapped (error, TenantID ""; #2386): the conf.d root
	// defaults carrier has no `defaults:` mapping, and its top level holds keys
	// /effective shows but the exporter's root decode does not read. Field is
	// the file's root-relative path.
	FindingRootDefaultsUnwrapped = register("root_defaults_unwrapped", PlaneThreshold,
		"judges the conf.d root defaults file's `defaults:` wrapper")
	// FindingDefaultsTopLevelIgnored (error, TenantID ""; #2386): a defaults
	// carrier whose `defaults:` is a mapping and whose top level also holds keys
	// the merge would otherwise take (actsWhenMerged). Field is the file's
	// root-relative path.
	FindingDefaultsTopLevelIgnored = register("defaults_toplevel_ignored", PlaneThreshold,
		"judges a defaults file's top-level keys beside its `defaults:` mapping")
	// FindingRootDefaultsCriticalKey (warn, TenantID ""; #2544): a
	// `<metric>_critical` key (either #1231 spelling) under the conf.d root
	// carrier's `defaults:` mapping. It does not become `<metric>`'s
	// severity=critical row, which only a tenant's override map
	// carries (the tenant's own key, or one a subtree `_defaults.yaml`, a root
	// platform `tenants:` entry or a profile hands it). Field is
	// `<file>:defaults.<key>`. Not blocking (owner decision on #2544, option b).
	FindingRootDefaultsCriticalKey = register("root_defaults_critical_key", PlaneThreshold,
		"judges a `<metric>_critical` key in the conf.d root defaults")
	// FindingRootDefaultNullUndeclared (warn; #2518): the tenant's config sets
	// Field, which the conf.d root `_defaults.yaml` writes as null — not
	// declared — so the exporter serves no series for it. Field may also be a
	// `<base>_critical` key whose base the root writes as null: the critical row
	// needs the base in the root defaults.
	FindingRootDefaultNullUndeclared = register("root_default_null_undeclared", PlaneThreshold,
		"judges a tenant threshold the conf.d root defaults write as null")
	// FindingSubtreeDefaultUndeliverable (warn; #1976): the tenant inherits Field,
	// a threshold key (see undeliverableThresholds) a subtree `_defaults.yaml` names and the conf.d root `_defaults.yaml`
	// and `optional_overrides:` do not declare. The exporter serves no series for
	// it. Planned to become an error in the next minor release.
	FindingSubtreeDefaultUndeliverable = register("subtree_default_undeliverable", PlaneThreshold,
		"judges a threshold key a subtree defaults file names and the root does not declare")
	// FindingSubtreeDefaultReservedKey (warn; #2388): a subtree `_defaults.yaml`
	// in the tenant's defaults chain carries Field, a reserved key, in its
	// defaults, whatever its non-null value and whether or not the exporter
	// applies it.
	// Planned to become an error in the next minor release, when the exporter
	// stops applying such keys.
	FindingSubtreeDefaultReservedKey = register("subtree_default_reserved_key", PlaneThreshold,
		"judges a reserved key in a subtree defaults file (pkg/config subtreeDefaultsRefusedKey)")
	// FindingScheduleNullValue (error; #2708): a threshold is written as a
	// schedule with override windows and a null in it. Field is
	// `<file>:<section>.[<owner>.]<key>`; TenantID is the tenant for a
	// `tenants:` entry, empty for a defaults block or a profile.
	FindingScheduleNullValue = register("schedule_null_value", PlaneThreshold,
		"judges a null inside a threshold schedule that has override windows")
	// FindingValueNotServed (error; #2065): the tenant's effective config shows
	// Field with a value /metrics does not serve as written. The message starts
	// with the not_served reason.
	FindingValueNotServed = register("value_not_served", PlaneThreshold,
		"judges a threshold value /metrics does not serve as written")
	// FindingCustomAlertDuplicateSeries (error; #2031): a `_custom_alerts` entry
	// the exporter drops because an earlier entry already serves its series.
	FindingCustomAlertDuplicateSeries = register("custom_alert_duplicate_series", PlaneThreshold,
		"judges `_custom_alerts` entries that put the same series on /metrics")
	// FindingMetricsNotGatherable (error; #2031): the exporter's /metrics cannot
	// be gathered for the tree.
	FindingMetricsNotGatherable = register("metrics_not_gatherable", PlaneThreshold,
		"judges whether the exporter's /metrics can be gathered for the whole tree")
)
