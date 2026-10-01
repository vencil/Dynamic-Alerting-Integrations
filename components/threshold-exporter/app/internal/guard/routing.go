package guard

// Routing schema guardrails — PR-2 of the C-12 Dangling Defaults
// Guard family.
//
// SCOPE REDIRECT FROM PLANNING SPEC
// ----------------------------------
// Planning row §C-12 layer (ii) describes "ADR-016/017 Routing
// Guardrails — routing tree cycle detection + orphaned route
// detection". That description assumes an Alertmanager-style
// graph where receivers can reference routes and back-edges are
// possible.
//
// The actual `_routing` model in this codebase is a *flat
// per-tenant block*: one receiver + an optional `overrides[]`
// list of sub-routes. Routes never reference receivers by name,
// receivers never trigger routes, sub-routes don't reference
// parent routes. **Cycles are structurally impossible.**
// "Orphaned route" is also degenerate — there's nothing for a
// route to be orphaned from.
//
// Implementing a graph cycle detector against a model that can't
// cycle would be theatre. PR-2 instead ships the checks that catch
// real bugs in the model that exists:
//
//   1. Unknown receiver type (error)
//      receiver.type not in {webhook, email, slack, teams,
//      rocketchat, pagerduty}. The Alertmanager pipeline rejects
//      these silently per existing code (config_resolve.go), so
//      catching them at the guard layer surfaces the issue
//      before merge.
//
//   2. Missing or unloadable required receiver fields (error)
//      A required value of the wrong type or format (#2180) is an
//      error too: Alertmanager would reject the whole config. So are
//      optional values Alertmanager cannot load (#2295): a non-boolean
//      send_resolved / require_tls, and a malformed http_config (more
//      than one auth method, a bad proxy_url). Contract per type:
//      pkg/receiverspec (shared with tenant-api and pkg/config), pinned
//      to the hub docs/schemas/tenant-config.schema.json. Same checks
//      for receivers embedded in overrides and routes.
//
//   3. Override matcher contract (error)
//      The route generator
//      (scripts/tools/ops/_grar_routes.py::_validate_override_matcher)
//      requires EXACTLY ONE of `alertname` / `metric_group` per
//      override, treating an empty-string value as unset, and skips
//      any override that violates that. We block both discarded
//      shapes so they can't merge as a silent no-op:
//        - neither set (empty matcher) — would otherwise shadow ALL
//          alerts for the tenant. Kind: empty_override_matcher.
//        - both set (conflicting) — the generator can't pick one, so
//          the override never fires. Kind: conflicting_override_matcher.
//      No other key (severity, component, db_type, environment) is a
//      matcher; they ride along as receiver/timing config.
//
//   4. Duplicate override matcher (warning)
//      Two overrides with identical matchers — the first wins
//      and the second is dead code. Warning so the author can
//      remove the dead override.
//
//   5. Redundant override receiver (warning)
//      An override whose receiver is structurally identical to
//      the main tenant receiver has no effect. Warning.
//
// #2280 — the routing checked is the RESOLVED one, not the raw `_routing`:
// cmd/da-guard resolves `_routing_defaults` → routing profile → the tenant's
// `_routing` (pkg/routingpolicy, pinned to the Python generator by
// tests/shared/routing_policy_parity_matrix.json) before calling the guard.
// On top of the five checks:
//
//  6. ADR-007 `routes` (error): an entry the generator skips is
//     invalid_route_entry; a renderable entry's receiver gets checks 1 + 2.
//  7. Domain policies (error): every rendered receiver type (main,
//     overrides, routes) against forbidden_receiver_types and
//     allowed_receiver_types, judged independently.
//  8. Unknown routing profile (warn), and platform files the checks could
//     not use (domain_policy_unusable error / routing_profiles_unusable
//     warn / routing_defaults_routes_ignored error, TenantID ""). Only the checks that need such a file are
//     skipped; the run and its exit code are otherwise unchanged (#1654).
//  9. Routing in an unread location (error, TenantID "", #2291): a
//     `_routing` / `_routing_*` key in a defaults block or a threshold
//     profile, which the generator never renders — the tenant layer
//     cmd/da-guard resolves is the tenant file's plus the root platform
//     overlay's, never the effective config.
// 10. A matcher value that is not a string (error, #2431,
//     routing_value_not_string): `routes[i].match.<label>` or
//     `overrides[i].alertname` / `metric_group` as the generator's PyYAML
//     reads it (cmd/da-guard hands over the routing with those values
//     re-read, routingpolicy.WithPyYAMLRouting) — the generator's --strict
//     ERROR, one predicate: routingpolicy.ValuesNotString.
// 11. A bad group_by element (error, #2503, routing_group_by_invalid): not
//     a string as the generator's PyYAML reads it, empty, a repeated label,
//     or `...` alongside other labels — main route, `overrides[i]`,
//     `routes[i]` — the generator's --strict ERROR, one predicate:
//     routingpolicy.GroupByInvalid. The same for the `_routing_enforced`
//     route(s) the generator renders (TenantID "", Field
//     `<file>:_routing_enforced[ (<tenant>)].group_by[i]`):
//     routingpolicy.EnforcedGroupByInvalid — the only part of the NOC layer
//     da-guard reads.
//
// Why these and not more:
//   - Field-by-field receiver validation against type-specific
//     constraints (URL allowlist, timing bounds, etc.) is
//     better placed in the existing config_resolve.go layer
//     where it already lives — duplicating here would drift.
//   - Cross-referencing override matchers against actual alert
//     rule names needs rule discovery (which alerts exist
//     globally), out of scope for the per-tenant guard.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/receiverspec"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
)

// matcherKeys is the EXACT set of override-block keys the routing
// pipeline treats as a matcher: alertname and metric_group only.
//
// The generator's contract lives in
// scripts/tools/ops/_grar_routes.py::_validate_override_matcher +
// _build_override_matchers: an override must carry EXACTLY ONE of
// alertname / metric_group, and the emitted sub-route's matcher list
// is built solely from whichever one is set (`tenant="…",
// alertname="…"` OR `tenant="…", metric_group="…"`). No other key —
// severity, component, db_type, environment — ever contributes to a
// matcher; the generator treats those (alongside group_by / timing)
// as receiver/timing config and never matches on them.
//
// So the guard canonicalises an override's matcher "intent" from
// these two keys alone (used by the duplicate-matcher check).
// Anything else inside an override entry is receiver/timing config,
// not a matcher — the guard stays lenient about those keys and only
// tightens matcher semantics.
var matcherKeys = map[string]struct{}{
	"alertname":    {},
	"metric_group": {},
}

// checkRoutingGuardrails runs the five PR-2 routing checks. Returns
// findings; run.go handles the global sort.
//
// The per-tenant checks are a no-op when input.RoutingByTenant is empty —
// absent routing is a valid configuration (some tenants intentionally
// disable alerting), and silence here matches that intent. The single-shape
// `_routing_enforced` route is judged regardless (#2503), as the generator
// renders it with no tenant routing at all.
//
// #2280: also reports the platform files the routing checks could not use
// (PlatformProblems), the unknown routing profiles, and — per tenant — the
// domain-policy verdict on every receiver type (checkDomainPolicies).
func checkRoutingGuardrails(input CheckInput) []Finding {
	out := platformProblemFindings(input.PlatformProblems)
	for _, tenantID := range sortedStringKeys(input.UnknownRoutingProfiles) {
		name := input.UnknownRoutingProfiles[tenantID]
		out = append(out, Finding{
			Severity: SeverityWarn,
			Kind:     FindingUnknownRoutingProfile,
			TenantID: tenantID,
			Field:    "_routing_profile",
			Message: fmt.Sprintf(
				"tenant %q: _routing_profile references unknown profile %q (no _routing_profiles.yaml at the conf.d root defines it); nothing from it is applied",
				tenantID, name),
		})
	}
	tenants := make([]string, 0, len(input.RoutingByTenant))
	for t := range input.RoutingByTenant {
		tenants = append(tenants, t)
	}
	sort.Strings(tenants)
	out = append(out, checkEnforcedGroupBy(input.RoutingEnforced, input.RoutingByTenant, tenants)...)
	if len(input.RoutingByTenant) == 0 {
		return out
	}

	for _, tenantID := range tenants {
		routing := input.RoutingByTenant[tenantID]
		if routing == nil {
			continue
		}
		out = append(out, checkOneTenantRouting(tenantID, routing)...)
		out = append(out, checkValuesNotString(tenantID, routing)...)
		out = append(out, checkGroupByInvalid(tenantID, routing)...)
		out = append(out, checkDomainPolicies(tenantID, routing, input.DomainPolicies, input.RoutingProvenance[tenantID])...)
		out = append(out, checkCriticalEscalation(tenantID, routing, input.DomainPolicies)...)
	}
	return out
}

// checkValuesNotString reports each matcher value of the resolved routing
// the route generator's PyYAML does not read as a string (#2431,
// routingpolicy.ValuesNotString — the generator's --strict ERROR).
func checkValuesNotString(tenantID string, routing map[string]any) []Finding {
	var out []Finding
	for _, v := range routingpolicy.ValuesNotString(routing) {
		out = append(out, Finding{
			Severity: SeverityError,
			Kind:     FindingRoutingValueNotString,
			TenantID: tenantID,
			Field:    v.Field,
			Message:  fmt.Sprintf("tenant %q: %s", tenantID, v.Message()),
		})
	}
	return out
}

// checkGroupByInvalid reports each bad group_by element of the resolved
// routing (#2503, routingpolicy.GroupByInvalid — the generator's --strict
// ERROR).
func checkGroupByInvalid(tenantID string, routing map[string]any) []Finding {
	var out []Finding
	for _, p := range routingpolicy.GroupByInvalid(routing) {
		out = append(out, Finding{
			Severity: SeverityError,
			Kind:     FindingRoutingGroupByInvalid,
			TenantID: tenantID,
			Field:    p.Field,
			Message:  fmt.Sprintf("tenant %q: %s", tenantID, p.Message()),
		})
	}
	return out
}

// checkEnforcedGroupBy reports each bad group_by element of the
// `_routing_enforced` route(s) the generator renders (#2503,
// routingpolicy.EnforcedGroupByInvalid — the generator's --strict ERROR),
// the `{{tenant}}` shape once per tenant with a resolved routing (the
// generator expands it over its routing_configs). A platform-file finding:
// empty TenantID, Field `<file>:<path>`, as platformProblemFindings spells it.
func checkEnforcedGroupBy(enforced *routingpolicy.Enforced, routing map[string]map[string]any, tenants []string) []Finding {
	routed := make([]string, 0, len(tenants))
	for _, t := range tenants {
		if routing[t] != nil {
			routed = append(routed, t)
		}
	}
	var out []Finding
	for _, p := range routingpolicy.EnforcedGroupByInvalid(enforced, routed) {
		out = append(out, Finding{
			Severity: SeverityError,
			Kind:     FindingRoutingGroupByInvalid,
			Field:    p.File + ":" + p.Path(),
			Message:  fmt.Sprintf("%s: %s", p.File, p.Message()),
		})
	}
	return out
}

// checkCriticalEscalation reports ADR-007 `require_critical_escalation`
// (#2325, routingpolicy.CheckCriticalEscalation — the route generator's
// judgement): per requiring domain, an error when severity=critical alerts
// reach no pagerduty receiver, else a warning per non-pagerduty destination
// that still catches some of them first.
func checkCriticalEscalation(tenantID string, routing map[string]any, policies []routingpolicy.Policy) []Finding {
	var out []Finding
	escalation := strings.Join(routingpolicy.EscalationTypes, ", ")
	for _, f := range routingpolicy.CheckCriticalEscalation(tenantID, routing, policies) {
		if !f.Verdict.Compliant() {
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingCriticalEscalationMissing,
				TenantID: tenantID,
				Field:    "receiver.type",
				Message: fmt.Sprintf("tenant %q: domain policy %q requires critical escalation, but severity=critical alerts "+
					"reach no receiver of type %s (main receiver type %q, and no rendered routes entry matches "+
					"severity=critical with such a receiver); add `routes: - match: {severity: critical}` with a "+
					"pagerduty receiver to the tenant's _routing or its routing profile, or switch the main receiver.type to pagerduty",
					tenantID, f.Domain, escalation, routingpolicy.ReceiverType(routing["receiver"])),
			})
			continue
		}
		for _, l := range f.Verdict.Leaks {
			field, msg := l.Ref+".receiver.type", ""
			if l.Ref == routingpolicy.MainReceiverRef {
				field = "receiver.type"
				msg = fmt.Sprintf("tenant %q (domain policy %q): severity=critical alerts that no sub-route catches go to "+
					"the main receiver (type %q), not a receiver of type %s", tenantID, f.Domain, l.ReceiverType, escalation)
			} else {
				msg = fmt.Sprintf("tenant %q (domain policy %q): %s (%s) receiver type %q catches alerts with %s before "+
					"any receiver of type %s does, so they never reach one",
					tenantID, f.Domain, l.Ref, routingpolicy.FormatLabels(l.Match), l.ReceiverType,
					routingpolicy.FormatLabels(l.Caught), escalation)
			}
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     FindingCriticalEscalationLeak,
				TenantID: tenantID,
				Field:    field,
				Message:  msg,
			})
		}
	}
	return out
}

// checkDomainPolicies judges every receiver the resolved routing renders
// (main route, overrides, routes — routingpolicy.Targets) against the
// ADR-007 domain policies that list the tenant. forbidden_receiver_types and
// allowed_receiver_types are independent tests, so one receiver can yield
// two findings (the Python generator's --strict semantics).
func checkDomainPolicies(tenantID string, routing map[string]any, policies []routingpolicy.Policy, prov routingpolicy.Provenance) []Finding {
	if len(policies) == 0 {
		return nil
	}
	var out []Finding
	for _, v := range routingpolicy.CheckReceiverTypes(tenantID, routing, policies) {
		field := v.Target + ".receiver.type"
		if v.Target == "receiver" {
			field = "receiver.type"
		}
		topKey := v.Target
		if i := strings.IndexByte(topKey, '['); i >= 0 {
			topKey = topKey[:i]
		}
		source := routingpolicy.SourceTenant
		if s, ok := prov[topKey]; ok {
			source = s
		}
		verdict := fmt.Sprintf("is forbidden by domain policy %q (forbidden_receiver_types)", v.Domain)
		if v.Constraint == routingpolicy.ConstraintAllowed {
			verdict = fmt.Sprintf("is not in domain policy %q allowed_receiver_types", v.Domain)
		}
		out = append(out, Finding{
			Severity: SeverityError,
			Kind:     FindingDomainPolicyViolation,
			TenantID: tenantID,
			Field:    field,
			Message: fmt.Sprintf("tenant %q: %s receiver type %q %s; %s comes from %s",
				tenantID, v.Target, v.ReceiverType, verdict, topKey, routingpolicy.Describe(source)),
		})
	}
	return out
}

// platformProblemFindings turns what routingpolicy.LoadTree could not use
// into findings with an empty TenantID. An unusable domain policy is an
// error (a policy that is not enforced reads as a clean pass); an unusable
// routing_profiles block is a warning, as in the Python reader; ignored
// `_routing_defaults.routes` is an error, as the generator's --validate;
// routing in a location the generator never reads is an error (#2291): the
// author meant it to route, and nothing is rendered from it. The #2326
// routing-tree shapes are errors: the generator refuses the tree on the
// blocking ones, and an out-of-scope policy entry is not enforced.
func platformProblemFindings(problems []routingpolicy.Problem) []Finding {
	var out []Finding
	for _, p := range problems {
		f := Finding{Severity: SeverityError, Kind: FindingDomainPolicyUnusable, Message: p.Message}
		switch p.Kind {
		case routingpolicy.ProblemRoutingProfilesUnusable:
			f.Severity, f.Kind = SeverityWarn, FindingRoutingProfilesUnusable
		case routingpolicy.ProblemRoutingDefaultsRoutes:
			f.Kind = FindingRoutingDefaultsRoutesIgnored
		case routingpolicy.ProblemRoutingInUnreadLocation:
			f.Kind = FindingRoutingInUnreadLocation
		case routingpolicy.ProblemRoutingEnforcedBelowRoot:
			f.Kind = FindingRoutingEnforcedBelowRoot
		case routingpolicy.ProblemRoutingDefaultsNullBelowRoot:
			f.Kind = FindingRoutingDefaultsNullBelowRoot
		case routingpolicy.ProblemRoutingProfileDuplicate:
			f.Kind = FindingRoutingProfileDuplicate
		case routingpolicy.ProblemDuplicateTenant:
			f.Kind = FindingDuplicateTenant
		case routingpolicy.ProblemDomainPolicyOutOfScope:
			f.Kind = FindingDomainPolicyOutOfScope
		}
		switch {
		case p.File != "" && p.Field != "":
			f.Field = p.File + ":" + p.Field
		case p.File != "":
			f.Field = p.File
		default:
			f.Field = p.Field
		}
		out = append(out, f)
	}
	return out
}

func sortedStringKeys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// checkOneTenantRouting applies all five checks to one tenant's
// `_routing` dict. Helper extracted so unit tests can exercise a
// single tenant without building the full RoutingByTenant map.
func checkOneTenantRouting(tenantID string, routing map[string]any) []Finding {
	var out []Finding

	mainReceiver, _ := routing["receiver"].(map[string]any)
	mainSig := receiverSignature(mainReceiver)

	// Checks 1 + 2 against the main receiver. nil main receiver is
	// handled here (not earlier) so the finding still references
	// the right `receiver` field path.
	out = append(out, checkReceiverShape(tenantID, "receiver", routing["receiver"])...)

	// ADR-007 label-match `routes` (#2280): checked whether or not the
	// tenant has overrides — they used to sit behind an "no overrides,
	// return" and were never looked at.
	out = append(out, checkRoutes(tenantID, routing)...)

	// Checks 3 + 4 + 5 walk the overrides list. Missing overrides
	// list is fine — most tenants have only the main receiver.
	overrides, _ := routing["overrides"].([]any)

	// Stable order: preserve the YAML list order but track
	// duplicate signatures by canonical hash.
	seenMatcher := make(map[string]int) // matcher hash → first index
	for i, raw := range overrides {
		fieldPath := fmt.Sprintf("overrides[%d]", i)
		ov, ok := raw.(map[string]any)
		if !ok {
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingMissingReceiverField,
				TenantID: tenantID,
				Field:    fieldPath,
				Message: fmt.Sprintf(
					"tenant %q: %s is not an object (got %T); expected a map with matcher keys + receiver",
					tenantID, fieldPath, raw),
			})
			continue
		}

		// Check 3: matcher contract — the generator's
		// _validate_override_matcher requires EXACTLY ONE of
		// alertname / metric_group, treating an absent key OR an
		// empty-string value as "not set". We mirror that truthiness
		// so the guard blocks the two override shapes the generator
		// silently discards (both-set and neither-set), instead of
		// letting them merge as a dead no-op.
		hasAlertname := matcherValuePresent(ov["alertname"])
		hasMetricGroup := matcherValuePresent(ov["metric_group"])

		switch {
		case hasAlertname && hasMetricGroup:
			// Both set → the generator requires exactly one and skips
			// the override entirely, so it silently never fires.
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingConflictingOverrideMatcher,
				TenantID: tenantID,
				Field:    fieldPath,
				Message: fmt.Sprintf(
					"tenant %q: %s sets both alertname and metric_group (exactly one required); the route generator skips this override, so it never takes effect",
					tenantID, fieldPath),
			})
			// No duplicate check: a skipped override yields no matcher.
		case !hasAlertname && !hasMetricGroup:
			// Neither set → an empty matcher would shadow ALL alerts
			// for the tenant (and the generator skips it anyway).
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingEmptyOverrideMatcher,
				TenantID: tenantID,
				Field:    fieldPath,
				Message: fmt.Sprintf(
					"tenant %q: %s has no matcher field (needs exactly one of alertname, metric_group; empty-string values count as unset); an empty matcher would shadow ALL alerts for this tenant",
					tenantID, fieldPath),
			})
			// No duplicate check: same as the empty-matcher rationale.
		default:
			// Exactly one set → valid matcher. Check 4: duplicate
			// matcher across overrides (dead-code sub-route).
			matcherFingerprint := canonicalMatcher(ov)
			if first, dup := seenMatcher[matcherFingerprint]; dup {
				out = append(out, Finding{
					Severity: SeverityWarn,
					Kind:     FindingDuplicateOverrideMatcher,
					TenantID: tenantID,
					Field:    fieldPath,
					Message: fmt.Sprintf(
						"tenant %q: %s shares the same matcher with overrides[%d]; the first override wins and this one is dead code",
						tenantID, fieldPath, first),
				})
			} else {
				seenMatcher[matcherFingerprint] = i
			}
		}

		// Checks 1 + 2 against the override's receiver.
		out = append(out, checkReceiverShape(tenantID, fieldPath+".receiver", ov["receiver"])...)
		ovReceiver, _ := ov["receiver"].(map[string]any)

		// Check 5: redundant override receiver vs main.
		if mainSig != "" && receiverSignature(ovReceiver) == mainSig {
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     FindingRedundantOverrideReceiver,
				TenantID: tenantID,
				Field:    fieldPath + ".receiver",
				Message: fmt.Sprintf(
					"tenant %q: %s.receiver is structurally identical to the main receiver; the override has no routing effect",
					tenantID, fieldPath),
			})
		}
	}
	return out
}

// checkRoutes walks the ADR-007 `routes` list of a resolved routing. An
// entry the route generator would skip (routingpolicy.RouteEntryProblem, the
// one predicate shared with the Python generator's route_entry_matchers) is
// invalid_route_entry and nothing else; a renderable entry gets the same
// receiver checks (1 + 2) as the main receiver and the overrides. Duplicate
// matches are not reported.
func checkRoutes(tenantID string, routing map[string]any) []Finding {
	raw, present := routing["routes"]
	if !present || raw == nil {
		return nil
	}
	routes, ok := raw.([]any)
	if !ok {
		return []Finding{{
			Severity: SeverityError,
			Kind:     FindingInvalidRouteEntry,
			TenantID: tenantID,
			Field:    "routes",
			Message: fmt.Sprintf(
				"tenant %q: routes must be a list of {match, receiver} entries (got %T); the route generator renders none of it",
				tenantID, raw),
		}}
	}
	var out []Finding
	for i, entry := range routes {
		fieldPath := fmt.Sprintf("routes[%d]", i)
		if reason, bad := routingpolicy.RouteEntryProblem(entry); bad {
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingInvalidRouteEntry,
				TenantID: tenantID,
				Field:    fieldPath,
				Message: fmt.Sprintf("tenant %q: %s %s; the route generator skips this entry",
					tenantID, fieldPath, reason),
			})
			continue
		}
		m, _ := entry.(map[string]any)
		out = append(out, checkReceiverShape(tenantID, fieldPath+".receiver", m["receiver"])...)
	}
	return out
}

// checkReceiverShape applies checks 1 + 2 to one receiver dict: the
// receiver contract in pkg/receiverspec (#2295; shared with tenant-api and
// pkg/config), rendered as findings. A nil/missing receiver, a missing or
// unknown type are one finding each; every other problem (missing,
// malformed or conflicting fields, optional boolean values, http_config)
// is one finding per field. The receiver is handed over as decoded: a
// mapping with a key PyYAML reads as a non-string (`1:`) is map[any]any,
// which receiverspec.Check takes as the mapping it is (#2295).
func checkReceiverShape(tenantID, fieldPath string, receiver any) []Finding {
	problems := receiverspec.Check(receiver)
	out := make([]Finding, 0, len(problems))
	for _, p := range problems {
		field := fieldPath
		if p.Field != "" {
			field = fieldPath + "." + p.Field
		}
		out = append(out, Finding{
			Severity: SeverityError,
			Kind:     receiverFindingKind[p.Kind],
			TenantID: tenantID,
			Field:    field,
			Message:  fmt.Sprintf("tenant %q: %s: %s", tenantID, fieldPath, p.Message),
		})
	}
	return out
}

// receiverFindingKind maps receiverspec problem kinds onto the da-guard
// finding kinds, which are the JSON contract and predate the package.
var receiverFindingKind = map[receiverspec.Kind]FindingKind{
	receiverspec.KindNotObject:   FindingMissingReceiverField,
	receiverspec.KindMissingType: FindingMissingReceiverField,
	receiverspec.KindMissing:     FindingMissingReceiverField,
	receiverspec.KindUnknownType: FindingUnknownReceiverType,
	receiverspec.KindInvalid:     FindingInvalidReceiverField,
	receiverspec.KindConflicting: FindingConflictingReceiverField,
}

// matcherValuePresent reports whether an override matcher value
// counts as "set". It mirrors the generator's truthiness test in
// _grar_routes.py::_validate_override_matcher
// (`"alertname" in override and override["alertname"]`): an absent
// key, a nil value, or an EMPTY STRING all read as unset, so
// `alertname: ""` is indistinguishable from a missing alertname —
// the generator would build no matcher from it and skip the override.
//
// Matcher values are strings in every shipping config; a non-string,
// non-nil scalar is treated as present so the guard stays at least as
// strict as the generator's Python truthiness (both-set / neither-set
// detection can't be fooled by an odd value type).
func matcherValuePresent(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case string:
		return t != ""
	default:
		return true
	}
}

// canonicalMatcher reduces an override entry to a stable fingerprint
// of its matcher keys + values, ignoring receiver / timing config and
// any empty-string matcher value (per matcherValuePresent). Used only
// by the duplicate-matcher check now that the empty / conflicting
// checks in checkOneTenantRouting test key presence directly. Returns
// "" when no matcher key carries a non-empty value.
func canonicalMatcher(ov map[string]any) string {
	subset := make(map[string]any)
	for k := range matcherKeys {
		if matcherValuePresent(ov[k]) {
			subset[k] = ov[k]
		}
	}
	if len(subset) == 0 {
		return ""
	}
	// encoding/json sorts map keys alphabetically — gives a stable
	// canonical form regardless of how the YAML was authored.
	b, err := json.Marshal(subset)
	if err != nil {
		// Defensive: any value we got from a yaml.v3 unmarshal is by
		// construction JSON-marshalable (string keys, scalar leaves).
		// json.Marshal can only fail here if the caller fed us a map
		// with non-string keys (e.g. yaml.v2's map[any]any) — a caller
		// violation, not a config error.
		//
		// LIMITATION: when this fires, two different malformed
		// overrides will both fall back to the same error string and
		// trip the duplicate-matcher check (false positive warning).
		// We accept that over panicking or silently dropping the
		// override — a noisy false positive is the right failure mode
		// for "your input shape is wrong". Caller's responsibility to
		// supply yaml.v3-style maps; CLI wrapper (PR-4) will enforce.
		return fmt.Sprintf("err:%v", err)
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// receiverSignature is the structural fingerprint used by check 5
// (redundant override receiver). Same canonicalisation as
// canonicalMatcher: sort keys via encoding/json, hash. Returns ""
// when receiver is nil so check 5 is skipped (the
// missing-receiver finding from check 1 covers that path).
//
// Same json.Marshal collision limitation as canonicalMatcher: a
// caller-supplied map[any]any value would trip the err-fallback
// path, and two such broken receivers would falsely sig-match. See
// canonicalMatcher's comment for the rationale.
func receiverSignature(receiver map[string]any) string {
	if receiver == nil {
		return ""
	}
	b, err := json.Marshal(receiver)
	if err != nil {
		return fmt.Sprintf("err:%v", err)
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
