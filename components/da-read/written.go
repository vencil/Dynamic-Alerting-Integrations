package main

import (
	"errors"
	"fmt"

	"go.yaml.in/yaml/v3"
)

// What the text writes: the file composed into yaml.v3 nodes (the node tree,
// not a decode — no value is typed, every key keeps its text), with `<<`
// merges applied the way the consumer's YAML decoder applies them. A key a
// merge brings in counts as written. The decoders differ, so each consumer
// names its rules (mergeRules), measured against its own loader:
//
//   - mergeV2 — go-yaml v2 (gopkg.in/yaml.v2 v2.4.0, which Alertmanager's
//     config.Load decodes with, and go.yaml.in/yaml/v2 v2.4.4, which
//     Prometheus' config.Load decodes with; decoder.mappingStruct /
//     decoder.merge): in written order, a later key replaces an earlier one —
//     a `<<` written after an explicit key replaces it too — and in a merged
//     sequence an earlier mapping takes precedence. The same rules as
//     threshold-exporter's cmd/da-crdecode/aswritten.go (which measures them
//     against go-yaml v2); TestMergedKeysAreWritten measures them against
//     Alertmanager's loader, TestPrometheusMergedKeysAreWritten against
//     Prometheus'.
//   - mergeV3 — go-yaml v3 (go.yaml.in/yaml/v3 v3.0.5, which Prometheus'
//     rulefmt.Parse decodes with; decoder.mapping / mappingStruct / merge):
//     an explicit key wins over a merged one wherever it is written, and in a
//     merged sequence an earlier mapping takes precedence (with its own
//     merges before the next mapping). A second `<<` in one mapping is
//     refused by v3 ("mapping key already defined") before this runs.
//     TestRuleMergedKeysAreWritten measures them against rulefmt.

// errNotComposed wraps the reason the text could not be composed or merged.
var errNotComposed = errors.New("the file loads, but it cannot be read as written " +
	"to tell which keys it writes")

// mergeRules names a YAML decoder's `<<` semantics.
type mergeRules int

const (
	mergeV2 mergeRules = iota
	mergeV3
)

// writtenMap is a mapping node's entries after `<<` merges, keyed by key text.
type writtenMap struct {
	order []string
	value map[string]*yaml.Node
}

type composed struct {
	rules   mergeRules
	merged  map[*yaml.Node]*writtenMap
	inMerge map[*yaml.Node]bool
}

// newComposed composes with go-yaml v2's merge rules (Alertmanager's).
func newComposed() *composed { return newComposedWith(mergeV2) }

func newComposedWith(rules mergeRules) *composed {
	return &composed{rules: rules, merged: map[*yaml.Node]*writtenMap{}, inMerge: map[*yaml.Node]bool{}}
}

func deref(n *yaml.Node) *yaml.Node {
	for n != nil && n.Kind == yaml.AliasNode {
		n = n.Alias
	}
	return n
}

// isMerge is go-yaml v2's isMerge on a yaml.v3 node: the scalar `<<`, plain
// and untagged (v2's "implicit"), or tagged !!merge. It is v3's too: v3's
// parser tags a plain `<<` !!merge and a quoted one !!str.
func isMerge(k *yaml.Node) bool {
	const notImplicit = yaml.TaggedStyle | yaml.DoubleQuotedStyle | yaml.SingleQuotedStyle |
		yaml.LiteralStyle | yaml.FoldedStyle
	return k.Kind == yaml.ScalarNode && k.Value == "<<" &&
		(k.Style&notImplicit == 0 || k.Tag == "!!merge")
}

// isNull is a scalar go-yaml reads as null: `~`, `null` and an empty plain
// scalar, untagged, or tagged !!null.
func isNull(n *yaml.Node) bool {
	if n == nil {
		return true
	}
	if n.Kind != yaml.ScalarNode {
		return false
	}
	if n.Tag == "!!null" {
		return true
	}
	return n.Style == 0 && (n.Tag == "" || n.Tag == "!!null") &&
		(n.Value == "" || n.Value == "~" || n.Value == "null" || n.Value == "Null" || n.Value == "NULL")
}

// mergeSources is the mappings the `<<` value *v* merges, in written order.
func mergeSources(v *yaml.Node) ([]*yaml.Node, error) {
	src := deref(v)
	switch {
	case src != nil && src.Kind == yaml.MappingNode:
		return []*yaml.Node{src}, nil
	case src != nil && src.Kind == yaml.SequenceNode:
		items := make([]*yaml.Node, len(src.Content))
		for j, it := range src.Content {
			items[j] = deref(it)
			if items[j] == nil || items[j].Kind != yaml.MappingNode {
				return nil, fmt.Errorf("%w: a << merge of a non-mapping (line %d)", errNotComposed, it.Line)
			}
		}
		return items, nil
	}
	return nil, fmt.Errorf("%w: a << merge of a non-mapping (line %d)", errNotComposed, v.Line)
}

// entries is *m*'s keys and values as the consumer's decoder builds the
// mapping from it.
func (c *composed) entries(m *yaml.Node) (*writtenMap, error) {
	if got, ok := c.merged[m]; ok {
		return got, nil
	}
	if c.inMerge[m] {
		return nil, fmt.Errorf("%w: a mapping merges itself (line %d)", errNotComposed, m.Line)
	}
	c.inMerge[m] = true
	defer delete(c.inMerge, m)
	var wm *writtenMap
	var err error
	if c.rules == mergeV3 {
		wm, err = c.entriesV3(m)
	} else {
		wm, err = c.entriesV2(m)
	}
	if err != nil {
		return nil, err
	}
	c.merged[m] = wm
	return wm, nil
}

// entriesV2 is go-yaml v2's mapping of *m*.
func (c *composed) entriesV2(m *yaml.Node) (*writtenMap, error) {
	wm := &writtenMap{value: map[string]*yaml.Node{}}
	set := func(text string, v *yaml.Node) {
		if _, seen := wm.value[text]; !seen {
			wm.order = append(wm.order, text)
		}
		wm.value[text] = v
	}
	apply := func(src *yaml.Node, setOrder, setValue bool) error {
		sub, err := c.entries(src)
		if err != nil {
			return err
		}
		for _, t := range sub.order {
			if _, seen := wm.value[t]; !seen && setOrder {
				wm.order = append(wm.order, t)
				wm.value[t] = sub.value[t]
			}
			if setValue {
				wm.value[t] = sub.value[t]
			}
		}
		return nil
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		k, v := m.Content[i], m.Content[i+1]
		if isMerge(k) {
			items, err := mergeSources(v)
			if err != nil {
				return nil, err
			}
			if len(items) == 1 && deref(v).Kind == yaml.MappingNode {
				if err := apply(items[0], true, true); err != nil {
					return nil, err
				}
				continue
			}
			// A merged sequence: the order of first appearance follows the
			// sequence; the value is the earliest mapping's (v2 steps
			// backwards).
			for _, it := range items {
				if err := apply(it, true, false); err != nil {
					return nil, err
				}
			}
			for j := len(items) - 1; j >= 0; j-- {
				if err := apply(items[j], false, true); err != nil {
					return nil, err
				}
			}
			continue
		}
		kd := deref(k)
		if kd == nil || kd.Kind != yaml.ScalarNode {
			return nil, fmt.Errorf("%w: a key that is not a scalar (line %d)", errNotComposed, k.Line)
		}
		set(kd.Value, v)
	}
	return wm, nil
}

// entriesV3 is go-yaml v3's mapping of *m*: the explicit keys in written
// order, then what the last `<<` brings in that no explicit key — and no
// earlier merged mapping — already holds.
func (c *composed) entriesV3(m *yaml.Node) (*writtenMap, error) {
	wm := &writtenMap{value: map[string]*yaml.Node{}}
	var mergeValue *yaml.Node
	for i := 0; i+1 < len(m.Content); i += 2 {
		k, v := m.Content[i], m.Content[i+1]
		if isMerge(k) {
			mergeValue = v // v3 refuses a second one before this runs
			continue
		}
		kd := deref(k)
		if kd == nil || kd.Kind != yaml.ScalarNode {
			return nil, fmt.Errorf("%w: a key that is not a scalar (line %d)", errNotComposed, k.Line)
		}
		if _, seen := wm.value[kd.Value]; !seen {
			wm.order = append(wm.order, kd.Value)
		}
		wm.value[kd.Value] = v
	}
	if mergeValue == nil {
		return wm, nil
	}
	sources, err := mergeSources(mergeValue)
	if err != nil {
		return nil, err
	}
	for _, src := range sources {
		sub, err := c.entries(src)
		if err != nil {
			return nil, err
		}
		for _, t := range sub.order {
			if _, seen := wm.value[t]; !seen {
				wm.order = append(wm.order, t)
				wm.value[t] = sub.value[t]
			}
		}
	}
	return wm, nil
}
