package routingpolicy

import (
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/pyyamlcompat"
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

// ReportsUnusable reports whether LoadRoot names a root file of this name
// that it cannot parse as a Problem (domain_policy_unusable /
// routing_profiles_unusable) — a duplicate key included — rather than
// leaving it to the exporter's parse-failure list.
func ReportsUnusable(name string) bool {
	return contains(policyFileNames, name) || contains(profileFileNames, name)
}

// errUnusable marks a block that decodes but has the wrong shape.
var errUnusable = errors.New("unusable")

// parseDoc decodes one YAML document into its top-level node. A nil node with
// a nil error is an empty document. The full decode is run as well so that a
// duplicate key fails here as it fails in every other reader of the file —
// and so does a key only the route generator counts as written twice (an
// alias key beside its anchor, two `<<`): its StrictLoader refuses the whole
// file (#2295, pyyamlcompat.FindDuplicateKey), so nothing in it is read.
//
// policy is true only for a `_domain_policy.yaml` / `.yml` document: only
// there does `require_critical_escalation` go through
// normalizeTaggedNullEscalation (#2325). Every other platform file keeps
// yaml.v3's reading of a `!!null`-tagged value — refusing one there would
// drop a profiles / defaults file the policy check still needs.
func parseDoc(data []byte, policy bool) (*yaml.Node, error) {
	var doc yaml.Node
	if err := yaml.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	if doc.Kind == 0 || len(doc.Content) == 0 {
		return nil, nil
	}
	top := doc.Content[0]
	normalizeTaggedBools(top)
	if policy {
		if err := normalizeTaggedNullEscalation(top, false); err != nil {
			return nil, err
		}
	}
	var probe any
	if err := top.Decode(&probe); err != nil {
		return nil, err
	}
	if d := pyyamlcompat.FindDuplicateKeyIn(data); d != nil {
		return nil, d
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
		if deref(m.Content[i]).Value == key { // an alias key: its anchor's text
			return deref(m.Content[i+1])
		}
	}
	return nil
}

// lookupEntry is lookup with YAML merge keys expanded (mappingEntries): a key
// supplied through `<<:` is found, and one the mapping writes itself wins. A
// domain policy and its constraints are read this way, as the generator's
// safe_load reads them (#2438); lookup would miss a merged constraint.
func lookupEntry(m *yaml.Node, key string) *yaml.Node {
	if m == nil || m.Kind != yaml.MappingNode {
		return nil
	}
	for _, e := range mappingEntries(m) {
		if e.key == key {
			return e.value
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

// taggedBoolWords is what PyYAML's construct_yaml_bool accepts for a scalar
// TAGGED `!!bool` (any style), looked up by value.lower() — not the plain set
// above: `!!bool yEs` is True (PyYAML 6.0.3), and `!!bool y` / `!!bool 1`
// raise, failing the whole safe_load.
var taggedBoolWords = map[string]bool{"yes": true, "no": false, "true": true, "false": false, "on": true, "off": false}

// taggedBool reports whether n is a scalar explicitly tagged `!!bool` (or
// `!<tag:yaml.org,2002:bool>`) and, if so, the boolean PyYAML reads (ok false
// = PyYAML refuses it). yaml.v3 decodes only true/false under that tag.
func taggedBool(n *yaml.Node) (b, tagged, ok bool) {
	if n.Kind != yaml.ScalarNode || n.Style&yaml.TaggedStyle == 0 || n.ShortTag() != "!!bool" {
		return false, false, false
	}
	b, ok = taggedBoolWords[strings.ToLower(n.Value)]
	return b, true, ok
}

// normalizeTaggedBools rewrites every `!!bool` scalar PyYAML reads to the
// plain `true` / `false` yaml.v3 decodes, so that a file the generator reads
// is not refused whole here (#2325). One PyYAML refuses is left as is, and
// yaml.v3 refuses the file as PyYAML does.
func normalizeTaggedBools(n *yaml.Node) {
	if b, tagged, ok := taggedBool(n); tagged {
		if ok {
			n.Tag, n.Style, n.Value = "!!bool", 0, fmt.Sprint(b)
		}
		return
	}
	for _, c := range n.Content {
		normalizeTaggedBools(c)
	}
}

// normalizeTaggedNullEscalation hands the value of every
// `require_critical_escalation` key that carries the `!!null` tag
// (`!!null x`, `!<tag:yaml.org,2002:null> x`; an alias followed once) to
// DecodePyYAML before any struct decode (#2325). Every such key in the
// document counts, not only one under `domain_policies.*.constraints`: in a
// policy file a same-named key in `_routing_defaults` or an overlay is
// rewritten (or refused) too, as PyYAML's safe_load of the whole file
// constructs every node. yaml.v3 never passes such a node to an Unmarshaler:
// it refuses `!!null x` itself (the whole file, while PyYAML reads None) and
// reads `!!null {}` as null (while PyYAML refuses the whole file). A scalar
// PyYAML reads as None is rewritten to a plain `null`. A node PyYAML refuses
// is, unless lenient, the error returned, and the caller refuses the document
// as the generator does; lenient (UnmarshalPolicy), the entry's value is
// replaced by a refusedTag scalar carrying the refusal, which PyYAMLValue
// records in Refused. Nothing else is touched. Domain policy documents only
// (parseDoc's policy, UnmarshalPolicy).
func normalizeTaggedNullEscalation(n *yaml.Node, lenient bool) error {
	if n.Kind == yaml.MappingNode {
		for i := 0; i+1 < len(n.Content); i += 2 {
			v := deref(n.Content[i+1])
			if n.Content[i].Value != ConstraintRequireCriticalEscalation || v == nil || v.ShortTag() != "!!null" {
				continue
			}
			if _, err := DecodePyYAML(v); err != nil {
				if !lenient {
					return fmt.Errorf("%s: %w", ConstraintRequireCriticalEscalation, err)
				}
				// A new node, not v rewritten: an anchor elsewhere keeps its value.
				n.Content[i+1] = &yaml.Node{Kind: yaml.ScalarNode, Tag: refusedTag, Value: err.Error()}
				continue
			}
			if v.Kind == yaml.ScalarNode {
				v.Tag, v.Style, v.Value = "!!null", 0, "null"
			}
		}
	}
	for _, c := range n.Content {
		if err := normalizeTaggedNullEscalation(c, lenient); err != nil {
			return err
		}
	}
	return nil
}

// refusedTag marks, in UnmarshalPolicy's lenient pass, a `!!null`-tagged
// value PyYAML refuses; the scalar's text is the refusal. yaml.v3 hands a
// node so tagged to an Unmarshaler, where it does not a `!!null` one.
const refusedTag = "!routingpolicy-pyyaml-refused"

// UnmarshalPolicy is yaml.Unmarshal(data, out) for a `_domain_policy.yaml`
// whose `require_critical_escalation` is a PyYAMLValue: the document goes
// through normalizeTaggedNullEscalation (lenient) first, so a `!!null`-tagged
// value reads as PyYAML reads it — None, or refused. A value PyYAML refuses
// never fails the decode: it lands in that PyYAMLValue's Refused, and the
// rest of the document decodes. An empty document leaves out untouched, as
// yaml.Unmarshal does.
func UnmarshalPolicy(data []byte, out any) error {
	var doc yaml.Node
	if err := yaml.Unmarshal(data, &doc); err != nil {
		return err
	}
	if doc.Kind == 0 || len(doc.Content) == 0 {
		return nil
	}
	if err := normalizeTaggedNullEscalation(&doc, true); err != nil {
		return err
	}
	return doc.Decode(out)
}

// DecodePyYAML decodes n as PyYAML's safe_load reads it (#2325), for where
// the Python generator's reading of a boolean flag decides the outcome. For
// a scalar it returns an error exactly when safe_load refuses the value
// (`!!bool y`, `!!int abc`, a plain `2001-13-40`) — the generator then drops
// the whole file. Otherwise it returns what PyYAML builds: nil for None
// (`!!null x` included), the bool for a bool (a plain `yes` / `On`, a
// `!!bool yEs`; a quoted `"yes"` or `!!str yes` stays a string), and for
// anything else a non-bool value — yaml.v3's decode where it has one, else
// the scalar's text. Pinned against PyYAML by
// tests/shared/pyyaml_tagged_scalar_matrix.json; the one blind spot (the
// non-specific tag `!` on a quoted scalar) is in pyyaml.go.
//
// A mapping or sequence (an alias is followed once) is never a bool. It is
// an error when PyYAML refuses it for its own tag or its direct children
// (`!!bool [true]`, `!!omap [1]`, `{<<: 1}`, `{[1]: 2}`; see pyCollection),
// otherwise a non-bool. Nothing deeper is looked at: no recursion, so an
// alias cycle (`&x [*x]`) or fan-out costs nothing. Accepted gap: where
// PyYAML refuses something deeper (`[!!bool y]`) the generator drops the
// whole file and da-guard refuses it, but this returns a non-bool; tenant-api
// treats that as it treats every refused value — this constraint off.
func DecodePyYAML(n *yaml.Node) (any, error) {
	if n = deref(n); n == nil {
		return nil, nil
	}
	if n.Kind != yaml.ScalarNode {
		if err := pyCollection(n); err != nil {
			return nil, err
		}
		return kindName(n), nil
	}
	v, other, err := pyScalar(n)
	if err != nil || !other {
		return v, err
	}
	if n.Decode(&v) == nil && v != nil {
		if _, isBool := v.(bool); !isBool {
			return v, nil
		}
	}
	return n.Value, nil
}

// PyYAMLValue is a struct field decoded with DecodePyYAML (a null or absent
// value leaves Value nil). A value PyYAML refuses does not fail the decode:
// Value is nil and Refused says why (tenant-api: the constraint is off, the
// rest of the file applies). yaml.v3 never hands a `!!null`-tagged node to an
// Unmarshaler: decode through UnmarshalPolicy, or `!!null x` (None in PyYAML)
// fails the enclosing decode and `!!null {}` (refused by PyYAML) reads as
// null.
type PyYAMLValue struct {
	Value   any
	Refused error
}

// UnmarshalYAML implements yaml.Unmarshaler.
func (p *PyYAMLValue) UnmarshalYAML(n *yaml.Node) error {
	if n.Tag == refusedTag {
		p.Value, p.Refused = nil, errors.New(n.Value)
		return nil
	}
	v, err := DecodePyYAML(n)
	if err != nil {
		p.Value, p.Refused = nil, err
		return nil
	}
	p.Value, p.Refused = v, nil
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
	top, err := parseDoc(data, false)
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
	m, ok := asStringMap(withPyYAMLReceiversFrom(v, n))
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
	top, err := parseDoc(data, false)
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
	// mappingEntries, not the raw pairs: a `<<:` here supplies profiles the
	// generator reads (#2438), and an alias key names its anchor's text (#2437).
	entries := mappingEntries(n)
	out := make(map[string]map[string]any, len(entries))
	for _, e := range entries {
		var v any
		if err := e.value.Decode(&v); err != nil {
			return nil, true, err
		}
		m, _ := asStringMap(withPyYAMLReceiversFrom(v, e.value)) // not a mapping: known name, empty body
		out[e.key] = m
	}
	return out, true, nil
}

// ParseDomainPolicies returns the domain policies of one `_domain_policy.yaml`
// document, sorted by domain name, and the parts of it that cannot be
// enforced. Tenant ids are the scalars' source TEXT (`010` is "010"), as the
// exporter keys tenants.
func ParseDomainPolicies(data []byte) ([]Policy, []Problem, error) {
	top, err := parseDoc(data, true)
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
	// As in profilesFromNode: `<<:` expanded (#2438), alias keys by text (#2437).
	entries := mappingEntries(n)
	out := make(map[string]*yaml.Node, len(entries))
	for _, e := range entries {
		out[e.key] = e.value
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
		if t := lookupEntry(n, "tenants"); t != nil {
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
		c := lookupEntry(n, "constraints")
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
			l := lookupEntry(c, cn.key)
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
		if e := lookupEntry(c, ConstraintRequireCriticalEscalation); !isNull(e) {
			// A value PyYAML refuses is a problem too (fail-closed): the
			// generator drops the whole file over it.
			v, err := DecodePyYAML(e)
			if b, ok := v.(bool); ok {
				p.RequireCriticalEscalation = b
			} else if err != nil {
				bad(field+".constraints."+ConstraintRequireCriticalEscalation,
					"domain policy %q: constraint '%s' cannot be read by the route generator (%v) — it refuses the whole file; set it to true or false (unquoted)",
					name, ConstraintRequireCriticalEscalation, err)
			} else {
				bad(field+".constraints."+ConstraintRequireCriticalEscalation,
					"domain policy %q: constraint '%s' must be a boolean, got %s %q — the constraint cannot be enforced; set it to true or false (unquoted)",
					name, ConstraintRequireCriticalEscalation, kindName(e), e.Value)
			}
		}
		pols = append(pols, p)
	}
	return pols, probs
}

// LoadRoot reads the routing layers and the domain policies from the conf.d
// ROOT only — the root half of LoadTree, which the route generator and
// da-guard read since #2326; tenant-api, whose tenants all live at the root,
// reads this half alone. The root's platform files (`_`-prefixed; of the
// defaults carriers only the one the exporter selects) come from the
// exporter's own walker, config.RootPlatformFiles, and are read in name
// order; a later file replaces `_routing_defaults` whole and overrides a
// same-named domain. A profile name defined by two files is
// ProblemRoutingProfileDuplicate; the first definition is kept (#2326).
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
	layers, pols, probs, _ := loadRoot(configDir, skip)
	return layers, pols, probs
}

// loadRoot is LoadRoot plus, per routing-profile name, the root file that
// defined it (LoadTree continues the uniqueness check below the root).
//
// #2326 (ADR-007 amendment 2026-09-28 (c)): a profile name is unique across
// the tree, so a second root file defining it (`_routing_profiles.yml` beside
// `.yaml`) is ProblemRoutingProfileDuplicate and the FIRST definition, in name
// order, is kept — before, the later file silently replaced it.
func loadRoot(configDir string, skip func(rel string) bool) (Layers, []Policy, []Problem, map[string]string) {
	var layers Layers
	var probs []Problem
	profileOrigin := map[string]string{}

	files, err := config.RootPlatformFiles(configDir)
	if err != nil {
		return layers, nil, []Problem{{Kind: ProblemDomainPolicyUnusable,
			Message: fmt.Sprintf("conf.d root could not be read, so no domain policy was read: %v", err)}}, profileOrigin
	}

	policyNodes := map[string]*yaml.Node{}
	policyOrigin := map[string]string{}
	for _, f := range files {
		if skip != nil && skip(f.Name) {
			continue
		}
		isPolicy := contains(policyFileNames, f.Name)
		isProfiles := contains(profileFileNames, f.Name)
		top, err := parseDoc(f.Data, isPolicy)
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
				for _, k := range sortedKeys(p) {
					if first, dup := profileOrigin[k]; dup {
						probs = append(probs, duplicateProfile(k, first, f.Name))
						continue
					}
					profileOrigin[k] = f.Name
					layers.Profiles[k] = p[k]
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
	return layers, pols, probs, profileOrigin
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
		// Keys made strings as for a tenant file (the exporter's merge), so
		// a `1:` in the body or `_routing` does not drop it unjudged.
		body, ok := asStringMap(config.NormalizeYAMLToJSON(decoded))
		if !ok {
			continue
		}
		if r, has := body["_routing"]; has {
			body["_routing"] = withPyYAMLReceiversFrom(r, routingNode(e.value))
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
		own = append(own, mapEntry{key: deref(k).Value, value: v}) // an alias key: its anchor's text, as a decode reads it
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
