package gitops

// Receiver shape pre-flight for a whole-document tenant write (#2295).
//
// The receivers a body writes in `_routing` are judged by pkg/receiverspec —
// the Go copy of the receiver contract da-guard uses, pinned to
// tenant-config.schema.json and, through the shared case table, to the Python
// route generator and Alertmanager. PUT /tenants/{id} runs it (the handler
// calls ReceiverPreflight, in both write modes) and so do both dry-runs
// behind POST /tenants/{id}/validate (dryRunPreflight), so the two give the
// same verdict on the same body.
//
// #2431: the same pre-flight refuses a matcher value the body writes in
// `_routing` — `routes[i].match.<label>`, `overrides[i].alertname` /
// `metric_group` — that the route generator's PyYAML does not read as a
// string (`yes`, `1:30`, `~`, a `!!int` tag): routingpolicy.ValuesNotString,
// the predicate da-guard uses, over the body's own `_routing` only (never a
// value inherited from `_routing_defaults` or a routing profile).
//
// #2503: likewise a `group_by` element the body writes (main route,
// `overrides[i]`, `routes[i]`) that is not a string as PyYAML reads it
// (`8`, `on`), is empty, repeats a label or is `...` beside other labels:
// routingpolicy.GroupByInvalid, da-guard's routing_group_by_invalid. A batch
// patch cannot write a group_by (its values are scalars, and a scalar over
// the structured `_routing` is refused), so batch has nothing to judge.
//
// #2341: and a `_routing` the body writes that is neither a mapping nor a
// disabling string (`"slack"`, a list, null, an unquoted `false` / `off`,
// which PyYAML reads as a boolean) — routingpolicy.RoutingNotMapping,
// da-guard's routing_not_mapping.
//
// ⛔ NOT IN validateBodyOnly, AND NOT IN THE WRITER'S write(). Both also serve
// writes that change only another part of the tenant file: a batch op's
// MERGED document (readMergeBodyOnly) and the custom-alerts PUT (write() via
// WriteIfUnchanged) carry the whole file, so a receiver already broken on disk
// would refuse a write that never touched it. Only a body the author wrote in
// full — PUT /tenants/{id} — is judged.

import (
	"fmt"
	"strings"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/pyyamlcompat"
	"github.com/vencil/threshold-exporter/pkg/receiverspec"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
	"gopkg.in/yaml.v3"
)

// ReceiverViolation is one receiver problem, one matcher value that is
// not a string (#2431), or one bad group_by element (#2503): Field is the
// document path
// (`tenants.<id>._routing.receiver.url`, `tenants.<id>._routing.routes[0].
// match.team`), Reason the receiverspec / routingpolicy message.
type ReceiverViolation struct {
	Field  string
	Reason string
}

// ReceiverShapeError is ReceiverPreflight's refusal. It unwraps to
// ErrValidation; the PUT handler reads Violations for its INVALID_BODY
// response.
type ReceiverShapeError struct {
	Violations []ReceiverViolation
}

func (e *ReceiverShapeError) Error() string {
	return fmt.Sprintf("%v: %s", ErrValidation, strings.Join(e.lines(), "; "))
}

// Unwrap makes errors.Is(err, ErrValidation) hold.
func (e *ReceiverShapeError) Unwrap() error { return ErrValidation }

func (e *ReceiverShapeError) lines() []string {
	out := make([]string, len(e.Violations))
	for i, v := range e.Violations {
		out[i] = v.Field + ": " + v.Reason
	}
	return out
}

// ReceiverPreflight judges the receivers and matcher values a PUT
// /tenants/{id} body writes. A
// body the Writer's own pre-flight would refuse (over the size cap, bad YAML,
// a missing tenant section) passes here, so the Writer answers it as before.
func ReceiverPreflight(tenantID, yamlContent string) error {
	if v := receiverViolations(tenantID, yamlContent); len(v) > 0 {
		return &ReceiverShapeError{Violations: v}
	}
	return nil
}

// dryRunPreflight is the dry-runs' pre-flight: the Writer's validateBodyOnly,
// then — once the body is well-formed — what ReceiverPreflight judges.
func dryRunPreflight(tenantID, yamlContent string) []string {
	if errs := validateBodyOnly(tenantID, yamlContent); len(errs) > 0 {
		return errs
	}
	if v := receiverViolations(tenantID, yamlContent); len(v) > 0 {
		return (&ReceiverShapeError{Violations: v}).lines()
	}
	return nil
}

// receiverViolations returns one violation per receiverspec problem of every
// receiver the tenant block writes in `_routing`: the main `receiver`,
// `overrides[i].receiver` and `routes[i].receiver`.
//
// Only what the body writes is judged. A receiver the tenant inherits from
// `_routing_defaults` or a routing profile is not the author's to fix here
// (da-guard judges the resolved routing). A `receiver` key the body leaves
// out or writes as null is not judged here either. That is a gap, not a
// verdict: an override or a routes entry without a receiver of its own is
// skipped by the route generator and reported by da-guard (the override is
// not routed to the main receiver). A `_routing` that is neither a mapping
// nor a disabling string is one violation and nothing else is judged
// (#2341, routingpolicy.RoutingNotMapping). Other shapes that are not a
// receiver's (a routes entry that is not a mapping) are left to the checks
// that own them. Known limitation: nothing else in the tenant block
// (other sections that end up in Alertmanager's config) is judged here.
func receiverViolations(tenantID, yamlContent string) []ReceiverViolation {
	// #1722's pre-parse size gate, first: an oversize body is the Writer's
	// to refuse (validateShape answers it with a length compare), and the
	// parses below are the cost that gate bounds — so it is not parsed here.
	if len(CheckTenantDocSize(yamlContent)) > 0 {
		return nil
	}
	var doc struct {
		Tenants map[string]map[string]any `yaml:"tenants"`
	}
	if yaml.Unmarshal([]byte(yamlContent), &doc) != nil || pyyamlcompat.FindDuplicateKeyIn([]byte(yamlContent)) != nil {
		return nil // validateBodyOnly owns every decode error, a repeated key included
	}
	raw, written := doc.Tenants[tenantID]["_routing"]
	if !written {
		return nil
	}
	// #2295 / #2431: the receivers and matcher values as the route
	// generator's PyYAML reads them — plain `on` a boolean, quoted "on" a
	// string (yaml.v3 alone makes both the string "on"). ⛔ Fail-closed: a
	// value with no PyYAML counterpart (the tenant or its `_routing` not
	// found there) is routingpolicy.Unmatched / UnmatchedValue and refused,
	// never the yaml.v3 value.
	py := routingpolicy.PyYAMLRoutingByTenant([]byte(yamlContent))[tenantID]
	base := fmt.Sprintf("tenants.%s._routing", tenantID)
	// #2341 R5: a `_routing` that is neither a mapping nor a disabling string
	// (as PyYAML reads it: an unquoted `off` is a boolean) renders no route
	// at all; nothing below applies to it.
	if v := routingpolicy.WithPyYAMLRouting(raw, py); routingpolicy.RoutingNotMapping(v) {
		return []ReceiverViolation{{Field: base, Reason: routingpolicy.RoutingNotMappingMessage(v)}}
	}
	// Keys made strings as the exporter's merge does, so a `1:` or `~:` key
	// beside the receiver leaves `_routing` a mapping (da-guard reads it so).
	routing, ok := cfg.NormalizeYAMLToJSON(raw).(map[string]any)
	if !ok {
		return nil
	}
	routing, _ = routingpolicy.WithPyYAMLRouting(routing, py).(map[string]any)
	var out []ReceiverViolation
	check := func(path string, holder map[string]any) {
		recv, present := holder["receiver"]
		if !present || recv == nil {
			return
		}
		for _, p := range receiverspec.Check(recv) {
			field := path + ".receiver"
			if p.Field != "" {
				field += "." + p.Field
			}
			out = append(out, ReceiverViolation{Field: field, Reason: p.Message})
		}
	}
	check(base, routing)
	for _, list := range []string{"overrides", "routes"} {
		entries, _ := routing[list].([]any)
		for i, e := range entries {
			if m, ok := e.(map[string]any); ok {
				check(fmt.Sprintf("%s.%s[%d]", base, list, i), m)
			}
		}
	}
	for _, v := range routingpolicy.ValuesNotString(routing) {
		out = append(out, ReceiverViolation{Field: base + "." + v.Field, Reason: v.Message()})
	}
	// #2503: after `{{tenant}}` substitution, as the generator renders it.
	for _, p := range routingpolicy.GroupByInvalidForTenant(tenantID, routing) {
		out = append(out, ReceiverViolation{Field: base + "." + p.Field, Reason: p.Message()})
	}
	return out
}
