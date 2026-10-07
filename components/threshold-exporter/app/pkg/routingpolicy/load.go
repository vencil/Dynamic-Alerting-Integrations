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

	// PlatformBodies is the set of tenant ids a root platform file's
	// `tenants:` entry gives a body PyYAML reads as a mapping (any keys, none
	// included). The route generator loads such a tenant whatever its own
	// file's body is (_lib_confd.overlay_platform_tenants merges the platform
	// entry first), so a tenant file's null body does not keep it out of the
	// generator's tenant set (PyYAMLTenantBodyNotMapping, #2519). nil = none.
	PlatformBodies map[string]bool
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

// IsDomainPolicyFile reports whether name is a `_domain_policy.yaml` /
// `.yml` — the one ReportsUnusable kind whose Problem is an error.
func IsDomainPolicyFile(name string) bool { return contains(policyFileNames, name) }

// errUnusable marks a block that decodes but has the wrong shape.
var errUnusable = errors.New("unusable")

// parseDoc decodes one YAML document into its top-level node. A nil node with
// a nil error is an empty document. The full decode is run as well so that a
// duplicate key fails here as it fails in every other reader of the file
// (two `<<` in one mapping included: the decode refuses that) — and so does a
// key only the route generator counts as written twice (an alias key beside
// its anchor): its StrictLoader refuses the whole file (#2295,
// pyyamlcompat.FindDuplicateKey), so nothing in it is read. So does a merge
// key whose value PyYAML refuses to merge (mergeKeyShape, #2677: `<<: 5`,
// `!!merge q: ~`).
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
	if err := mergeKeyShape(top); err != nil {
		return nil, err
	}
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

// lookup returns the value node of key in a mapping node, or nil. YAML merge
// keys are expanded (mappingEntries, in mergeSourceOrder's precedence): a key
// supplied through `<<:` is found, one the mapping writes itself wins, and an
// alias key names its anchor's text (one naming an anchored merge key is a
// merge key: classifyKey). PyYAML's safe_load expands `<<` in EVERY mapping, the document's top
// level included (#2438: a top-level `<<: *x` supplying `domain_policies`,
// `routing_profiles`, `_routing_defaults` or `tenants`), so there is no
// raw-pairs variant: one would miss what the route generator reads.
func lookup(m *yaml.Node, key string) *yaml.Node {
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

// lookupOne is lookup for one key without listing any mapping's entries
// (#2659 round 4): lookup flattens every merge chain it passes, which costs
// O(n²) time and memory on a chain of n mappings each merging the previous
// one — and DomainPoliciesShapeError runs on files no decode has bounded
// yet (tenant-api: startup, every reload, every PR-mode PUT). Same answer as
// lookup, in the same precedence (mergeSourceOrder): a key the mapping
// writes itself (its first such pair) wins; else the merge sources in
// mergeSourceOrder, each searched the same way, the first hit winning — the
// order changes nothing in the bound (#2677). Each mapping is visited at
// most once per call — one already visited (or being visited: a merge
// cycle) holds no hit, as lookup's memoized or cut-off listing of it holds
// none at that point — so the cost is O(nodes).
func lookupOne(m *yaml.Node, key string) *yaml.Node {
	if m == nil || m.Kind != yaml.MappingNode {
		return nil
	}
	return findKey(m, key, map[*yaml.Node]bool{})
}

func findKey(m *yaml.Node, key string, visited map[*yaml.Node]bool) *yaml.Node {
	if visited[m] {
		return nil
	}
	visited[m] = true
	for i := 0; i+1 < len(m.Content); i += 2 {
		if k := m.Content[i]; !isMergeKey(k) && deref(k).Value == key {
			return deref(m.Content[i+1])
		}
	}
	for _, s := range mergeSourceOrder(m) {
		if v := findKey(s, key, visited); v != nil {
			return v
		}
	}
	return nil
}

// mergeSourceOrder lists m's merge sources (deref'd mappings only) in the
// order a first-hit search must take them to resolve a key as PyYAML does
// (#2677). The precedence is PyYAML 6.0.x's: SafeConstructor.flatten_mapping
// then construct_mapping. flatten_mapping removes every merge pair
// from the mapping and builds `merge`: for each merge pair in document order
// it appends the source's pairs (the source flattened first, recursively),
// and for a merge SEQUENCE it flattens each item and appends their pairs
// LAST item first (`submerge.reverse()`); then the mapping's pairs become
// merge + own. construct_mapping assigns them in that order, so the LAST
// pair for a key wins. Read back as a first-hit search, a key resolves to:
//
//  1. a pair the mapping writes itself (not a merge key) — always wins (its
//     first such pair here, its last in PyYAML: a key written twice is a
//     duplicate every reader refuses before either reading matters);
//  2. else the merge keys LAST to first: a later merge pair beats an earlier
//     one (`!!merge q: {...}` then `<<: {...}`: the `<<` source wins);
//  3. within one merge sequence, its items FIRST to last (`<<: [*a, *b]`:
//     *a wins);
//  4. each source resolving the key by these same rules (a nested merge is
//     flattened first, so its own pairs beat its merges, and so on down).
//
// mappingEntries, lookup and lookupOne all take the sources in this order
// through this function. pyyamlcompat.Decode builds flatten_mapping's list
// literally and lands on the same values. A source that is not a mapping
// supplies nothing (the decode refuses that file; this keeps a node walk
// total).
func mergeSourceOrder(m *yaml.Node) []*yaml.Node {
	var out []*yaml.Node
	for i := len(m.Content) - 2; i >= 0; i -= 2 {
		if !isMergeKey(m.Content[i]) {
			continue
		}
		for _, s := range mergeSources(deref(m.Content[i+1])) {
			if s = deref(s); s != nil && s.Kind == yaml.MappingNode {
				out = append(out, s)
			}
		}
	}
	return out
}

// classifyKey is the one place a key node is judged a merge key (#2677):
// pyyaml is the route generator's reading (pyyamlcompat.IsMergeKey — an
// alias key judged by the node it names, `!!merge` under any spelling), the
// one every reader in this package follows; yamlv3 is yaml.v3's own decode's
// (its isMerge: the key node itself, a scalar whose text is `<<`, untagged
// or tagged merge — never an alias, never a tagged `!!merge q`). Where the
// two differ, a struct decode of the file (tenant-api) reads a plain key
// where the generator merges.
func classifyKey(k *yaml.Node) (pyyaml, yamlv3 bool) {
	pyyaml = pyyamlcompat.IsMergeKey(k)
	yamlv3 = k != nil && k.Kind == yaml.ScalarNode && k.Value == "<<" &&
		(k.Tag == "" || k.Tag == "!" || k.ShortTag() == "!!merge")
	return pyyaml, yamlv3
}

// isMergeKey is classifyKey's PyYAML reading.
func isMergeKey(k *yaml.Node) bool {
	pyyaml, _ := classifyKey(k)
	return pyyaml
}

// mergeSources is the merge value v's sources: v itself, or a sequence's
// items in document order.
func mergeSources(v *yaml.Node) []*yaml.Node {
	if v != nil && v.Kind == yaml.SequenceNode {
		return v.Content
	}
	return []*yaml.Node{v}
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

// mergeKeyShape refuses a merge key in the tree under n whose value PyYAML refuses to merge — SafeConstructor
// .flatten_mapping's check: a mapping, or a sequence of mappings (an alias
// judged by the node it names), else "expected a mapping or list of
// mappings for merging" and the generator drops the whole file. yaml.v3
// refuses some of these (`<<: 5`) and reads the tagged spelling
// (`!!merge q: 5`) as a plain key, so the check is made here, for both,
// before any decode. It refuses only: what it passes, yaml.v3's decode may
// still refuse — an alias to a sequence as the whole merge value
// (`<<: *seq`, even `*seq` naming `[]`) PyYAML merges and yaml.v3 does not,
// so parseDoc refuses that file (a gap kept: it errs on the refusing side).
// Every node is written once in the tree, so a walk of it (aliases not
// entered) sees every mapping PyYAML constructs. A merge value is checked
// once however many merge keys alias it, so the cost is linear in the
// nodes. It also refuses a merge-tagged scalar (`<<`, `!!merge x`) PyYAML
// would construct as a value — it has no constructor for one, so the
// generator drops the whole file. The generator's loader builds a mapping
// value (any key but a null one), and a sequence's items — except a
// sequence under a `tenants` key (_lib_yaml_keys.ExporterKeyLoader,
// raw_text_sequences): its scalar items are read as their source text, never
// constructed. A sequence is constructed when it is reached anywhere else
// too (written in place or through an alias): as another key's value, or as
// an item of a sequence.
func mergeKeyShape(n *yaml.Node) error {
	_, err := mergeKeyShapeWork(n)
	return err
}

// mergeKeyShapeWork is mergeKeyShape, also returning how many merge-value
// nodes (a value and a sequence value's items) it looked at.
func mergeKeyShapeWork(n *yaml.Node) (int, error) {
	checked := map[*yaml.Node]bool{}
	var built []*yaml.Node // sequences PyYAML constructs (items and all)
	isBuilt := map[*yaml.Node]bool{}
	build := func(s *yaml.Node) {
		if s != nil && s.Kind == yaml.SequenceNode && !isBuilt[s] {
			isBuilt[s] = true
			built = append(built, s)
		}
	}
	work := 0
	stack := []*yaml.Node{n}
	for len(stack) > 0 {
		n := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		switch n.Kind {
		case yaml.DocumentNode:
			for _, c := range n.Content {
				build(deref(c))
			}
		case yaml.SequenceNode:
			for _, c := range n.Content {
				build(deref(c))
			}
		case yaml.MappingNode:
			for i := 0; i+1 < len(n.Content); i += 2 {
				k, v := deref(n.Content[i]), deref(n.Content[i+1])
				if isMergeKey(n.Content[i]) || k == nil || k.ShortTag() == "!!null" {
					continue // a merge value is merged, a null key's value never built
				}
				if mergeTaggedScalar(v) {
					return work, fmt.Errorf("line %d: PyYAML cannot build a merge key (%s) as a value", v.Line, keySpelling(v))
				}
				if k.Kind != yaml.ScalarNode || k.Value != "tenants" {
					build(v)
				}
			}
		}
		if n.Kind == yaml.MappingNode {
			for i := 0; i+1 < len(n.Content); i += 2 {
				if !isMergeKey(n.Content[i]) {
					continue
				}
				v := deref(n.Content[i+1])
				if checked[v] {
					continue
				}
				checked[v] = true
				work++
				if v != nil && v.Kind == yaml.SequenceNode {
					work += len(v.Content)
				}
				if err := mergeValueShape(v); err != nil {
					return work, fmt.Errorf("line %d: %w", n.Content[i].Line, err)
				}
			}
		}
		if n.Kind != yaml.AliasNode {
			stack = append(stack, n.Content...)
		}
	}
	for _, s := range built {
		for _, c := range s.Content {
			if d := deref(c); mergeTaggedScalar(d) {
				return work, fmt.Errorf("line %d: PyYAML cannot build a merge key (%s) as a value", d.Line, keySpelling(d))
			}
		}
	}
	return work, nil
}

// mergeTaggedScalar reports whether n is a merge-tagged scalar (`<<`,
// `!!merge x`).
func mergeTaggedScalar(n *yaml.Node) bool {
	return n != nil && n.Kind == yaml.ScalarNode && n.ShortTag() == "!!merge"
}

// dropEscalationValues replaces, in DomainPoliciesShapeError's own parse,
// the value tenant-api reads leniently (#2325 — one PyYAML refuses turns only
// that constraint off) by a null, so what is written there is not a reason
// to refuse the file: `domain_policies.<domain>.constraints
// .require_critical_escalation`, each step a key written in place (not
// through a merge or an alias), and only a value with no anchor in it
// (aliased elsewhere, PyYAML builds it there too). Anywhere else that key is
// looked at like any other.
func dropEscalationValues(top *yaml.Node) {
	dp := ownValue(top, "domain_policies")
	if dp == nil || dp.Kind != yaml.MappingNode {
		return
	}
	for i := 1; i < len(dp.Content); i += 2 {
		c := ownValue(dp.Content[i], "constraints")
		if c == nil || c.Kind != yaml.MappingNode {
			continue
		}
		for j := 0; j+1 < len(c.Content); j += 2 {
			if k := c.Content[j]; k.Kind == yaml.ScalarNode && k.Value == "require_critical_escalation" &&
				!isMergeKey(k) && !hasAnchor(c.Content[j+1]) {
				c.Content[j+1] = &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!null", Line: c.Content[j+1].Line}
			}
		}
	}
}

// ownValue is the value m (a mapping written in place) writes itself under
// key, as written (an alias stays one), or nil.
func ownValue(m *yaml.Node, key string) *yaml.Node {
	if m == nil || m.Kind != yaml.MappingNode {
		return nil
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		if k := m.Content[i]; k.Kind == yaml.ScalarNode && k.Value == key && !isMergeKey(k) {
			return m.Content[i+1]
		}
	}
	return nil
}

// hasAnchor reports whether any node under n (aliases not entered) has an
// anchor.
func hasAnchor(n *yaml.Node) bool {
	stack := []*yaml.Node{n}
	for len(stack) > 0 {
		n := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if n.Anchor != "" {
			return true
		}
		if n.Kind != yaml.AliasNode {
			stack = append(stack, n.Content...)
		}
	}
	return false
}

// MergeShapeError is mergeKeyShape's refusal for one YAML file, nil when it
// passes or does not parse (its reader's decode reports that). da-guard
// names such a file in parse_failed, as for a key the generator counts as
// repeated (#2677).
func MergeShapeError(data []byte) error {
	var doc yaml.Node
	if yaml.Unmarshal(data, &doc) != nil {
		return nil
	}
	return mergeKeyShape(&doc)
}

func mergeValueShape(v *yaml.Node) error {
	if v != nil && v.Kind == yaml.MappingNode {
		return nil
	}
	if v != nil && v.Kind == yaml.SequenceNode {
		for _, c := range v.Content {
			if c = deref(c); c == nil || c.Kind != yaml.MappingNode {
				return fmt.Errorf("PyYAML expects a mapping for merging, but found %s", kindName(c))
			}
		}
		return nil
	}
	return fmt.Errorf("PyYAML expects a mapping or list of mappings for merging, but found %s", kindName(v))
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
	d, present, _, _, err := routingDefaultsFromNode(top)
	return d, present, err
}

// routingDefaultsFromNode is RoutingDefaultsFrom over a parsed document;
// stripped reports that the block carried `routes`, which were removed.
// notMapping (#2341 R5) is the value when, as PyYAML reads it, it is neither
// a mapping nor null (nil otherwise): the caller reports
// ProblemRoutingDefaultsNotMapping.
func routingDefaultsFromNode(top *yaml.Node) (defaults map[string]any, present, stripped bool, notMapping any, err error) {
	n := lookup(top, "_routing_defaults")
	if n == nil {
		return nil, false, false, nil, nil
	}
	var v any
	if err := n.Decode(&v); err != nil {
		return nil, true, false, nil, err
	}
	pv := withPyYAMLRoutingFrom(v, n)
	m, ok := asStringMap(pv)
	if !ok {
		if _, isMap := pv.(map[any]any); !isMap && pv != nil {
			notMapping = pv
		}
		return nil, true, false, notMapping, nil
	}
	_, stripped = m["routes"]
	delete(m, "routes")
	return m, true, stripped, nil, nil
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
		m, _ := asStringMap(withPyYAMLRoutingFrom(v, e.value)) // not a mapping: known name, empty body
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

// DomainPoliciesShapeError is the shape check da-guard applies to a
// `_domain_policy.yaml` / `.yml` (#2659): `domain_policies` present and not
// a mapping — null, `~`, bare, a list, a scalar — is the error da-guard
// reports as domain_policy_unusable (the generator's --strict drops the
// block too). It is the SAME predicate as ParseDomainPolicies' (both call
// domainPoliciesNode), on a node parse only: nothing is decoded, so no alias
// is expanded. A document that does not parse, an empty one, or one whose
// top level is not a mapping is nil here — those are the caller's own
// decode's to judge. tenant-api's policy loader calls it first.
//
// It refuses more (#2677), since tenant-api then decodes the file with
// yaml.v3 — which never reads a part its struct does not name — so the
// policy it reads could differ from the one the generator reads: a file
// the generator drops whole for mergeKeyShape's reasons or a key it counts
// as written twice (a value PyYAML refuses under require_critical_escalation
// stays tenant-api's to read leniently, #2325: dropEscalationValues),
// and a key the generator and yaml.v3 do not both read as
// a merge key, or both as a plain one (classifyKey: `!!merge q:`, an alias
// naming an anchored `<<`), anywhere in it. One literal `<<` per mapping
// (yaml.v3 refuses two) yaml.v3 merges as PyYAML does.
func DomainPoliciesShapeError(data []byte) error {
	var doc yaml.Node
	if err := yaml.Unmarshal(data, &doc); err != nil || len(doc.Content) == 0 {
		return nil
	}
	top := doc.Content[0]
	if top.Kind != yaml.MappingNode {
		return nil
	}
	dropEscalationValues(top)
	if err := mergeKeyShape(top); err != nil {
		return fmt.Errorf("%w — the route generator drops this file: %w", err, errUnusable)
	}
	if d := pyyamlcompat.FindDuplicateKey(&doc); d != nil {
		return fmt.Errorf("%w: %w", d, errUnusable)
	}
	if k := divergentMergeKey(top); k != nil {
		return fmt.Errorf("line %d: a merge key spelled %q in the file — "+
			"tenant-api cannot read this file as the route generator does: %w", k.Line, keySpelling(k), errUnusable)
	}
	_, err := domainPoliciesNode(top)
	return err
}

// divergentMergeKey returns a key in the tree under n (an alias's target
// included) that classifyKey's two readings disagree on, or nil. Each node
// is visited once.
func divergentMergeKey(n *yaml.Node) *yaml.Node {
	visited := map[*yaml.Node]bool{}
	stack := []*yaml.Node{n}
	for len(stack) > 0 {
		n := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if n == nil || visited[n] {
			continue
		}
		visited[n] = true
		if n.Kind == yaml.MappingNode {
			for i := 0; i+1 < len(n.Content); i += 2 {
				if py, v3 := classifyKey(n.Content[i]); py != v3 {
					return n.Content[i]
				}
			}
		}
		stack = append(stack, n.Content...)
		if n.Kind == yaml.AliasNode {
			stack = append(stack, n.Alias)
		}
	}
	return nil
}

// keySpelling is how a key node is written, for an error message: `*m`
// for an alias, else its tag and text.
func keySpelling(k *yaml.Node) string {
	if k.Kind == yaml.AliasNode {
		return "*" + k.Value
	}
	return strings.TrimSpace(k.Tag + " " + k.Value)
}

// domainPoliciesNode is the `domain_policies` mapping of a document's top
// mapping (nil: no such key), found with lookup (`<<:` expanded as da-guard
// expands it), or the error that it is not a mapping.
func domainPoliciesNode(top *yaml.Node) (*yaml.Node, error) {
	n := lookupOne(top, "domain_policies") // bounded: DomainPoliciesShapeError's input is unvetted
	if n == nil {
		return nil, nil
	}
	if n.Kind != yaml.MappingNode {
		return nil, fmt.Errorf("'domain_policies:' must be a mapping of domain name to policy, got %s — "+
			"the whole block is ignored: %w", kindName(n), errUnusable)
	}
	return n, nil
}

func policyNodesFrom(top *yaml.Node) (map[string]*yaml.Node, error) {
	n, err := domainPoliciesNode(top)
	if n == nil || err != nil {
		return nil, err
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
	layers, pols, probs, _, _ := loadRoot(configDir, skip)
	return layers, pols, probs
}

// loadRoot is LoadRoot plus, per routing-profile name, the root file that
// defined it (LoadTree continues the uniqueness check below the root), and
// the `_routing_enforced` block the generator renders from (Tree.Enforced,
// #2503: the last root file, in name order, that enables one).
//
// #2326 (ADR-007 amendment 2026-09-28 (c)): a profile name is unique across
// the tree, so a second root file defining it (`_routing_profiles.yml` beside
// `.yaml`) is ProblemRoutingProfileDuplicate and the FIRST definition, in name
// order, is kept — before, the later file silently replaced it.
func loadRoot(configDir string, skip func(rel string) bool) (Layers, []Policy, []Problem, map[string]string, *Enforced) {
	var layers Layers
	var probs []Problem
	var enforced *Enforced
	profileOrigin := map[string]string{}

	files, err := config.RootPlatformFiles(configDir)
	if err != nil {
		return layers, nil, []Problem{{Kind: ProblemDomainPolicyUnusable,
			Message: fmt.Sprintf("conf.d root could not be read, so no domain policy was read: %v", err)}}, profileOrigin, nil
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
		if e := enforcedFrom(f.Name, top); e != nil {
			enforced = e
		}
		if d, present, stripped, bad, err := routingDefaultsFromNode(top); present {
			if err != nil {
				d = nil
			}
			layers.Defaults = d
			if bad != nil {
				probs = append(probs, routingDefaultsNotMapping(f.Name, bad))
			}
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
	return layers, pols, probs, profileOrigin, enforced
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
		if pyyamlMapping(e.value) {
			if layers.PlatformBodies == nil {
				layers.PlatformBodies = map[string]bool{}
			}
			layers.PlatformBodies[e.key] = true
		}
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
			body["_routing"] = withPyYAMLRoutingFrom(r, routingNode(e.value))
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
// expanded PyYAML's way (mergeSourceOrder): a merge source (an alias to a
// mapping, or a sequence of them) supplies the keys the mapping does not
// write itself — a later merge key beating an earlier one, an earlier item of
// one merge sequence beating a later one; an explicit key replaces a merged
// one whole.
// Keys keep their source text, which a decode into a map would lose.
//
// #2659: DomainPoliciesShapeError looks a key up in a document no decode has
// vetted (a node parse only), so a merge source is listed once per call
// (memoized: a merge fan-out costs O(nodes), not a product) and one already
// being listed contributes nothing (`&x {<<: *x}` ends instead of recursing
// without bound). On a document parseDoc accepted — no alias cycle, no
// excessive aliasing — the result is the same as listing it naively.
func mappingEntries(m *yaml.Node) []mapEntry {
	return (&entryLister{memo: map[*yaml.Node][]mapEntry{}, active: map[*yaml.Node]bool{}}).entries(m)
}

type entryLister struct {
	memo   map[*yaml.Node][]mapEntry
	active map[*yaml.Node]bool
}

func (l *entryLister) entries(m *yaml.Node) []mapEntry {
	if out, ok := l.memo[m]; ok {
		return out
	}
	if l.active[m] {
		return nil
	}
	l.active[m] = true
	out := l.list(m)
	delete(l.active, m)
	l.memo[m] = out
	return out
}

func (l *entryLister) list(m *yaml.Node) []mapEntry {
	var merged, own []mapEntry
	seen := map[string]bool{}
	for _, s := range mergeSourceOrder(m) { // first hit wins, as in findKey
		for _, e := range l.entries(s) {
			if !seen[e.key] {
				seen[e.key] = true
				merged = append(merged, e)
			}
		}
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		if k := m.Content[i]; !isMergeKey(k) {
			own = append(own, mapEntry{key: deref(k).Value, value: deref(m.Content[i+1])}) // an alias key: its anchor's text, as a decode reads it
		}
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
