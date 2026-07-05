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
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
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
// logical (recipe → identity → metric(s) → params → routing), matching how
// the recipe library + hand-written conf.d entries read. Keys not listed are
// appended after (sorted) so a future schema field is never silently dropped.
var recipeFieldOrder = []string{
	"recipe", "name", "metric", "denominator_metric", "capacity_metric",
	"op", "window", "horizon", "quantile", "threshold",
	"selectors", "selectors_re", "mode", "for",
}

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

// valueNode renders a recipe field value. String scalars containing a colon
// (e.g. `value:severity`) or other quote-worthy chars are force double-quoted
// to match the source convention + stay unambiguous for downstream parsers;
// nested maps (selectors / selectors_re) and non-strings encode naturally.
func valueNode(v any) *yaml.Node {
	if s, ok := v.(string); ok {
		n := &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: s}
		if strings.ContainsAny(s, ":#") || s == "" || strings.TrimSpace(s) != s {
			n.Style = yaml.DoubleQuotedStyle
		}
		return n
	}
	var n yaml.Node
	_ = n.Encode(v) // numbers / bools / nested maps (selectors)
	return &n
}

// QuantileStringViolations rejects any recipe whose `quantile` arrives as a
// non-string JSON value (number/bool/null) — one violation per offending
// recipe, carrying the same `_custom_alerts[N]` locator prefix the S5
// validator uses so the portal modal can point at the offending card.
//
// WHY reject instead of normalise (#1017): the quantile TEXT enters the
// cross-language recipe_id slug that the Go exporter and the Python compiler
// compute independently. A JSON number has already lost its authored text
// (0.990 → float64 → "0.99"), and valueNode would emit it as a BARE YAML
// scalar — which the two YAML readers can disagree on (PyYAML 1.1 reads a
// dotless exponent like 95e-2 as a string, yaml.v3 as a float), silently
// splitting the recipe_id join and muting the alert. The schema is
// string-only (tenant-config.schema.json), so the write plane fails loud
// here, symmetric with the conf.d schema CI gate (check_confd_schema.py).
func QuantileStringViolations(recipes []map[string]any) []string {
	var viol []string
	for i, r := range recipes {
		q, present := r["quantile"]
		if !present {
			continue
		}
		if _, ok := q.(string); ok {
			continue
		}
		name, _ := r["name"].(string)
		viol = append(viol, fmt.Sprintf(
			"_custom_alerts[%d] (name %q): quantile must be a JSON string (e.g. \"0.99\"), got %T — "+
				"the quantile text enters the cross-language recipe_id contract; a bare number is "+
				"YAML-dialect-ambiguous and silently breaks the Go/Python join (#1017)",
			i, name, q))
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
