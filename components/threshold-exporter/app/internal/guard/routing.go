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
//      error too: Alertmanager would reject the whole config.
//      Contract per type: receiverTypeSpecs, pinned to the hub
//      docs/schemas/tenant-config.schema.json (see its comment). Same
//      checks for receivers embedded in overrides.
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
//     warn, TenantID ""). Only the checks that need such a file are
//     skipped; the run and its exit code are otherwise unchanged (#1654).
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
	"regexp"
	"slices"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
)

// receiverTypeSpec is the field-presence contract of one receiver
// type, in the same two concepts every copy of it uses:
//
//   - Required:     every field must be set.
//   - ExactlyOneOf: for each group, exactly one field must be set
//     (both set and none set are errors).
//
// The same contract is declared in docs/schemas/tenant-config.schema.json
// (`required` + a `oneOf` whose branches each require one field) and in
// scripts/tools/_lib_constants.py::RECEIVER_TYPES (`required` +
// `exactly_one_of`). The schema is the hub: TestReceiverTypeSpecs_MatchSchema
// parses it and fails on any difference with this map, and
// tests/shared/test_receiver_spec_parity.py pins the Python copy to the
// same schema — so no copy is read out of another language's source text.
//
// Required fields also carry the schema's value shape (#2180), because
// Alertmanager rejects the whole config over one bad value:
//
//   - Patterns:    required string fields whose schema property has a
//     `pattern` (the URL / smarthost formats), copied verbatim.
//   - StringLists: required fields the schema types as an array of
//     non-empty strings (email `to`).
//
// TestReceiverTypeSpecs_MatchSchema pins both to the schema as well. Other
// optional fields and value shapes stay with the schema and
// config_resolve.go.
type receiverTypeSpec struct {
	Required     []string
	ExactlyOneOf [][]string
	Patterns     map[string]string
	StringLists  []string
}

// Copies of the two `pattern`s in tenant-config.schema.json
// (definitions.receiverHttpUrl / receiverSmtpHostPort), which hold the
// only authored copy and the reasoning; the Go binary cannot read the
// schema at run time. TestReceiverTypeSpecs_MatchSchema fails on any
// difference.
const (
	receiverHTTPURLPattern      = `^[Hh][Tt][Tt][Pp][Ss]?://(([A-Za-z0-9._~!$&'()*+,;=:-]|%[0-9A-Fa-f]{2})+@)?(([A-Za-z0-9._~!$&'()*+,;=<>"-]|[^\x00-\x7f]|%(25|[89A-Fa-f][0-9A-Fa-f]))+|\[[0-9A-Fa-f:.]+\])(:[0-9]*)?(/([^\x00-\x20\x7f%?#]|%[0-9A-Fa-f]{2})*)?(\?[^\x00-\x20\x7f#]*)?(#([^\x00-\x20\x7f%]|%[0-9A-Fa-f]{2})*)?$`
	receiverSMTPHostPortPattern = `^([^\x00-\x20\x7f:/?#@\[\]\\]+|\[[0-9A-Fa-f:.]+\]):[0-9]+$`
)

var receiverTypeSpecs = map[string]receiverTypeSpec{
	"webhook": {Required: []string{"url"}, Patterns: map[string]string{"url": receiverHTTPURLPattern}},
	"email": {
		Required:    []string{"to", "smarthost", "from"},
		Patterns:    map[string]string{"smarthost": receiverSMTPHostPortPattern},
		StringLists: []string{"to"},
	},
	"slack":      {Required: []string{"api_url"}, Patterns: map[string]string{"api_url": receiverHTTPURLPattern}},
	"teams":      {Required: []string{"webhook_url"}, Patterns: map[string]string{"webhook_url": receiverHTTPURLPattern}},
	"rocketchat": {Required: []string{"url"}, Patterns: map[string]string{"url": receiverHTTPURLPattern}},
	// Alertmanager accepts both keys at once but then uses the Events
	// API v1 (service_key) and silently ignores routing_key
	// (notify/pagerduty/pagerduty.go), so both-set is rejected too.
	"pagerduty": {ExactlyOneOf: [][]string{{"service_key", "routing_key"}}},
}

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
// No-op when input.RoutingByTenant is empty — absent routing is a
// valid configuration (some tenants intentionally disable
// alerting), and silence here matches that intent.
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
	if len(input.RoutingByTenant) == 0 {
		return out
	}
	tenants := make([]string, 0, len(input.RoutingByTenant))
	for t := range input.RoutingByTenant {
		tenants = append(tenants, t)
	}
	sort.Strings(tenants)

	for _, tenantID := range tenants {
		routing := input.RoutingByTenant[tenantID]
		if routing == nil {
			continue
		}
		out = append(out, checkOneTenantRouting(tenantID, routing)...)
		out = append(out, checkDomainPolicies(tenantID, routing, input.DomainPolicies, input.RoutingProvenance[tenantID])...)
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

// platformProblemFindings turns what routingpolicy.LoadRoot could not use
// into findings with an empty TenantID. An unusable domain policy is an
// error (a policy that is not enforced reads as a clean pass); an unusable
// routing_profiles block is a warning, as in the Python reader.
func platformProblemFindings(problems []routingpolicy.Problem) []Finding {
	var out []Finding
	for _, p := range problems {
		f := Finding{Severity: SeverityError, Kind: FindingDomainPolicyUnusable, Message: p.Message}
		if p.Kind == routingpolicy.ProblemRoutingProfilesUnusable {
			f.Severity, f.Kind = SeverityWarn, FindingRoutingProfilesUnusable
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
	out = append(out, checkReceiverShape(tenantID, "receiver", mainReceiver)...)

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
		ovReceiver, _ := ov["receiver"].(map[string]any)
		out = append(out, checkReceiverShape(tenantID, fieldPath+".receiver", ovReceiver)...)

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
		recv, _ := m["receiver"].(map[string]any)
		out = append(out, checkReceiverShape(tenantID, fieldPath+".receiver", recv)...)
	}
	return out
}

// checkReceiverShape applies checks 1 + 2 to one receiver dict. A
// nil/missing receiver is its own error; a bad type is one error;
// missing required fields are one error each.
func checkReceiverShape(tenantID, fieldPath string, receiver map[string]any) []Finding {
	if receiver == nil {
		return []Finding{{
			Severity: SeverityError,
			Kind:     FindingMissingReceiverField,
			TenantID: tenantID,
			Field:    fieldPath,
			Message: fmt.Sprintf(
				"tenant %q: %s is missing or not an object; routing requires a receiver dict with `type`",
				tenantID, fieldPath),
		}}
	}

	rtype, _ := receiver["type"].(string)
	if rtype == "" {
		return []Finding{{
			Severity: SeverityError,
			Kind:     FindingMissingReceiverField,
			TenantID: tenantID,
			Field:    fieldPath + ".type",
			Message: fmt.Sprintf(
				"tenant %q: %s.type is missing or empty; receivers must declare a type",
				tenantID, fieldPath),
		}}
	}

	spec, known := receiverTypeSpecs[rtype]
	if !known {
		return []Finding{{
			Severity: SeverityError,
			Kind:     FindingUnknownReceiverType,
			TenantID: tenantID,
			Field:    fieldPath + ".type",
			Message: fmt.Sprintf(
				"tenant %q: %s.type=%q is not a supported receiver type (supported: %s)",
				tenantID, fieldPath, rtype, supportedTypeList()),
		}}
	}

	var out []Finding
	for _, field := range spec.Required {
		if f, bad := requiredFieldFinding(tenantID, fieldPath, rtype, spec, receiver, field); bad {
			out = append(out, f)
		}
	}
	for _, group := range spec.ExactlyOneOf {
		if f, bad := exactlyOneFinding(tenantID, fieldPath, rtype, receiver, group); bad {
			out = append(out, f)
		}
	}
	return out
}

// receiverFieldRegexps holds the compiled spec.Patterns, keyed by the
// pattern text (MustCompile: a bad copy fails at init, and the parity
// test pins the copies to the schema).
var receiverFieldRegexps = func() map[string]*regexp.Regexp {
	out := map[string]*regexp.Regexp{}
	for _, spec := range receiverTypeSpecs {
		for _, p := range spec.Patterns {
			if _, ok := out[p]; !ok {
				out[p] = regexp.MustCompile(p)
			}
		}
	}
	return out
}()

// requiredFieldFinding checks one Required field by the schema's type
// (#2180), the same rule as _lib_validation.receiver_required_problem on
// the Python side; cases in testdata/receiver_presence_cases.json.
//
//   - nil (absent, or a YAML key with no value) or "" is missing, as in
//     Alertmanager, whose config decodes both to the zero value.
//   - A string must match the field's Patterns entry, if any.
//   - A StringLists field given as a list needs at least one item, each a
//     non-empty string: the pipeline joins it into Alertmanager's `to`
//     string, where [""] reads as no address. A plain string is still
//     taken — Alertmanager's own `to` is a string.
//   - Any other type is an error. For some (email from: 0) that is
//     stricter than Alertmanager, which renders them as text; for most
//     (a list, or url: 0) Alertmanager rejects the config.
func requiredFieldFinding(tenantID, fieldPath, rtype string, spec receiverTypeSpec, receiver map[string]any, field string) (Finding, bool) {
	missing := func(format string, args ...any) (Finding, bool) {
		return Finding{
			Severity: SeverityError,
			Kind:     FindingMissingReceiverField,
			TenantID: tenantID,
			Field:    fieldPath + "." + field,
			Message:  fmt.Sprintf("tenant %q: receiver type %q ", tenantID, rtype) + fmt.Sprintf(format, args...),
		}, true
	}
	invalid := func(format string, args ...any) (Finding, bool) {
		return Finding{
			Severity: SeverityError,
			Kind:     FindingInvalidReceiverField,
			TenantID: tenantID,
			Field:    fieldPath + "." + field,
			Message: fmt.Sprintf("tenant %q: receiver type %q field %q ", tenantID, rtype, field) +
				fmt.Sprintf(format, args...),
		}, true
	}
	switch v := receiver[field].(type) {
	case nil:
		return missing("requires field %q", field)
	case string:
		if v == "" {
			return missing("field %q is present but empty string", field)
		}
		if p, ok := spec.Patterns[field]; ok && !receiverFieldRegexps[p].MatchString(v) {
			return invalid("value %q is not in the format tenant-config.schema.json requires", v)
		}
		return Finding{}, false
	case []any:
		if !slices.Contains(spec.StringLists, field) {
			return invalid("must be a string, got %T", v)
		}
		if len(v) == 0 {
			return missing("field %q is present but an empty list", field)
		}
		for i, item := range v {
			if s, ok := item.(string); !ok || s == "" {
				return invalid("item %d must be a non-empty string, got %#v", i, item)
			}
		}
		return Finding{}, false
	default:
		return invalid("must be a string, got %T", v)
	}
}

// exactlyOneFinding checks one ExactlyOneOf group, by the schema's type
// rule (tenant-config.schema.json: `type: ["string", "null"]`), the same
// rule as _lib_validation.receiver_field_state on the Python side; cases
// in testdata/receiver_presence_cases.json. A non-string value is an
// error — stricter than Alertmanager on purpose: it renders 0 as "0" but
// fails to load a list, so no "counts as given" rule fits all of them.
// "Exactly one" is judged only once every field's type is valid.
func exactlyOneFinding(tenantID, fieldPath, rtype string, receiver map[string]any, group []string) (Finding, bool) {
	var set []string
	for _, field := range group {
		switch v := receiver[field].(type) {
		case nil:
		case string:
			if v != "" {
				set = append(set, field)
			}
		default:
			return Finding{
				Severity: SeverityError,
				Kind:     FindingInvalidReceiverField,
				TenantID: tenantID,
				Field:    fieldPath + "." + field,
				Message: fmt.Sprintf(
					"tenant %q: receiver type %q field %q must be a string, got %T",
					tenantID, rtype, field, v),
			}, true
		}
	}
	if len(set) == 1 {
		return Finding{}, false
	}
	quoted := make([]string, len(group))
	for i, field := range group {
		quoted[i] = fmt.Sprintf("%q", field)
	}
	names := strings.Join(quoted, ", ")
	if len(set) == 0 {
		return Finding{
			Severity: SeverityError,
			Kind:     FindingMissingReceiverField,
			TenantID: tenantID,
			Field:    fieldPath + "." + group[0],
			Message: fmt.Sprintf(
				"tenant %q: receiver type %q requires exactly one of %s; none is set",
				tenantID, rtype, names),
		}, true
	}
	msg := fmt.Sprintf(
		"tenant %q: receiver type %q requires exactly one of %s; %d are set",
		tenantID, rtype, names, len(set))
	if rtype == "pagerduty" {
		msg += " (Alertmanager would use the Events API v1 via service_key and silently ignore routing_key)"
	}
	return Finding{
		Severity: SeverityError,
		Kind:     FindingConflictingReceiverField,
		TenantID: tenantID,
		Field:    fieldPath + "." + set[len(set)-1],
		Message:  msg,
	}, true
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

// supportedTypeList returns the receiver types in alphabetical
// order, suitable for embedding in a finding Message.
func supportedTypeList() string {
	keys := make([]string, 0, len(receiverTypeSpecs))
	for k := range receiverTypeSpecs {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := ""
	for i, k := range keys {
		if i > 0 {
			out += ", "
		}
		out += k
	}
	return out
}
