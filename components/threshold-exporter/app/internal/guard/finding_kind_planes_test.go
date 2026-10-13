package guard

// finding_kind_planes_test.go — which plane every da-guard finding kind
// belongs to (#2904, ADR-036 §9). Routing kinds judge `_routing*` keys,
// routing profiles, domain policies, receivers / overrides / routes, or which
// tenants the route generator renders: ADR-036 §9 moves those checks to the
// generator, and tests/shared/test_routing_policy_parity.py refuses to let one
// be deleted from Go before the parity matrix pins the generator against it.
// Threshold kinds judge threshold values, defaults files or cardinality and
// stay with da-guard ("閾值相關的檢查不變").
//
// This is a _test.go file on purpose: TestFindingKindPlanes computes the kind
// set from the package's NON-test sources, so an entry here adds nothing to
// that set and a stale one (a kind no source has any more) stays visible as
// a key the computed set lacks.
//
// The classification is the one the Python gate kept by hand until #2904
// round 4 (THRESHOLD_KINDS, plus "emitted from routing.go"), moved here so
// the language that owns the kinds owns the decision too.

type kindPlane string

const (
	planeRouting   kindPlane = "routing"
	planeThreshold kindPlane = "threshold"
)

// findingKindPlanes must name exactly the kinds the package's sources
// produce (TestFindingKindPlanes). Adding a kind means deciding its plane
// here, in the diff; a routing one also needs a reason below.
var findingKindPlanes = map[FindingKind]kindPlane{
	// Threshold plane.
	FindingMissingRequired:             planeThreshold,
	FindingRedundantOverride:           planeThreshold,
	FindingCardinalityExceeded:         planeThreshold,
	FindingCardinalityWarning:          planeThreshold,
	FindingCustomAlertDuplicateSeries:  planeThreshold,
	FindingDefaultsTopLevelIgnored:     planeThreshold,
	FindingRootDefaultsUnwrapped:       planeThreshold,
	FindingRootDefaultsCriticalKey:     planeThreshold,
	FindingRootDefaultNullUndeclared:   planeThreshold,
	FindingSubtreeDefaultReservedKey:   planeThreshold,
	FindingSubtreeDefaultUndeliverable: planeThreshold,
	FindingScheduleNullValue:           planeThreshold,
	FindingValueNotServed:              planeThreshold,
	FindingMetricsNotGatherable:        planeThreshold,

	// Routing plane.
	FindingUnknownReceiverType:          planeRouting,
	FindingMissingReceiverField:         planeRouting,
	FindingConflictingReceiverField:     planeRouting,
	FindingInvalidReceiverField:         planeRouting,
	FindingEmptyOverrideMatcher:         planeRouting,
	FindingConflictingOverrideMatcher:   planeRouting,
	FindingDuplicateOverrideMatcher:     planeRouting,
	FindingRedundantOverrideReceiver:    planeRouting,
	FindingInvalidRouteEntry:            planeRouting,
	FindingRoutingValueNotString:        planeRouting,
	FindingRoutingGroupByInvalid:        planeRouting,
	FindingDomainPolicyViolation:        planeRouting,
	FindingCriticalEscalationMissing:    planeRouting,
	FindingCriticalEscalationLeak:       planeRouting,
	FindingUnknownRoutingProfile:        planeRouting,
	FindingDomainPolicyUnusable:         planeRouting,
	FindingRoutingProfilesUnusable:      planeRouting,
	FindingRoutingDefaultsRoutesIgnored: planeRouting,
	FindingRoutingInUnreadLocation:      planeRouting,
	FindingRoutingKeyIgnored:            planeRouting,
	FindingRoutingNotMapping:            planeRouting,
	FindingRoutingDefaultsNotMapping:    planeRouting,
	FindingInvalidTenantID:              planeRouting,
	FindingRoutingEnforcedBelowRoot:     planeRouting,
	FindingRoutingDefaultsNullBelowRoot: planeRouting,
	FindingRoutingProfileDuplicate:      planeRouting,
	FindingDuplicateTenant:              planeRouting,
	FindingDomainPolicyOutOfScope:       planeRouting,
}

// findingKindPlaneReasons says why a kind is on its plane. Required for
// every routing kind (that is the claim the parity gate then holds the
// generator to); allowed for a threshold kind whose plane is not obvious.
var findingKindPlaneReasons = map[FindingKind]string{
	FindingMissingRequired: "judges the effective config against --required-fields; a `_routing.*` " +
		"path there is judged against the resolved routing, but ADR-036 §9 makes such a path an " +
		"outright error (second half of #2904) instead of moving it to the generator, so what " +
		"is left is a threshold check",

	FindingUnknownReceiverType:          "judges a receiver's `type` (receiver contract)",
	FindingMissingReceiverField:         "judges a receiver's required fields (receiver contract)",
	FindingConflictingReceiverField:     "judges receiver fields that exclude each other (receiver contract)",
	FindingInvalidReceiverField:         "judges a receiver field's value (receiver contract)",
	FindingEmptyOverrideMatcher:         "judges an `_routing.overrides` matcher",
	FindingConflictingOverrideMatcher:   "judges an `_routing.overrides` matcher",
	FindingDuplicateOverrideMatcher:     "judges `_routing.overrides` matchers against each other",
	FindingRedundantOverrideReceiver:    "judges an override receiver against the main receiver",
	FindingInvalidRouteEntry:            "judges an `_routing.routes` entry the generator renders",
	FindingRoutingValueNotString:        "judges a matcher value of the resolved routing",
	FindingRoutingGroupByInvalid:        "judges a `group_by` element of the resolved or enforced routing",
	FindingDomainPolicyViolation:        "judges a receiver type against an ADR-007 domain policy",
	FindingCriticalEscalationMissing:    "judges a domain policy's require_critical_escalation",
	FindingCriticalEscalationLeak:       "judges a domain policy's require_critical_escalation",
	FindingUnknownRoutingProfile:        "judges `_routing_profile` against the routing profiles",
	FindingDomainPolicyUnusable:         "judges the shape of a `_domain_policy.yaml`",
	FindingRoutingProfilesUnusable:      "judges the shape of a `routing_profiles:` block",
	FindingRoutingDefaultsRoutesIgnored: "judges `_routing_defaults.routes`, which the generator drops",
	FindingRoutingInUnreadLocation:      "judges where a `_routing*` key sits relative to what the generator reads",
	FindingRoutingKeyIgnored:            "judges which keys of a routing block the generator reads",
	FindingRoutingNotMapping:            "judges the shape of a tenant's `_routing`",
	FindingRoutingDefaultsNotMapping:    "judges the shape of `_routing_defaults`",
	FindingInvalidTenantID:              "judges which tenants the route generator renders (it refuses the tree)",
	FindingRoutingEnforcedBelowRoot:     "judges the routing tree: `_routing_enforced` outside the root",
	FindingRoutingDefaultsNullBelowRoot: "judges the routing tree: a null `_routing_defaults` below the root",
	FindingRoutingProfileDuplicate:      "judges the routing tree: a routing profile defined twice",
	FindingDuplicateTenant:              "judges which tenants the route generator renders (one id, two files)",
	FindingDomainPolicyOutOfScope:       "judges the routing tree: a domain policy entry the generator does not enforce",
}
