package routingpolicy

import (
	"github.com/vencil/threshold-exporter/pkg/pyyamlcompat"
	"gopkg.in/yaml.v3"
)

// Receivers as the route generator reads them (#2295).
//
// The Go readers decode routing with yaml.v3, which keeps plain `on` or
// `1:30` a string; PyYAML — the generator — reads a boolean and an integer,
// so a receiver field that must be a string is refused there and the
// receiver skipped. Only the receivers are re-read: each one a routing
// carries (`receiver`, `overrides[i].receiver`, `routes[i].receiver`) is
// replaced by the value pkg/pyyamlcompat builds from the same node, and
// everything else in the routing stays the yaml.v3 value it was.
//
// ⛔ FAIL-CLOSED: a receiver whose PyYAML reading cannot be found (no such
// node on the PyYAML side, a list of another length, an entry that is not a
// mapping there) becomes Unmatched — never the yaml.v3 value, which would
// judge a string the generator never sees. A mapping on the PyYAML side
// whose other keys are not strings (`on:`, `1:`, `~:` are a boolean, an
// integer and null to PyYAML) is still read by its string key `receiver`.

// Unmatched is the receiver WithPyYAMLReceivers leaves where it cannot find
// the one the route generator reads. It is not a mapping, so the receiver
// check (pkg/receiverspec) refuses it.
var Unmatched = pyyamlcompat.Unsupported{Tag: "receiver", Reason: "not found where the route generator reads it"}

// WithPyYAMLReceivers returns routing with its receivers taken from py, the
// same `_routing` decoded by pyyamlcompat (nil when it could not be found).
// routing is not modified. A routing, list or entry that is not a
// string-keyed mapping on the yaml.v3 side is left as it is: no receiver is
// read from it downstream either. Every receiver the yaml.v3 side carries
// and py does not is Unmatched (see the fail-closed note above).
func WithPyYAMLReceivers(routing, py any) any {
	r, ok := asStringMap(routing)
	if !ok {
		return routing
	}
	withReceiver := func(dst map[string]any, src any) {
		if _, has := dst["receiver"]; !has {
			return
		}
		if v, has := stringKey(src, "receiver"); has {
			dst["receiver"] = v
		} else {
			dst["receiver"] = Unmatched
		}
	}
	withReceiver(r, py)
	for _, list := range []string{"overrides", "routes"} {
		entries, ok := r[list].([]any)
		if !ok {
			continue
		}
		pyList, _ := stringKey(py, list)
		pyEntries, ok := pyList.([]any)
		if !ok || len(pyEntries) != len(entries) {
			pyEntries = make([]any, len(entries)) // nothing matches: every receiver Unmatched
		}
		out := make([]any, len(entries))
		for i, e := range entries {
			out[i] = e
			em, ok := asStringMap(e)
			if !ok {
				continue
			}
			withReceiver(em, pyEntries[i])
			out[i] = em
		}
		r[list] = out
	}
	return r
}

// stringKey is the value under the string key k of a decoded mapping of
// either type — map[any]any when some other key is not a string.
func stringKey(m any, k string) (any, bool) {
	switch t := m.(type) {
	case map[string]any:
		v, ok := t[k]
		return v, ok
	case map[any]any:
		v, ok := t[k]
		return v, ok
	}
	return nil, false
}

// withPyYAMLReceiversFrom is WithPyYAMLReceivers over the node the routing
// was decoded from (nil: not found, every receiver Unmatched).
func withPyYAMLReceiversFrom(routing any, n *yaml.Node) any {
	return WithPyYAMLReceivers(routing, pyyamlcompat.Decode(n))
}

// PyYAMLRoutingByTenant returns, per tenant id, the `_routing` of one tenant
// file's `tenants:` entries decoded by pyyamlcompat — the value to hand
// WithPyYAMLReceivers for that tenant's own routing. Tenant ids and the
// `_routing` key are matched by source text (an alias key by its anchor's),
// merge keys expanded, as the exporter keys tenants. nil when the document
// does not parse or has no such entries.
func PyYAMLRoutingByTenant(data []byte) map[string]any {
	top, err := parseDoc(data)
	if err != nil || top == nil {
		return nil
	}
	t := lookup(top, "tenants")
	if t == nil || t.Kind != yaml.MappingNode {
		return nil
	}
	var out map[string]any
	for _, e := range mappingEntries(t) {
		if n := routingNode(e.value); n != nil {
			if out == nil {
				out = map[string]any{}
			}
			out[e.key] = pyyamlcompat.Decode(n)
		}
	}
	return out
}

// routingNode is the `_routing` value node of a tenant entry body (the last
// one, merge keys expanded), or nil.
func routingNode(body *yaml.Node) *yaml.Node {
	if body == nil || body.Kind != yaml.MappingNode {
		return nil
	}
	var out *yaml.Node
	for _, f := range mappingEntries(body) {
		if f.key == "_routing" {
			out = f.value
		}
	}
	return out
}
