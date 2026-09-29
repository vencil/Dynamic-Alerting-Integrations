package routingpolicy

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
)

// Target is one route of a resolved routing that renders its own
// Alertmanager receiver.
type Target struct {
	// Ref is "receiver" (the tenant's main route), "overrides[<i>]" or
	// "routes[<i>]" — the index into the resolved list.
	Ref      string
	Receiver any
	// Match is the equality match the route adds under the tenant route
	// (#2325, _grar_validate._subroute_match): nil for the main route;
	// `{alertname|metric_group: str(value)}` for an override — the
	// generator formats the value into the matcher, so `alertname: 123` is
	// "123" (PyStr); a `routes` entry's own `match` (RouteEntryProblem
	// already requires string values).
	Match map[string]string
}

// RouteEntryKeys are the keys a `routes` entry may carry
// (_grar_validate.ROUTE_ENTRY_KEYS). Anything else — `continue`,
// `match_re`, `matchers`, a typo — makes the generator skip the entry.
var RouteEntryKeys = []string{"group_by", "group_interval", "group_wait", "match", "receiver", "repeat_interval"}

var labelNameRE = regexp.MustCompile(`^[a-zA-Z_][a-zA-Z0-9_]*$`)

// RouteEntryProblem reports why the generator would skip one `routes` entry,
// one-for-one with _grar_validate.route_entry_matchers: not a mapping, an
// unsupported key, a missing / empty / non-mapping `match`, a label that is
// not a label name, or a value that is not a non-empty string. bad=false: the
// entry renders (its receiver is judged elsewhere).
//
// ⚠️ The YAML reader decides what a "string" is. yaml.v3 reads `yes` as the
// string "yes", PyYAML as True — so `match: {x: yes}` renders here and is
// skipped by the Python generator (a documented row of the parity matrix).
func RouteEntryProblem(entry any) (reason string, bad bool) {
	var keys []string
	var get func(string) any
	switch t := entry.(type) {
	case map[string]any:
		for k := range t {
			keys = append(keys, k)
		}
		get = func(k string) any { return t[k] }
	case map[any]any:
		for k := range t {
			s, ok := k.(string)
			if !ok {
				return fmt.Sprintf("has a non-string key %v (supported: %s)", k, strings.Join(RouteEntryKeys, ", ")), true
			}
			keys = append(keys, s)
		}
		get = func(k string) any { return t[k] }
	default:
		return fmt.Sprintf("must be a mapping, got %s", typeName(entry)), true
	}
	sort.Strings(keys)
	var unsupported []string
	for _, k := range keys {
		if !contains(RouteEntryKeys, k) {
			unsupported = append(unsupported, k)
		}
	}
	if len(unsupported) > 0 {
		return fmt.Sprintf("has unsupported key(s) %s (supported: %s; label equality only — no regex, no continue)",
			strings.Join(unsupported, ", "), strings.Join(RouteEntryKeys, ", ")), true
	}
	const needMatch = "needs a non-empty 'match' mapping of label: value (an empty match would take every alert of the tenant)"
	var labels []string
	var value func(string) any
	switch m := get("match").(type) {
	case map[string]any:
		for k := range m {
			labels = append(labels, k)
		}
		value = func(k string) any { return m[k] }
	case map[any]any:
		for k := range m {
			s, ok := k.(string)
			if !ok {
				return fmt.Sprintf("match label %v is not a valid label name", k), true
			}
			labels = append(labels, s)
		}
		value = func(k string) any { return m[k] }
	default:
		return needMatch, true
	}
	if len(labels) == 0 {
		return needMatch, true
	}
	sort.Strings(labels)
	for _, label := range labels {
		if !labelNameRE.MatchString(label) {
			return fmt.Sprintf("match label %q is not a valid label name", label), true
		}
		s, ok := value(label).(string)
		if !ok {
			return fmt.Sprintf("match value for '%s' must be a string, got %s (quote it in YAML)", label, typeName(value(label))), true
		}
		if s == "" {
			return fmt.Sprintf("match value for '%s' is empty (it would match every alert without that label)", label), true
		}
	}
	return "", false
}

// Targets lists the routes of a resolved routing that render a receiver, in
// the generator's order: the main route, the `overrides`, then the `routes`
// — one-for-one with the main route plus _grar_validate.list_tenant_subroutes.
// Nothing when the main receiver is empty (the generator skips the tenant).
// An override needs exactly one of `alertname` / `metric_group` and a
// receiver; a `routes` entry needs RouteEntryProblem to pass and a receiver.
func Targets(resolved map[string]any) []Target {
	main := resolved["receiver"]
	if !Truthy(main) {
		return nil
	}
	out := []Target{{Ref: "receiver", Receiver: main}}
	if overrides, ok := resolved["overrides"].([]any); ok {
		for i, raw := range overrides {
			ov, ok := asStringMap(raw)
			if !ok {
				continue
			}
			if Truthy(ov["alertname"]) == Truthy(ov["metric_group"]) {
				continue
			}
			if !Truthy(ov["receiver"]) {
				continue
			}
			key := "alertname"
			if !Truthy(ov["alertname"]) {
				key = "metric_group"
			}
			out = append(out, Target{Ref: fmt.Sprintf("overrides[%d]", i), Receiver: ov["receiver"],
				Match: map[string]string{key: PyStr(ov[key])}})
		}
	}
	if routes, ok := resolved["routes"].([]any); ok {
		for i, raw := range routes {
			if _, bad := RouteEntryProblem(raw); bad {
				continue
			}
			entry, _ := asStringMap(raw)
			if !Truthy(entry["receiver"]) {
				continue
			}
			match := map[string]string{}
			m, _ := asStringMap(entry["match"])
			for k, v := range m {
				match[k], _ = v.(string)
			}
			out = append(out, Target{Ref: fmt.Sprintf("routes[%d]", i), Receiver: entry["receiver"], Match: match})
		}
	}
	return out
}

// ReceiverType is the type a policy judges: the receiver's `type` when the
// receiver is a mapping and the type a non-empty string, "" (not judged)
// otherwise.
func ReceiverType(receiver any) string {
	m, ok := asStringMap(receiver)
	if !ok {
		return ""
	}
	s, _ := m["type"].(string)
	return s
}

// Truthy is Python truthiness over a decoded YAML value: nil, false, zero,
// "" and an empty list or mapping are false.
func Truthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case string:
		return t != ""
	case int:
		return t != 0
	case int64:
		return t != 0
	case uint64:
		return t != 0
	case float64:
		return t != 0
	case []any:
		return len(t) > 0
	case map[string]any:
		return len(t) > 0
	case map[any]any:
		return len(t) > 0
	}
	return true
}

func typeName(v any) string {
	switch v.(type) {
	case nil:
		return "null"
	case string:
		return "a string"
	case bool:
		return "a boolean"
	case int, int64, uint64:
		return "an integer"
	case float64:
		return "a float"
	case []any:
		return "a list"
	case map[string]any, map[any]any:
		return "a mapping"
	}
	return fmt.Sprintf("%T", v)
}
