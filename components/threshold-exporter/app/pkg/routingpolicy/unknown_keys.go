package routingpolicy

import (
	"fmt"
	"sort"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// RoutingLayer names where UnknownRoutingKeys reads a mapping's keys.
type RoutingLayer string

const (
	// LayerTenant is a tenant's config: the generator reads `_routing` and
	// `_routing_profile` there.
	LayerTenant RoutingLayer = "tenant"
	// LayerPlatform is the top level of a conf.d `_` file: the generator
	// reads `_routing_defaults` and `_routing_enforced` there.
	LayerPlatform RoutingLayer = "platform"
)

// UnknownRoutingKeys returns, sorted, the `_routing`-prefixed keys of block
// the route generator does not read in layer (#2983) —
// _grar_validate.unknown_routing_keys. `_routing` is a reserved PREFIX, so
// such a key (`_routing_profle`, `_routing_defualts`) passes every key check
// as reserved while nothing reads it. Keys without the prefix are other
// checks'; a null key is passed over (stringKeyed).
func UnknownRoutingKeys(block any, layer RoutingLayer) []string {
	m, ok := stringKeyed(block)
	if !ok {
		return nil
	}
	var out []string
	for k := range m {
		if config.IsUnknownRoutingKey(k, layer == LayerPlatform) {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}

// UnknownRoutingKeyHint is the hint both the platform Problem and da-guard's
// tenant finding print after the key.
func UnknownRoutingKeyHint(key string, layer RoutingLayer) string {
	return config.UnknownRoutingKeyHint(key, layer == LayerPlatform)
}

// unknownPlatformKeyProblems is one ProblemRoutingKeyIgnored per top-level
// key of the `_` file file (its document top) that UnknownRoutingKeys names
// for LayerPlatform; Field is the key.
func unknownPlatformKeyProblems(file string, top *yaml.Node) []Problem {
	keys := map[string]any{}
	for _, e := range mappingEntries(top) {
		if !e.null { // the generator's loader drops a null key's pair
			keys[e.key] = nil
		}
	}
	var out []Problem
	for _, k := range UnknownRoutingKeys(keys, LayerPlatform) {
		out = append(out, Problem{Kind: ProblemRoutingKeyIgnored, File: file, Field: k,
			Message: fmt.Sprintf("%s: top-level %s is not a key the route generator reads (%s); it has no effect",
				file, k, UnknownRoutingKeyHint(k, LayerPlatform))})
	}
	return out
}
