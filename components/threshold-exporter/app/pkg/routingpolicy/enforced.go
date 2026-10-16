package routingpolicy

// enforced.go — the one piece of `_routing_enforced` (the NOC layer) this
// package models: the group_by of the enforced route(s) the route generator
// renders (#2503). Python twin: _grar_parse._parse_platform_config (which
// block is read) and _grar_routes.enforced_group_by_problems (what is judged).
// Of `match` only its shape is read (#2989): an enabled block whose `match`
// is not a list of non-empty strings is not enabled (ProblemRoutingEnforcedMatchInvalid), as
// _grar_parse._parse_platform_config refuses it
// (_grar_routes.enforced_match_invalid). Nothing else of the block — the
// matchers themselves, timing, the `--policy` domain allowlist — is read
// here; tests/shared/routing_policy_parity_matrix.json
// (`enforced_group_by_invalid` and the `platform` rows) pins both languages.

import (
	"fmt"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/pyyamlcompat"
	"github.com/vencil/threshold-exporter/pkg/receiverspec"
	"gopkg.in/yaml.v3"
)

// EnforcedContext is the context the generator names an enforced route by.
const EnforcedContext = "_routing_enforced"

// ProblemRoutingEnforcedMatchInvalid (#2989): a root `_routing_enforced` that
// is enabled but whose `match` is written and is not a list of non-empty
// strings — the `routes[i].match` spelling `{severity: critical}`, a string, a
// number, or a list holding 1, ~, a mapping or an empty string. The route
// generator takes `match` only as a list of matcher strings and used to drop
// a non-list (the NOC route received every alert) and crash on a bad
// element; it now renders no enforced route from that block (a blocking
// `WARN … skipping`,
// --validate fails) and an earlier root file's block stays, as for a
// non-boolean `enabled`. Not one of IsBlocking's kinds.
const ProblemRoutingEnforcedMatchInvalid = "routing_enforced_match_invalid"

// Enforced is the `_routing_enforced` block the route generator renders
// from: the one of the LAST root platform file, in name order, that carries
// it as a mapping with `enabled: true`. A later file whose block is disabled,
// not a boolean or not a mapping leaves an earlier one in place, as
// _parse_platform_config does (it only ever assigns on `enabled is True`).
type Enforced struct {
	// File is that root file's name.
	File string
	// Value is the block as the generator's PyYAML reads it
	// (pyyamlcompat.Decode): `enabled: yes` is true there, a plain `on` in
	// group_by a boolean. Never the yaml.v3 reading, so nothing is judged as
	// a value the generator does not see.
	Value any
}

// enforcedFrom is the root file top's `_routing_enforced` when the generator
// enables it (merge keys expanded, as safe_load expands them), else nil. An
// enabled block whose `match` is not a list of non-empty strings is not
// enabled; it is reported
// instead (ProblemRoutingEnforcedMatchInvalid, #2989).
func enforcedFrom(name string, top *yaml.Node) (*Enforced, []Problem) {
	n := lookup(top, "_routing_enforced")
	if n == nil {
		return nil, nil
	}
	v := pyyamlcompat.Decode(n)
	if !isMapping(v) {
		return nil, nil
	}
	if enabled, _ := stringKey(v, "enabled"); enabled != true {
		return nil, nil
	}
	if m, _ := stringKey(v, "match"); enforcedMatchInvalid(m) {
		return nil, []Problem{{Kind: ProblemRoutingEnforcedMatchInvalid, File: name,
			Field: EnforcedContext + ".match",
			Message: fmt.Sprintf("%s: _routing_enforced.match must be a list of Alertmanager matcher "+
				"strings, each non-empty (e.g. ['severity=\"critical\"']), got %s; the route generator "+
				"renders no platform-enforced (NOC) route from this block",
				name, enforcedMatchShape(m))}}
	}
	return &Enforced{File: name, Value: v}, nil
}

// enforcedMatchInvalid is _grar_routes.enforced_match_invalid on the value of
// `match` (as PyYAML reads it): written (absent and null both mean "no
// matcher", and so does `[]`) and not a list of non-empty strings — a
// mapping, a scalar, or a list with an element such as `1`, `~`, `yes` (a
// boolean to PyYAML), `{severity: critical}`, or a string that is empty or
// only whitespace (Python's str.strip(): unicode.IsSpace plus U+001C–U+001F,
// which str.isspace() also counts).
func enforcedMatchInvalid(m any) bool {
	if m == nil {
		return false
	}
	list, isList := m.([]any)
	if !isList {
		return true
	}
	for _, e := range list {
		if s, ok := e.(string); !ok || strings.TrimFunc(s, pyIsSpace) == "" {
			return true
		}
	}
	return false
}

// enforcedMatchShape names what `match` is, for the refusal message.
func enforcedMatchShape(m any) string {
	if _, isList := m.([]any); isList {
		return "a list with an element that is not a non-empty string"
	}
	return typeName(m)
}

// EnforcedGroupByProblem is one bad group_by element of a rendered enforced
// route. Context is EnforcedContext, or `_routing_enforced (<tenant>)` for
// the `{{tenant}}` shape (Tenant set); Field is `group_by[i]`.
type EnforcedGroupByProblem struct {
	File    string
	Context string
	Tenant  string
	GroupByProblem
}

// Path is the element's path for a finding: Context + "." + Field.
func (p EnforcedGroupByProblem) Path() string { return p.Context + "." + p.Field }

// Message is the operator-facing refusal, the generator's --strict line
// without its prefix: `<context>: <group_by_problem_text>`.
func (p EnforcedGroupByProblem) Message() string {
	return p.Context + ": " + p.GroupByProblem.Message()
}

// EnforcedGroupByInvalid is _grar_routes.enforced_group_by_problems: the bad
// group_by elements of the enforced route(s) the generator renders from e
// (nil: none). Nothing is judged without a truthy `receiver`; the
// `{{tenant}}` shape (the placeholder in any string value) is judged once
// per tenant of tenants, in name order, AFTER substitution — a tenant id can
// repeat a listed label — and the single shape once. A route is judged only
// when it is rendered: group_by a non-empty list and the (substituted)
// receiver accepted by the receiver contract (pkg/receiverspec, the Go copy
// of build_receiver_config's checks). tenants are every tenant the
// generator recognises, routed or not (#2519, its dedup_configs keys —
// _grar_routes.enforced_route_tenants).
func EnforcedGroupByInvalid(e *Enforced, tenants []string) []EnforcedGroupByProblem {
	if e == nil {
		return nil
	}
	if r, _ := stringKey(e.Value, "receiver"); !Truthy(r) {
		return nil
	}
	var out []EnforcedGroupByProblem
	judge := func(ctx, tenant string, cfg any) {
		gb, _ := stringKey(cfg, "group_by")
		list, isList := gb.([]any)
		if !isList || len(list) == 0 {
			return
		}
		if r, _ := stringKey(cfg, "receiver"); len(receiverspec.Check(r)) > 0 {
			return
		}
		_, problems := GroupByProblems("", list)
		for _, p := range problems {
			out = append(out, EnforcedGroupByProblem{File: e.File, Context: ctx, Tenant: tenant, GroupByProblem: p})
		}
	}
	if !containsTenantPlaceholder(e.Value) {
		judge(EnforcedContext, "", e.Value)
		return out
	}
	sorted := append([]string(nil), tenants...)
	sort.Strings(sorted)
	for _, t := range sorted {
		judge(EnforcedContext+" ("+t+")", t, substituteTenant(e.Value, t))
	}
	return out
}

// containsTenantPlaceholder is _grar_merge._contains_tenant_placeholder:
// `{{tenant}}` in any string value (mapping keys are not looked at).
func containsTenantPlaceholder(v any) bool {
	switch t := v.(type) {
	case string:
		return strings.Contains(t, "{{tenant}}")
	case map[string]any:
		for _, e := range t {
			if containsTenantPlaceholder(e) {
				return true
			}
		}
	case map[any]any:
		for _, e := range t {
			if containsTenantPlaceholder(e) {
				return true
			}
		}
	case []any:
		for _, e := range t {
			if containsTenantPlaceholder(e) {
				return true
			}
		}
	}
	return false
}

// isMapping reports whether v is a decoded YAML mapping of either type
// (Python's isinstance(raw, dict)).
func isMapping(v any) bool {
	switch v.(type) {
	case map[string]any, map[any]any:
		return true
	}
	return false
}
