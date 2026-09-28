package routingpolicy

import (
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

// Layers is what the conf.d root contributes to every tenant's routing
// before the tenant's own `_routing` is applied.
type Layers struct {
	// Defaults is `_routing_defaults`, with `routes` removed (the Python
	// reader drops it before any merge, #2245). nil = none.
	Defaults map[string]any

	// Profiles is `routing_profiles`, by name. A nil value is a profile whose
	// body is not a mapping: the name is KNOWN (a reference to it is not an
	// unknown profile) but it contributes nothing, as in the Python reader.
	Profiles map[string]map[string]any

	// Overlay is, per tenant id, the `_routing` / `_routing_profile` keys of
	// the root platform files' `tenants:` entries (#2291) — the platform's
	// per-tenant layer the Python reader merges under the tenant's own file
	// (_lib_confd.overlay_platform_tenants): a later file replaces a key an
	// earlier one set. Tenant ids are the keys' source TEXT. nil = none.
	// TenantBlock lays a tenant's own keys over it.
	Overlay map[string]map[string]any
}

// routingBlockKeys are the tenant-block keys Resolve reads.
var routingBlockKeys = [...]string{"_routing", "_routing_profile"}

// TenantBlock returns the block Resolve reads for tenantID, as the route
// generator builds it (#2291): the root platform overlay's `_routing` /
// `_routing_profile`, each replaced WHOLE by the tenant file's own key when
// the tenant file writes it (a null included — `dict.update`, so
// `_routing: null` drops the platform's `_routing`). own is the tenant
// file's block (may be nil). Nothing else is read: routing in the defaults
// chain or in a threshold profile is never rendered, whatever the exporter
// merges into the effective config.
func (l Layers) TenantBlock(tenantID string, own map[string]any) map[string]any {
	out := map[string]any{}
	for _, k := range routingBlockKeys {
		if v, ok := own[k]; ok {
			out[k] = v
		} else if v, ok := l.Overlay[tenantID][k]; ok {
			out[k] = v
		}
	}
	return out
}

// Problem kinds. Each names one platform-file structure the checks depend on
// and cannot use. The file itself is not "failed" in the exporter's sense:
// only the checks that need it are skipped (#1654).
const (
	// ProblemDomainPolicyUnusable: a `_domain_policy.yaml` / `.yml` whose
	// policies cannot be enforced, whole or in part.
	ProblemDomainPolicyUnusable = "domain_policy_unusable"
	// ProblemRoutingProfilesUnusable: a `_routing_profiles.yaml` / `.yml`
	// whose `routing_profiles:` cannot be read.
	ProblemRoutingProfilesUnusable = "routing_profiles_unusable"
	// ProblemRoutingDefaultsRoutes: a `_routing_defaults` carrying `routes`.
	// ADR-007 routes belong to a routing profile or the tenant; the Python
	// reader drops them before any merge and records a blocking WARN
	// (`--validate` fails), so the Go side drops them too and says so.
	ProblemRoutingDefaultsRoutes = "routing_defaults_routes_ignored"
)

// Problem is one platform-file structure LoadRoot could not use.
type Problem struct {
	Kind    string // one of the Problem* kinds above
	File    string // path relative to the conf.d root ("" = the root itself)
	Field   string // dotted path inside the file, "" = the whole file
	Message string
}

// File names the Python reader takes these blocks from (exact spelling:
// _grar_parse._POLICY_FILENAMES and the routing_profiles branch).
var (
	policyFileNames  = []string{"_domain_policy.yaml", "_domain_policy.yml"}
	profileFileNames = []string{"_routing_profiles.yaml", "_routing_profiles.yml"}
)

// errUnusable marks a block that decodes but has the wrong shape.
var errUnusable = errors.New("unusable")

// parseDoc decodes one YAML document into its top-level node. A nil node with
// a nil error is an empty document. The full decode is run as well so that a
// duplicate key fails here as it fails in every other reader of the file.
func parseDoc(data []byte) (*yaml.Node, error) {
	var doc yaml.Node
	if err := yaml.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	if doc.Kind == 0 || len(doc.Content) == 0 {
		return nil, nil
	}
	top := doc.Content[0]
	var probe any
	if err := top.Decode(&probe); err != nil {
		return nil, err
	}
	if probe == nil {
		return nil, nil
	}
	if top.Kind != yaml.MappingNode {
		return nil, fmt.Errorf("top level must be a mapping, got %s: %w", kindName(top), errUnusable)
	}
	return top, nil
}

// lookup returns the value node of key in a mapping node, or nil.
func lookup(m *yaml.Node, key string) *yaml.Node {
	if m == nil || m.Kind != yaml.MappingNode {
		return nil
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			return deref(m.Content[i+1])
		}
	}
	return nil
}

func deref(n *yaml.Node) *yaml.Node {
	for n != nil && n.Kind == yaml.AliasNode {
		n = n.Alias
	}
	return n
}

func isNull(n *yaml.Node) bool {
	return n == nil || (n.Kind == yaml.ScalarNode && n.Tag == "!!null")
}

// yaml11Bools is PyYAML's YAML 1.1 boolean set for a PLAIN scalar
// (Resolver's tag:yaml.org,2002:bool regexp; measured with PyYAML 6.0.3:
// `yEs`, `y`, `n` stay strings). yaml.v3 resolves only true/false (YAML 1.2
// core) and reads the rest as strings.
var yaml11Bools = map[string]bool{
	"yes": true, "Yes": true, "YES": true, "no": false, "No": false, "NO": false,
	"true": true, "True": true, "TRUE": true, "false": false, "False": false, "FALSE": false,
	"on": true, "On": true, "ON": true, "off": false, "Off": false, "OFF": false,
}

// DecodePyYAML decodes n as yaml.v3 does, except that a scalar PyYAML's
// safe_load reads as a boolean decodes to that boolean (#2325): a PLAIN
// (unquoted, untagged) scalar in the YAML 1.1 set (`yes`, `On`, `OFF`, …).
// A quoted `"yes"` or `!!str yes` stays a string, as in PyYAML. (An explicit
// `!!bool yes` is not handled: yaml.v3 refuses it, and the whole document
// with it, before any field is read.) Use it where the Python generator's
// reading of a boolean decides the outcome, so both sides see the same value.
func DecodePyYAML(n *yaml.Node) (any, error) {
	if n = deref(n); n == nil {
		return nil, nil
	}
	if n.Kind == yaml.ScalarNode && n.Style == 0 {
		if b, ok := yaml11Bools[n.Value]; ok {
			return b, nil
		}
	}
	var v any
	err := n.Decode(&v)
	return v, err
}

// PyYAMLValue is a struct field decoded with DecodePyYAML (a null or absent
// value leaves Value nil).
type PyYAMLValue struct{ Value any }

// UnmarshalYAML implements yaml.Unmarshaler.
func (p *PyYAMLValue) UnmarshalYAML(n *yaml.Node) error {
	v, err := DecodePyYAML(n)
	if err != nil {
		return err
	}
	p.Value = v
	return nil
}

func kindName(n *yaml.Node) string {
	switch n.Kind {
	case yaml.MappingNode:
		return "a mapping"
	case yaml.SequenceNode:
		return "a list"
	case yaml.ScalarNode:
		if n.Tag == "!!null" {
			return "null"
		}
		return "a scalar (" + strings.TrimPrefix(n.Tag, "!!") + ")"
	}
	return "an unsupported node"
}

// RoutingDefaultsFrom returns `_routing_defaults` from one platform document,
// with `routes` removed. present reports whether the key is there at all: a
// later file that carries it replaces an earlier one WHOLE, even when its
// value is not a mapping (defaults then become nil), as in the Python reader.
func RoutingDefaultsFrom(data []byte) (defaults map[string]any, present bool, err error) {
	top, err := parseDoc(data)
	if err != nil || top == nil {
		return nil, false, err
	}
	d, present, _, err := routingDefaultsFromNode(top)
	return d, present, err
}

// routingDefaultsFromNode is RoutingDefaultsFrom over a parsed document;
// stripped reports that the block carried `routes`, which were removed.
func routingDefaultsFromNode(top *yaml.Node) (defaults map[string]any, present, stripped bool, err error) {
	n := lookup(top, "_routing_defaults")
	if n == nil {
		return nil, false, false, nil
	}
	var v any
	if err := n.Decode(&v); err != nil {
		return nil, true, false, err
	}
	m, ok := asStringMap(v)
	if !ok {
		return nil, true, false, nil
	}
	_, stripped = m["routes"]
	delete(m, "routes")
	return m, true, stripped, nil
}

// ParseRoutingProfiles returns the `routing_profiles:` block of one
// `_routing_profiles.yaml` document. present=false: no such key. A block that
// is not a mapping is an error (the Python reader WARNs and ignores it whole).
func ParseRoutingProfiles(data []byte) (profiles map[string]map[string]any, present bool, err error) {
	top, err := parseDoc(data)
	if err != nil || top == nil {
		return nil, false, err
	}
	return profilesFromNode(top)
}

func profilesFromNode(top *yaml.Node) (map[string]map[string]any, bool, error) {
	n := lookup(top, "routing_profiles")
	if n == nil {
		return nil, false, nil
	}
	if n.Kind != yaml.MappingNode {
		return nil, true, fmt.Errorf("'routing_profiles:' must be a mapping of profile name to routing, got %s: %w",
			kindName(n), errUnusable)
	}
	out := make(map[string]map[string]any, len(n.Content)/2)
	for i := 0; i+1 < len(n.Content); i += 2 {
		var v any
		if err := deref(n.Content[i+1]).Decode(&v); err != nil {
			return nil, true, err
		}
		m, _ := asStringMap(v) // not a mapping: known name, empty body
		out[n.Content[i].Value] = m
	}
	return out, true, nil
}

// ParseDomainPolicies returns the domain policies of one `_domain_policy.yaml`
// document, sorted by domain name, and the parts of it that cannot be
// enforced. Tenant ids are the scalars' source TEXT (`010` is "010"), as the
// exporter keys tenants.
func ParseDomainPolicies(data []byte) ([]Policy, []Problem, error) {
	top, err := parseDoc(data)
	if err != nil || top == nil {
		return nil, nil, err
	}
	nodes, err := policyNodesFrom(top)
	if err != nil {
		return nil, nil, err
	}
	pols, probs := buildPolicies(nodes, nil)
	return pols, probs, nil
}

func policyNodesFrom(top *yaml.Node) (map[string]*yaml.Node, error) {
	n := lookup(top, "domain_policies")
	if n == nil {
		return nil, nil
	}
	if n.Kind != yaml.MappingNode {
		return nil, fmt.Errorf("'domain_policies:' must be a mapping of domain name to policy, got %s — "+
			"the whole block is ignored: %w", kindName(n), errUnusable)
	}
	out := make(map[string]*yaml.Node, len(n.Content)/2)
	for i := 0; i+1 < len(n.Content); i += 2 {
		out[n.Content[i].Value] = deref(n.Content[i+1])
	}
	return out, nil
}

// buildPolicies turns merged per-domain nodes into Policies, reporting what
// _grar_validate.check_domain_policies reports under --strict for the two
// receiver-type constraints and the blocks they sit in. origin (may be nil)
// names the file each domain was last read from, for Problem.File.
func buildPolicies(nodes map[string]*yaml.Node, origin map[string]string) ([]Policy, []Problem) {
	names := make([]string, 0, len(nodes))
	for name := range nodes {
		names = append(names, name)
	}
	sort.Strings(names)

	var pols []Policy
	var probs []Problem
	var current string
	bad := func(field, format string, args ...any) {
		probs = append(probs, Problem{Kind: ProblemDomainPolicyUnusable, File: origin[current], Field: field,
			Message: fmt.Sprintf(format, args...)})
	}
	for _, name := range names {
		current = name
		n := nodes[name]
		field := "domain_policies." + name
		if isNull(n) {
			continue // explicit null: an inert policy, schema-legal
		}
		if n.Kind != yaml.MappingNode {
			bad(field, "domain policy %q must be a mapping, got %s — the policy cannot be enforced", name, kindName(n))
			continue
		}
		p := Policy{Domain: name}
		if t := lookup(n, "tenants"); t != nil {
			if t.Kind != yaml.SequenceNode {
				bad(field+".tenants", "domain policy %q: 'tenants' must be a list, got %s — the policy cannot be enforced",
					name, kindName(t))
				continue
			}
			for _, item := range t.Content {
				if item = deref(item); item.Kind == yaml.ScalarNode {
					p.Tenants = append(p.Tenants, item.Value)
				}
			}
		}
		c := lookup(n, "constraints")
		if isNull(c) {
			continue // no constraints: inert
		}
		if c.Kind != yaml.MappingNode {
			bad(field+".constraints", "domain policy %q: 'constraints' must be a mapping, got %s — the policy cannot be enforced",
				name, kindName(c))
			continue
		}
		for _, cn := range []struct {
			key string
			dst *[]string
		}{
			{ConstraintForbidden, &p.ForbiddenReceiverTypes},
			{ConstraintAllowed, &p.AllowedReceiverTypes},
		} {
			l := lookup(c, cn.key)
			if isNull(l) {
				continue
			}
			if l.Kind != yaml.SequenceNode {
				bad(field+".constraints."+cn.key, "domain policy %q: constraint '%s' must be a list, got %s — the constraint cannot be enforced",
					name, cn.key, kindName(l))
				continue
			}
			// Only strings can equal a receiver type; the other entries are
			// dropped, but a non-empty allowed list still restricts (see
			// Policy.AllowedListNonEmpty).
			if cn.key == ConstraintAllowed && len(l.Content) > 0 {
				p.AllowedListNonEmpty = true
			}
			for _, item := range l.Content {
				var v any
				if err := deref(item).Decode(&v); err == nil {
					if s, ok := v.(string); ok {
						*cn.dst = append(*cn.dst, s)
					}
				}
			}
		}
		// #2325: only a YAML boolean is a value; the Python check enforces
		// `is True` and --strict reports any other non-null value. Booleans
		// are read PyYAML's way (DecodePyYAML): a plain `yes` is true there.
		if e := lookup(c, ConstraintRequireCriticalEscalation); !isNull(e) {
			if v, err := DecodePyYAML(e); err == nil {
				if b, ok := v.(bool); ok {
					p.RequireCriticalEscalation = b
				} else {
					bad(field+".constraints."+ConstraintRequireCriticalEscalation,
						"domain policy %q: constraint '%s' must be a boolean, got %s %q — the constraint cannot be enforced; set it to true or false (unquoted)",
						name, ConstraintRequireCriticalEscalation, kindName(e), e.Value)
				}
			}
		}
		pols = append(pols, p)
	}
	return pols, probs
}

// LoadRoot reads the routing layers and the domain policies from the conf.d
// ROOT only, as the Python reader does (it is flat). The root's platform
// files (`_`-prefixed; of the defaults carriers only the one the exporter
// selects) come from the exporter's own walker, config.RootPlatformFiles, and
// are read in name order; a later file replaces `_routing_defaults` whole and
// overrides a same-named profile or domain.
//
//   - `_routing_defaults`: any of those files.
//   - `routing_profiles`: `_routing_profiles.yaml` / `.yml` only.
//   - `domain_policies`: `_domain_policy.yaml` / `.yml` only.
//
// skip (may be nil) receives each file's path relative to configDir; true
// leaves the file out — da-guard passes the files the exporter already
// reports as failed, so they are named once, not twice. LoadRoot never adds
// to that list: what it cannot use comes back as Problems.
func LoadRoot(configDir string, skip func(rel string) bool) (Layers, []Policy, []Problem) {
	var layers Layers
	var probs []Problem

	files, err := config.RootPlatformFiles(configDir)
	if err != nil {
		return layers, nil, []Problem{{Kind: ProblemDomainPolicyUnusable,
			Message: fmt.Sprintf("conf.d root could not be read, so no domain policy was read: %v", err)}}
	}

	policyNodes := map[string]*yaml.Node{}
	policyOrigin := map[string]string{}
	for _, f := range files {
		if skip != nil && skip(f.Name) {
			continue
		}
		isPolicy := contains(policyFileNames, f.Name)
		isProfiles := contains(profileFileNames, f.Name)
		top, err := parseDoc(f.Data)
		if err != nil {
			switch {
			case isPolicy:
				probs = append(probs, Problem{Kind: ProblemDomainPolicyUnusable, File: f.Name,
					Message: fmt.Sprintf("%s could not be used, so its domain policies are not enforced: %v", f.Name, trimUnusable(err))})
			case isProfiles:
				probs = append(probs, Problem{Kind: ProblemRoutingProfilesUnusable, File: f.Name,
					Message: fmt.Sprintf("%s could not be used, so its routing profiles are not applied: %v", f.Name, trimUnusable(err))})
			}
			continue
		}
		if top == nil {
			continue
		}
		if d, present, stripped, err := routingDefaultsFromNode(top); present {
			if err != nil {
				d = nil
			}
			layers.Defaults = d
			if stripped {
				probs = append(probs, Problem{Kind: ProblemRoutingDefaultsRoutes, File: f.Name,
					Field: "_routing_defaults.routes",
					Message: fmt.Sprintf("%s: _routing_defaults.routes is not supported (every tenant would inherit "+
						"the escalation); it is ignored — define routes in a routing profile or the tenant's _routing", f.Name)})
			}
		}
		if isProfiles {
			p, present, err := profilesFromNode(top)
			if err != nil {
				probs = append(probs, Problem{Kind: ProblemRoutingProfilesUnusable, File: f.Name,
					Field: "routing_profiles", Message: fmt.Sprintf("%s: %v", f.Name, trimUnusable(err))})
			} else if present {
				if layers.Profiles == nil {
					layers.Profiles = map[string]map[string]any{}
				}
				for k, v := range p {
					layers.Profiles[k] = v
				}
			}
		}
		overlayFrom(top, &layers)
		if isPolicy {
			nodes, err := policyNodesFrom(top)
			if err != nil {
				probs = append(probs, Problem{Kind: ProblemDomainPolicyUnusable, File: f.Name,
					Field: "domain_policies", Message: fmt.Sprintf("%s: %v", f.Name, trimUnusable(err))})
			}
			for k, v := range nodes {
				policyNodes[k] = v
				policyOrigin[k] = f.Name
			}
		}
	}
	pols, pprobs := buildPolicies(policyNodes, policyOrigin)
	probs = append(probs, pprobs...)
	return layers, pols, probs
}

// overlayFrom records the `_routing` / `_routing_profile` keys of one root
// platform file's `tenants:` entries in layers.Overlay, over what earlier
// files set. A `tenants:` that is not a mapping, or an entry body that is not
// one, contributes nothing (the Python reader skips it too; the exporter
// drops such a file whole, so da-guard has already named it).
func overlayFrom(top *yaml.Node, layers *Layers) {
	t := lookup(top, "tenants")
	if t == nil || t.Kind != yaml.MappingNode {
		return
	}
	for _, e := range mappingEntries(t) {
		// ⛔ The body is DECODED, not looked up node by node: a YAML merge
		// key (`ta: {<<: *base}`) is expanded only by the decoder, and the
		// Python reader and the exporter both see the merged keys (#2291
		// review: a `_routing` supplied through `<<:` was missed here).
		var decoded any
		if e.value == nil || e.value.Decode(&decoded) != nil {
			continue
		}
		body, ok := asStringMap(decoded)
		if !ok {
			continue
		}
		tid := e.key
		for _, k := range routingBlockKeys {
			v, present := body[k]
			if !present {
				continue
			}
			if layers.Overlay == nil {
				layers.Overlay = map[string]map[string]any{}
			}
			if layers.Overlay[tid] == nil {
				layers.Overlay[tid] = map[string]any{}
			}
			layers.Overlay[tid][k] = v
		}
	}
}

type mapEntry struct {
	key   string // the key's source text (tenant ids are text: `010` is "010")
	value *yaml.Node
}

// mappingEntries lists a mapping node's entries with YAML merge keys
// expanded the decoder's way: a `<<:` source (an alias to a mapping, or a
// sequence of them, earlier sources winning) supplies the keys the mapping
// does not write itself; an explicit key replaces a merged one whole.
// Keys keep their source text, which a decode into a map would lose.
func mappingEntries(m *yaml.Node) []mapEntry {
	var merged, own []mapEntry
	for i := 0; i+1 < len(m.Content); i += 2 {
		k, v := m.Content[i], deref(m.Content[i+1])
		if k.Tag == "!!merge" || (k.Tag == "" && k.Value == "<<") {
			sources := []*yaml.Node{v}
			if v != nil && v.Kind == yaml.SequenceNode {
				sources = v.Content
			}
			seen := map[string]bool{}
			for _, e := range merged {
				seen[e.key] = true
			}
			for _, s := range sources {
				s = deref(s)
				if s == nil || s.Kind != yaml.MappingNode {
					continue
				}
				for _, e := range mappingEntries(s) {
					if !seen[e.key] {
						seen[e.key] = true
						merged = append(merged, e)
					}
				}
			}
			continue
		}
		own = append(own, mapEntry{key: k.Value, value: v})
	}
	written := map[string]bool{}
	for _, e := range own {
		written[e.key] = true
	}
	out := make([]mapEntry, 0, len(merged)+len(own))
	for _, e := range merged {
		if !written[e.key] {
			out = append(out, e)
		}
	}
	return append(out, own...)
}

func trimUnusable(err error) string {
	return strings.TrimSuffix(err.Error(), ": "+errUnusable.Error())
}

func contains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}
