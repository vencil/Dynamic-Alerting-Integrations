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

// WithPyYAMLReceivers returns routing with its receivers taken from py, the
// same `_routing` decoded by pyyamlcompat. routing is not modified; a routing,
// list or entry that is not the expected shape on either side is left as it
// is.
func WithPyYAMLReceivers(routing, py any) any {
	r, ok := asStringMap(routing)
	if !ok {
		return routing
	}
	p, ok := asStringMap(py)
	if !ok {
		return routing
	}
	withReceiver := func(dst map[string]any, src map[string]any) {
		if _, has := dst["receiver"]; !has {
			return
		}
		if v, has := src["receiver"]; has {
			dst["receiver"] = v
		}
	}
	withReceiver(r, p)
	for _, list := range []string{"overrides", "routes"} {
		entries, ok := r[list].([]any)
		if !ok {
			continue
		}
		pyEntries, ok := p[list].([]any)
		if !ok || len(pyEntries) != len(entries) {
			continue
		}
		out := make([]any, len(entries))
		for i, e := range entries {
			out[i] = e
			em, ok := asStringMap(e)
			if !ok {
				continue
			}
			pm, ok := asStringMap(pyEntries[i])
			if !ok {
				continue
			}
			withReceiver(em, pm)
			out[i] = em
		}
		r[list] = out
	}
	return r
}

// withPyYAMLReceiversFrom is WithPyYAMLReceivers over the node the routing
// was decoded from.
func withPyYAMLReceiversFrom(routing any, n *yaml.Node) any {
	return WithPyYAMLReceivers(routing, pyyamlcompat.Decode(n))
}

// PyYAMLRoutingByTenant returns, per tenant id, the `_routing` of one tenant
// file's `tenants:` entries decoded by pyyamlcompat — the value to hand
// WithPyYAMLReceivers for that tenant's own routing. Tenant ids and the
// `_routing` key are matched by source text, merge keys expanded, as the
// exporter keys tenants. nil when the document does not parse or has no such
// entries.
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
