package routingpolicy

// groupby.go — the group_by contract (#2503), the Go copy of
// _grar_validate.group_by_problems / routing_group_by_invalid. Measured
// against Alertmanager v0.34.1 with its default (UTF-8) label validation:
// AM refuses the WHOLE config for an empty label name, a repeated label
// other than `...` (its repeat check skips the wildcard) or `...` mixed with
// labels, and ACCEPTS a YAML `8` / `true`, grouping by a
// label literally named "8" / "true" — not what an unquoted `8` / `on`
// (PyYAML: an int, a bool) was meant to be. The generator refuses these
// under --strict and drops them otherwise; da-guard and tenant-api call
// GroupByInvalid. tests/shared/routing_policy_parity_matrix.json
// (`group_by_invalid`) pins both languages.
//
// ⛔ The values must be the ones PyYAML reads (WithPyYAMLRouting re-reads
// every group_by it judges): yaml.v3 alone reads a plain `on` or `8` as the
// string "on" / "8", which this predicate accepts. No label-name regex
// either: AM's UTF-8 validation takes any non-empty string.

import "fmt"

// GroupByWildcard is Alertmanager's group_by wildcard (group by every label).
const GroupByWildcard = "..."

// Kinds of GroupByProblem, in the order they are judged.
const (
	GroupByNotString     = "not_string"
	GroupByEmpty         = "empty"
	GroupByDuplicate     = "duplicate"
	GroupByWildcardMixed = "wildcard_mixed"
)

// GroupByProblem is one bad group_by element: Field its path in the routing
// (`group_by[1]`, `overrides[0].group_by[2]`), Kind one of the GroupBy*
// kinds, Value what PyYAML read.
type GroupByProblem struct {
	Field string
	Kind  string
	Value any
}

// Message is the operator-facing refusal; the same text as the generator's
// _grar_validate.group_by_problem_text.
func (p GroupByProblem) Message() string {
	switch p.Kind {
	case GroupByNotString:
		return fmt.Sprintf("%s is %s, not a string — quote it in YAML (e.g. \"8\", \"on\") so it is a label name; "+
			"Alertmanager would group by a label named after its text", p.Field, describePy(p.Value))
	case GroupByEmpty:
		return p.Field + " is an empty string — Alertmanager refuses an empty label name; remove it"
	case GroupByDuplicate:
		return fmt.Sprintf("%s repeats label '%s' listed earlier — Alertmanager refuses a repeated non-wildcard group_by label; remove it",
			p.Field, PyStr(p.Value))
	}
	return p.Field + " is '...' alongside other labels — Alertmanager refuses the wildcard mixed with labels; " +
		"keep ['...'] alone or list only the labels"
}

// GroupByProblems judges one group_by list, each element by its original
// index i (Field prefix+`group_by[i]`), in this order: not a string →
// not_string, "" → empty; a non-wildcard string already kept → duplicate
// (AM's repeat check skips `...`: `['...', '...']` loads, and is neither a
// finding nor repaired); every `...` while some other label remains →
// wildcard_mixed. kept is what the generator renders without --strict
// (empty: no group_by); problems are in index order.
func GroupByProblems(prefix string, groupBy []any) (kept []string, problems []GroupByProblem) {
	kinds := make([]string, len(groupBy)) // "" = kept
	seen := map[string]bool{}
	for i, v := range groupBy {
		s, isStr := v.(string)
		switch {
		case !isStr:
			kinds[i] = GroupByNotString
		case s == "":
			kinds[i] = GroupByEmpty
		case seen[s] && s != GroupByWildcard:
			kinds[i] = GroupByDuplicate
		default:
			seen[s] = true
		}
	}
	mixed := seen[GroupByWildcard] && len(seen) > 1
	for i, kind := range kinds {
		if kind == "" && mixed && groupBy[i] == GroupByWildcard {
			kind = GroupByWildcardMixed
		}
		if kind == "" {
			kept = append(kept, groupBy[i].(string))
			continue
		}
		problems = append(problems, GroupByProblem{Field: fmt.Sprintf("%sgroup_by[%d]", prefix, i), Kind: kind, Value: groupBy[i]})
	}
	return kept, problems
}

// GroupByInvalidForTenant is GroupByInvalid over a routing that has NOT been
// through Resolve (tenant-api's PUT body): `{{tenant}}` is first replaced
// by tenantID in every string value, as the generator does to the whole
// merged routing (_grar_merge.merge_routing_with_defaults →
// _substitute_tenant) and Resolve does here (substituteTenant), so a
// placeholder that becomes a listed label is judged a repeat (#2503 round 3).
func GroupByInvalidForTenant(tenantID string, routing any) []GroupByProblem {
	return GroupByInvalid(substituteTenant(routing, tenantID))
}

// GroupByInvalid is THE predicate (#2503) over a tenant's resolved routing:
// the main route's `group_by`, then each mapping entry's of `overrides`,
// then of `routes`, in list order — every list the generator renders from
// it. A group_by that is not a list is not judged (the generator renders
// none). routing must already carry the PyYAML readings (WithPyYAMLRouting).
func GroupByInvalid(routing any) []GroupByProblem {
	r, ok := asStringMap(routing)
	if !ok {
		return nil
	}
	var out []GroupByProblem
	judge := func(prefix string, holder any) {
		gb, _ := stringKey(holder, "group_by")
		if list, isList := gb.([]any); isList {
			_, problems := GroupByProblems(prefix, list)
			out = append(out, problems...)
		}
	}
	judge("", r)
	for _, key := range []string{"overrides", "routes"} {
		entries, _ := r[key].([]any)
		for i, e := range entries {
			judge(fmt.Sprintf("%s[%d].", key, i), e)
		}
	}
	return out
}
