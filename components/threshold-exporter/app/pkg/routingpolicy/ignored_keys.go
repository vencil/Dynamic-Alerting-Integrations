package routingpolicy

import (
	"fmt"
	"sort"

	"github.com/vencil/threshold-exporter/pkg/receiverspec"
)

// ProblemRoutingKeyIgnored: a key in a routing block the route generator does
// not read (#2960) — `continue`, `mute_time_intervals`, a typo. The block
// still renders, without the key; the generator names it with an
// IgnoredKeyWarning (blocking under --validate), da-guard as an error.
const ProblemRoutingKeyIgnored = "routing_key_ignored"

// BlockKind names the routing block IgnoredKeys reads.
type BlockKind string

const (
	// BlockRouting is a tenant `_routing` or a routing profile.
	BlockRouting BlockKind = "routing"
	// BlockRoutingDefaults is a `_routing_defaults` (root or a directory's
	// defaults carrier).
	BlockRoutingDefaults BlockKind = "routing_defaults"
	// BlockRoutingEnforced is the root `_routing_enforced`.
	BlockRoutingEnforced BlockKind = "routing_enforced"
)

// The keys each block is read for — copies of _grar_validate.ROUTING_BLOCK_KEYS
// and siblings; the shared table tests/shared/routing_ignored_keys_cases.json
// runs both. `routes` is read in `_routing_defaults` only to be refused with
// its own Problem (ProblemRoutingDefaultsRoutes).
var (
	timingKeys          = []string{"group_wait", "group_interval", "repeat_interval"}
	routingReadKeys     = append([]string{"receiver", "group_by", "overrides", "routes"}, timingKeys...)
	overrideEntryKeys   = append([]string{"alertname", "metric_group", "receiver", "group_by"}, timingKeys...)
	routingEnforcedKeys = append([]string{"enabled", "receiver", "match", "group_by"}, timingKeys...)
)

// IgnoredKeys returns the paths, relative to block and in sorted order, of
// the keys block carries that the route generator does not read
// (_grar_validate.ignored_routing_keys): the block's own keys, its
// receiver's, each `overrides` entry's and its receiver's (not for
// `_routing_enforced`), and each `routes` entry's receiver's (a `routes`
// entry's own keys are RouteEntryProblem's). `http_config` is not looked
// into — it is passed to Alertmanager as written. A null key is passed over:
// the generator's loader drops its pair (and config.NormalizeYAMLToJSON
// spells it "<nil>"). Any other key is named by its text — the generator
// reads every mapping key as source text, so `1:` and `on:` are keys it does
// not read. A value of the wrong shape is other checks' to name.
func IgnoredKeys(block any, kind BlockKind) []string {
	var allowed []string
	switch kind {
	case BlockRouting, BlockRoutingDefaults:
		allowed = routingReadKeys
	case BlockRoutingEnforced:
		allowed = routingEnforcedKeys
	default:
		panic(fmt.Sprintf("routingpolicy.IgnoredKeys: unknown block kind %q", kind))
	}
	m, ok := stringKeyed(block)
	if !ok {
		return nil
	}
	var out []string
	check := func(obj map[string]any, keys []string, path string) {
		for k := range obj {
			if !contains(keys, k) {
				out = append(out, path+k)
			}
		}
	}
	checkReceiver := func(v any, path string) {
		recv, ok := stringKeyed(v)
		if !ok {
			return
		}
		rtype, _ := recv["type"].(string)
		f, known := receiverspec.Fields(rtype)
		if !known {
			return
		}
		check(recv, append([]string{"type"}, f...), path+"receiver.")
	}
	check(m, allowed, "")
	checkReceiver(m["receiver"], "")
	if kind != BlockRoutingEnforced {
		if list, ok := m["overrides"].([]any); ok {
			for i, e := range list {
				if entry, ok := stringKeyed(e); ok {
					path := fmt.Sprintf("overrides[%d].", i)
					check(entry, overrideEntryKeys, path)
					checkReceiver(entry["receiver"], path)
				}
			}
		}
	}
	if kind == BlockRouting {
		if list, ok := m["routes"].([]any); ok {
			for i, e := range list {
				if entry, ok := stringKeyed(e); ok {
					checkReceiver(entry["receiver"], fmt.Sprintf("routes[%d].", i))
				}
			}
		}
	}
	sort.Strings(out)
	return out
}

// ignoredKeyProblems is one ProblemRoutingKeyIgnored per key IgnoredKeys
// names in block, read from file at fieldPrefix ("_routing_defaults.",
// "routing_profiles.<name>.", "_routing_enforced."); where names the block in
// the message.
func ignoredKeyProblems(file, fieldPrefix, where string, block any, kind BlockKind) []Problem {
	var out []Problem
	for _, f := range IgnoredKeys(block, kind) {
		out = append(out, Problem{Kind: ProblemRoutingKeyIgnored, File: file, Field: fieldPrefix + f,
			Message: fmt.Sprintf("%s: %s%s is not read by the route generator; the rest is rendered as if it were absent",
				where, fieldPrefix, f)})
	}
	return out
}

// nullKeyText is config.NormalizeYAMLToJSON's spelling of a null key.
const nullKeyText = "<nil>"

// stringKeyed is v as a map keyed by each key's text, null keys dropped (see
// IgnoredKeys). ok is false when v is no mapping.
func stringKeyed(v any) (map[string]any, bool) {
	switch t := v.(type) {
	case map[string]any:
		if _, has := t[nullKeyText]; !has {
			return t, true
		}
		out := make(map[string]any, len(t))
		for k, e := range t {
			if k != nullKeyText {
				out[k] = e
			}
		}
		return out, true
	case map[any]any:
		out := make(map[string]any, len(t))
		for k, e := range t {
			if k == nil {
				continue
			}
			s, ok := k.(string)
			if !ok {
				s = fmt.Sprint(k)
			}
			out[s] = e
		}
		return out, true
	}
	return nil, false
}
