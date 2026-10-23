// Package customalerts performs comment-preserving, AST-level edits of a
// tenant's `_custom_alerts` list inside its (human-authored) GitOps
// tenant.yaml (ADR-024 §S6b-2, #741).
//
// The cardinal rule (review Reef 1 — "YAML comments wipeout"): NEVER do a
// struct round-trip (`yaml.Unmarshal(file,&T)` → mutate → `yaml.Marshal`),
// which silently destroys every comment, blank line, and custom indentation
// an SRE put in the file. Instead we walk the yaml.v3 document Node tree,
// surgically replace (or delete) ONLY the `_custom_alerts` value node, and
// re-encode — leaving the rest of the tree (and its comments) intact.
//
// Empty input deletes the key entirely (review Reef 2 — no `_custom_alerts: []`
// debris).
package customalerts

import (
	"bytes"
	"fmt"
	"reflect"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// MergeCustomAlerts replaces the `tenants.<tenantID>._custom_alerts` sequence
// in rawYAML with recipes (or deletes the key if recipes is empty), preserving
// the rest of the document — including comments — via yaml.Node surgery.
//
// recipes is the desired full list (the client owns the array; this is a
// collection-replace, not a per-item merge).
func MergeCustomAlerts(rawYAML, tenantID string, recipes []map[string]any) (string, error) {
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(rawYAML), &doc); err != nil {
		return "", fmt.Errorf("parse tenant yaml: %w", err)
	}
	if doc.Kind != yaml.DocumentNode || len(doc.Content) == 0 {
		return "", fmt.Errorf("tenant yaml is not a document")
	}
	root := doc.Content[0]
	if root.Kind != yaml.MappingNode {
		return "", fmt.Errorf("tenant yaml root is not a mapping")
	}

	tenantsVal := mapValue(root, "tenants")
	if tenantsVal == nil || tenantsVal.Kind != yaml.MappingNode {
		return "", fmt.Errorf("tenant yaml has no `tenants:` mapping")
	}
	tenantVal := mapValue(tenantsVal, tenantID)
	if tenantVal == nil || tenantVal.Kind != yaml.MappingNode {
		return "", fmt.Errorf("tenant yaml has no `tenants.%s` mapping", tenantID)
	}

	const key = "_custom_alerts"
	if len(recipes) == 0 {
		// Reef 2: delete the key + value pair entirely, leaving no debris.
		deleteMapKey(tenantVal, key)
	} else {
		// Build the replacement sequence with CANONICAL key order + quoting —
		// a raw map encode would alphabetise keys (metric/name/recipe…) and
		// drop the source's quotes on `value:severity`, producing churny,
		// non-hand-written-looking diffs. Order + style are emission quality.
		seq := &yaml.Node{Kind: yaml.SequenceNode, Tag: "!!seq"}
		for _, r := range recipes {
			seq.Content = append(seq.Content, recipeNode(r))
		}
		setMapValue(tenantVal, key, seq)
	}

	DropImplicitTags(&doc)
	var buf bytes.Buffer
	enc := yaml.NewEncoder(&buf)
	// SetIndent(2) matches the platform's 2-space conf.d convention, under
	// which a file round-trips unchanged. CAVEAT (self-review F3): a tenant
	// file authored with a DIFFERENT indent (e.g. 4-space) is re-emitted at
	// 2 spaces — comments survive, but the whole file reflows to convention.
	// Acceptable while conf.d is uniformly 2-space; source-indent detection
	// is a future hardening if non-conventional files appear.
	enc.SetIndent(2)
	if err := enc.Encode(&doc); err != nil {
		return "", fmt.Errorf("re-encode tenant yaml: %w", err)
	}
	_ = enc.Close()
	return buf.String(), nil
}

// recipeFieldOrder is the canonical emission order for a recipe's keys —
// logical (recipe → identity → metric(s) → params → routing), from the recipe
// contract the exporter embeds (config.RecipeFieldOrder, #3229). Keys not
// listed are appended after (sorted) so a future schema field is never
// silently dropped.
var recipeFieldOrder = config.RecipeFieldOrder()

// recipeNode builds an ordered, properly-quoted mapping node for one recipe.
func recipeNode(r map[string]any) *yaml.Node {
	m := &yaml.Node{Kind: yaml.MappingNode, Tag: "!!map"}
	emit := func(k string) {
		v, ok := r[k]
		if !ok {
			return
		}
		m.Content = append(m.Content,
			&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: k},
			valueNode(v))
	}
	seen := map[string]bool{}
	for _, k := range recipeFieldOrder {
		if _, ok := r[k]; ok {
			emit(k)
			seen[k] = true
		}
	}
	// Forward-safety: any key not in the canonical order (e.g. a future schema
	// field) still gets emitted, sorted, rather than dropped.
	var extra []string
	for k := range r {
		if !seen[k] {
			extra = append(extra, k)
		}
	}
	sort.Strings(extra)
	for _, k := range extra {
		emit(k)
	}
	return m
}

// valueNode renders a recipe field value. Every string is quoted or a block
// (#2903), whatever it holds: a plain scalar is read by each YAML reader by
// its own rules (`1:20` is an integer to PyYAML, `2001-12-14t21:59:43` a
// datetime), so only a quoted one or a block scalar is a string to all of
// them. A string with a line break is a literal block (`|`), so a diff shows
// its lines, when that block reads back as the same text (see
// literalBlockKeeps); any other string is double-quoted. Numbers
// and bools encode as their type; nested maps (selectors / selectors_re) are
// rendered with the same rule for their string values.
func valueNode(v any) *yaml.Node {
	if s, ok := v.(string); ok {
		style := yaml.DoubleQuotedStyle
		if strings.Contains(s, "\n") && literalBlockKeeps(s) {
			style = yaml.LiteralStyle
		}
		return &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: s, Style: style}
	}
	if m, ok := v.(map[string]any); ok {
		keys := make([]string, 0, len(m))
		for k := range m {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		n := &yaml.Node{Kind: yaml.MappingNode, Tag: "!!map"}
		for _, k := range keys {
			n.Content = append(n.Content, &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: k}, valueNode(m[k]))
		}
		return n
	}
	var n yaml.Node
	_ = n.Encode(v) // numbers / bools
	return &n
}

// literalBlockKeeps reports whether s, written as a literal block where a
// recipe value sits, parses and reads back as s. Some texts do not: one
// opening with a tab is written as YAML that does not parse. (A text a block
// cannot carry at all, e.g. one with trailing spaces, the encoder itself
// writes double-quoted. One opening with a line break lost it under yaml.v3
// v3.0.1's encoder; go.yaml.in/yaml/v3 v3.0.5's writes the blank line, #3206.)
func literalBlockKeeps(s string) bool {
	// An encode error leaves out short, and a text that does not parse leaves
	// back without it: either way the comparison fails, so neither error is
	// checked on its own. The block is probed at the top level; the indent
	// a block scalar carries is relative, so the recipe's nesting reads the
	// same (TestMerge_MultilineStringIsALiteralBlock reads the file back).
	out, _ := yaml.Marshal(&yaml.Node{Kind: yaml.MappingNode, Tag: "!!map", Content: []*yaml.Node{
		{Kind: yaml.ScalarNode, Tag: "!!str", Value: "v"},
		{Kind: yaml.ScalarNode, Tag: "!!str", Value: s, Style: yaml.LiteralStyle}}})
	var back map[string]string
	_ = yaml.Unmarshal(out, &back)
	return reflect.DeepEqual(back, map[string]string{"v": s})
}

// stringTypedContractFields are the recipe fields whose authored TEXT is
// read as written and therefore MUST arrive as JSON strings (schema type:
// string): quantile (#1017, enters the recipe_id slug) and objective
// (ADR-031, read by the exporter's resolver and the custom-alert compiler,
// both pkg/config, #3229). The map value is the example quoted in the violation message;
// both come from the recipe contract (config.RecipeQuotedTextFields, #3229).
var stringTypedContractFields, stringTypedContractFieldNames = config.RecipeQuotedTextFields()

// QuantileStringViolations rejects any recipe whose string-typed contract
// field (quantile, objective — see stringTypedContractFields) arrives as a
// non-string JSON value (number/bool/null) — one violation per offending
// field, carrying the same `_custom_alerts[N]` locator prefix the S5
// validator uses so the portal modal can point at the offending card. (Name
// kept from the original #1017 quantile-only check; it now covers every
// string-typed contract field so the handler call site is unchanged.)
//
// WHY reject instead of normalise (#1017): the field TEXT is what the
// exporter and the compiler read. A JSON number has already lost its authored
// text (0.990 → float64 → "0.99"), and valueNode would emit it as a BARE YAML
// scalar — which YAML dialects disagree on (YAML 1.1 readers such as PyYAML
// read a dotless exponent like 95e-2 as a string, yaml.v3 as a float), so a
// reader of the file other than pkg/config can see another recipe than the
// one compiled. The schema is string-only
// (tenant-config.schema.json), so the write plane fails loud here, symmetric
// with the conf.d schema CI gate (check_confd_schema.py).
func QuantileStringViolations(recipes []map[string]any) []string {
	var viol []string
	for i, r := range recipes {
		for _, field := range stringTypedContractFieldNames { // sorted: deterministic order
			v, present := r[field]
			if !present {
				continue
			}
			if _, ok := v.(string); ok {
				continue
			}
			name, _ := r["name"].(string)
			viol = append(viol, fmt.Sprintf(
				"_custom_alerts[%d] (name %q): %s must be a JSON string (e.g. %s), got %T — "+
					"the %s text is read as written; a number has lost that text and a bare "+
					"YAML number is read differently by YAML dialects (#1017)",
				i, name, field, stringTypedContractFields[field], v, field))
		}
	}
	return viol
}

// mapValue returns the value node for key in a mapping node, or nil.
func mapValue(m *yaml.Node, key string) *yaml.Node {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			return m.Content[i+1]
		}
	}
	return nil
}

// setMapValue replaces the value node for key (preserving the key node + its
// comments), or appends a new key/value pair if absent.
func setMapValue(m *yaml.Node, key string, val *yaml.Node) {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			m.Content[i+1] = val
			return
		}
	}
	keyNode := &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: key}
	m.Content = append(m.Content, keyNode, val)
}

// deleteMapKey removes the key/value pair for key from a mapping node.
func deleteMapKey(m *yaml.Node, key string) {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			m.Content = append(m.Content[:i], m.Content[i+2:]...)
			return
		}
	}
}

// DropImplicitTags clears the tag of every node whose tag was not written
// (no explicit `!!x`), before a re-encode (#2903). yaml.v3 fills in the tag
// it resolved, and its encoder writes some of those back out explicitly —
// a `<<` merge key comes back as `!!merge <<` — so a file would not be
// re-emitted as it was written. With the tag cleared the encoder resolves
// it again and writes the node as it came.
func DropImplicitTags(n *yaml.Node) {
	if n == nil {
		return
	}
	if n.Kind != yaml.AliasNode && n.Style&yaml.TaggedStyle == 0 {
		n.Tag = ""
	}
	for _, c := range n.Content {
		DropImplicitTags(c)
	}
}
