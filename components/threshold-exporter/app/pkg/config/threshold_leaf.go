package config

import (
	"strings"

	"gopkg.in/yaml.v3"
)

// A threshold's value has one reading (#3250, #3361): the text the file
// wrote — what the flat plane's typed decode keeps (ScheduledValue holds a
// scalar's text; a schedule's `default:` and each window's `value:` too) —
// through thresholdTextNumber. The exporter reads every layer that way:
// tenant entries, root platform `tenants:` entries, profiles and the root
// `defaults:` (readRootDefault).
//
// The generic decode behind /effective, merged_hash, redundant_override and
// key_sources, and behind the subtree overlay, typed the same text by YAML's
// rules instead, and for some spellings YAML's number is not the text's:
// `017` was 15 where the exporter reads 17, `0x1F` was 31 and `0o17` 15
// where it refuses the text, `.nan` a NaN and `true` a bool where it refuses
// the text too. withThresholdLeaves puts the text back for exactly those
// leaves, so every reader of the generic tree reads what the exporter does.
//
// ⛔ Only a leaf YAML misstates changes (thresholdLeaf): a string already is
// the text, and a YAML number equal to the text's reading (`70`, `1e6`,
// `1_000`, `-0`) stays as decoded — /effective stays the written view, and
// merged_hash moves only for a value whose meaning changed. Only threshold
// keys (not `_`-prefixed, baseRowsSkipKey) are touched: a reserved key is
// not a threshold, and its readers take the generic value.

// thresholdLeaf is a threshold leaf's value in the generic tree, given its
// generic decode g and the text the file wrote: g when g is a string (the
// text itself) or a number equal to the text's reading, else the text. A
// null stays null (#2518: no write).
func thresholdLeaf(g any, text string) any {
	switch g.(type) {
	case nil, string:
		return g
	}
	if v, ok := thresholdTextNumber(text); ok {
		// == ignores the sign of a zero: yaml.v3 decodes `-0` as int 0 and the
		// exporter reads -0, which every comparison of an alert treats as 0.
		if f, isNumber := genericNumber(g); isNumber && f == v {
			return g
		}
	}
	return text
}

// thresholdTextNumber is the one reading of a threshold's text as a number:
// parseThresholdNumber of the text, trimmed. thresholdLeaf (the generic
// tree) and readRootDefault (the exporter's root `defaults:`) both call it.
func thresholdTextNumber(text string) (float64, bool) {
	return parseThresholdNumber(strings.TrimSpace(text))
}

// genericNumber is g as a float64 when yaml.v3's generic decode typed it as
// a number.
func genericNumber(g any) (float64, bool) {
	switch n := g.(type) {
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	case uint64:
		return float64(n), true
	case float64:
		return n, true
	}
	return 0, false
}

// withThresholdLeaves rewrites, in place, every threshold leaf of block — one
// mapping of the generic tree (a defaults level, a tenant entry, a profile)
// — to thresholdLeaf of the text n, the same mapping's node, writes for it.
// The keys and texts are the typed decode's own (n decoded into
// map[string]yaml.Node: yaml.v3 resolves `<<` merges and aliases as the
// flat plane's decode does), so block and n agree key for key. A key whose
// value is a scalar takes that scalar's text; a schedule (a mapping with
// `default:`) has its `default:` and each window's `value:` rewritten the
// same way. Any other shape, and every `_` key, is left as decoded. nil n,
// or a node that is not a mapping, changes nothing.
func withThresholdLeaves(block map[string]any, n *yaml.Node) {
	if len(block) == 0 || n == nil {
		return
	}
	var nodes map[string]yaml.Node
	if n.Decode(&nodes) != nil {
		return
	}
	for k, node := range nodes {
		if baseRowsSkipKey(k) {
			continue
		}
		g, has := block[k]
		if !has {
			continue
		}
		switch vn := resolveAlias(&node); vn.Kind {
		case yaml.ScalarNode:
			block[k] = thresholdLeaf(g, vn.Value)
		case yaml.MappingNode:
			if m, ok := g.(map[string]any); ok {
				withScheduleLeaves(m, vn)
			}
		}
	}
}

// withScheduleLeaves is withThresholdLeaves for one schedule: the mapping's
// `default:` and the `value:` of each `overrides:` entry, when the mapping
// is one (it writes `default:`, as ScheduledValue requires).
func withScheduleLeaves(m map[string]any, n *yaml.Node) {
	var sched struct {
		Default   yaml.Node   `yaml:"default"`
		Overrides []yaml.Node `yaml:"overrides"`
	}
	if n.Decode(&sched) != nil || sched.Default.Kind == 0 {
		return
	}
	if dn := resolveAlias(&sched.Default); dn.Kind == yaml.ScalarNode {
		if g, has := m["default"]; has {
			m["default"] = thresholdLeaf(g, dn.Value)
		}
	}
	windows, ok := m["overrides"].([]any)
	if !ok || len(windows) != len(sched.Overrides) {
		return
	}
	for i := range sched.Overrides {
		w, ok := windows[i].(map[string]any)
		if !ok {
			continue
		}
		var win struct {
			Value yaml.Node `yaml:"value"`
		}
		if sched.Overrides[i].Decode(&win) != nil || win.Value.Kind == 0 {
			continue
		}
		if vn := resolveAlias(&win.Value); vn.Kind == yaml.ScalarNode {
			if g, has := w["value"]; has {
				w["value"] = thresholdLeaf(g, vn.Value)
			}
		}
	}
}

func resolveAlias(n *yaml.Node) *yaml.Node {
	for n != nil && n.Kind == yaml.AliasNode {
		n = n.Alias
	}
	return n
}

// documentMapping is the top-level mapping node of a decoded document, nil
// when the document is not a mapping.
func documentMapping(doc *yaml.Node) *yaml.Node {
	n := doc
	if n != nil && n.Kind == yaml.DocumentNode && len(n.Content) == 1 {
		n = n.Content[0]
	}
	n = resolveAlias(n)
	if n == nil || n.Kind != yaml.MappingNode {
		return nil
	}
	return n
}

// mappingValue is the value node of top-level key key in mapping m (the
// last one when written twice, as the generic decode keeps), nil when m
// does not write it. A key m has only through a `<<` merge is found too, as
// yaml.v3 resolves the merge (mergedPairs).
func mappingValue(m *yaml.Node, key string) *yaml.Node {
	if m == nil {
		return nil
	}
	var out *yaml.Node
	for i := 0; i+1 < len(m.Content); i += 2 {
		if k := m.Content[i]; k.Kind == yaml.ScalarNode && k.Value == key {
			out = m.Content[i+1]
		}
	}
	if out == nil {
		if pairs, ok := mergedPairs(m); ok {
			if n, has := pairs[key]; has {
				out = &n
			}
		}
	}
	return resolveAlias(out)
}

// mergedPairs is mapping m with its `<<` merges resolved — yaml.v3's own
// decode into map[string]yaml.Node — when m writes a merge key and decodes;
// ok=false otherwise (nothing to resolve, or the decode's own error).
func mergedPairs(m *yaml.Node) (map[string]yaml.Node, bool) {
	merges := false
	for i := 0; i+1 < len(m.Content); i += 2 {
		if k := m.Content[i]; k.Kind == yaml.ScalarNode && k.Tag == "!!merge" {
			merges = true
		}
	}
	if !merges {
		return nil, false
	}
	var pairs map[string]yaml.Node
	if m.Decode(&pairs) != nil {
		return nil, false
	}
	return pairs, true
}

// withTenantThresholdLeaves is withThresholdLeaves for each entry of block, a
// generically decoded `tenants:` (or `profiles:`) mapping, n being that
// mapping's node: entry id → body, keyed by the id's text as the flat plane
// keys it.
func withTenantThresholdLeaves(block map[string]any, n *yaml.Node) {
	if len(block) == 0 || n == nil || n.Kind != yaml.MappingNode {
		return
	}
	var bodies map[string]yaml.Node
	if n.Decode(&bodies) != nil {
		return
	}
	for id, node := range bodies {
		if body, ok := block[id].(map[string]any); ok {
			withThresholdLeaves(body, resolveAlias(&node))
		}
	}
}
