package main

import (
	"errors"
	"fmt"

	"go.yaml.in/yaml/v3"
)

// What the text writes: the file composed into yaml.v3 nodes (the node tree,
// not a decode — no value is typed, every key keeps its text), with `<<`
// merges applied the way the go-yaml v2 decoder Alertmanager loads with
// (gopkg.in/yaml.v2 v2.4.0, decoder.mappingStruct / decoder.merge) applies
// them: in written order, a later key replaces an earlier one — a `<<`
// written after an explicit key replaces it too — and in a merged sequence an
// earlier mapping takes precedence. A key a merge brings in counts as
// written. The same rules as threshold-exporter's cmd/da-crdecode/aswritten.go
// (which measures them against go-yaml v2); TestMergedKeysAreWritten measures
// them here against Alertmanager's loader.

// errNotComposed wraps the reason the text could not be composed or merged.
var errNotComposed = errors.New("the file loads in Alertmanager, but it cannot be read as written " +
	"to tell which keys it writes")

// writtenMap is a mapping node's entries after `<<` merges, keyed by key text.
type writtenMap struct {
	order []string
	value map[string]*yaml.Node
}

type composed struct {
	merged  map[*yaml.Node]*writtenMap
	inMerge map[*yaml.Node]bool
}

func newComposed() *composed {
	return &composed{merged: map[*yaml.Node]*writtenMap{}, inMerge: map[*yaml.Node]bool{}}
}

func deref(n *yaml.Node) *yaml.Node {
	for n != nil && n.Kind == yaml.AliasNode {
		n = n.Alias
	}
	return n
}

// isMerge is go-yaml v2's isMerge on a yaml.v3 node: the scalar `<<`, plain
// and untagged (v2's "implicit"), or tagged !!merge.
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

// entries is *m*'s keys and values as go-yaml v2 builds the mapping from it.
func (c *composed) entries(m *yaml.Node) (*writtenMap, error) {
	if got, ok := c.merged[m]; ok {
		return got, nil
	}
	if c.inMerge[m] {
		return nil, fmt.Errorf("%w: a mapping merges itself (line %d)", errNotComposed, m.Line)
	}
	c.inMerge[m] = true
	defer delete(c.inMerge, m)
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
			src := deref(v)
			switch {
			case src != nil && src.Kind == yaml.MappingNode:
				if err := apply(src, true, true); err != nil {
					return nil, err
				}
			case src != nil && src.Kind == yaml.SequenceNode:
				// The order of first appearance follows the sequence; the
				// value is the earliest mapping's (v2 steps backwards).
				items := make([]*yaml.Node, len(src.Content))
				for j, it := range src.Content {
					items[j] = deref(it)
					if items[j] == nil || items[j].Kind != yaml.MappingNode {
						return nil, fmt.Errorf("%w: a << merge of a non-mapping (line %d)", errNotComposed, it.Line)
					}
				}
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
			default:
				return nil, fmt.Errorf("%w: a << merge of a non-mapping (line %d)", errNotComposed, v.Line)
			}
			continue
		}
		kd := deref(k)
		if kd == nil || kd.Kind != yaml.ScalarNode {
			return nil, fmt.Errorf("%w: a key that is not a scalar (line %d)", errNotComposed, k.Line)
		}
		set(kd.Value, v)
	}
	c.merged[m] = wm
	return wm, nil
}
