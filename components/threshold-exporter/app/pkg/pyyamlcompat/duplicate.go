package pyyamlcompat

import (
	"bytes"
	"fmt"

	"gopkg.in/yaml.v3"
)

// Duplicate is the first mapping key a document writes twice, by the route
// generator's rule (see FindDuplicateKey).
type Duplicate struct {
	Key       string // the key's text; "<non-scalar>" for a mapping or list key
	Line      int    // 1-based position of the second occurrence as written
	Column    int
	FirstLine int // 1-based line of the first occurrence as written
}

func (d *Duplicate) Error() string {
	return fmt.Sprintf("line %d: mapping key %q already defined at line %d "+
		"(the route generator refuses the whole file)", d.Line, d.Key, d.FirstLine)
}

// FindDuplicateKey returns the first repeated key of any mapping under n (a
// document node or any node of one), or nil. It is the route generator's
// verdict (#2295): scripts/tools/_lib_io.py reads conf.d with a StrictLoader
// that runs find_duplicate_key over the composed document and refuses the
// WHOLE file on a hit, whatever mapping the hit is in.
//
// Key identity is _lib_io._key_identity, copied: node kind + raw text (the
// text only for a scalar, so any two mapping keys, or any two list keys, of
// one mapping are the same key). What that means here:
//
//   - `1` and `'1'`, `true` and `'true'`, `<<` and `'<<'` are one key: the
//     style and the tag are not part of it.
//   - `true` and `True`, `1` and `01` are two keys: nothing is resolved.
//   - A merge key `<<` is an ordinary entry of its mapping: two of them are a
//     duplicate. The keys a merge brings in are NOT entries of the mapping
//     and are never compared with its own keys (the StrictLoader checks the
//     composed tree, before flatten_mapping).
//   - An alias used as a key is its anchor's node (PyYAML's composer hands
//     back the anchored node itself): kind and text of the anchor, one deref.
//
// The walk looks at each mapping's own keys and descends into keys and
// values, but never through an alias: every anchored node is also written
// in place somewhere in the same document, where the walk reaches it. So
// each node is visited once — O(nodes), a self-referencing anchor cannot
// loop and an alias fan-out cannot multiply the work.
func FindDuplicateKey(n *yaml.Node) *Duplicate {
	if n == nil {
		return nil
	}
	stack := []*yaml.Node{n}
	for len(stack) > 0 {
		cur := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		switch cur.Kind {
		case yaml.MappingNode:
			if d := duplicateInMapping(cur); d != nil {
				return d
			}
			for i := len(cur.Content) - 1; i >= 0; i-- {
				stack = append(stack, cur.Content[i])
			}
		case yaml.DocumentNode, yaml.SequenceNode:
			for i := len(cur.Content) - 1; i >= 0; i-- {
				stack = append(stack, cur.Content[i])
			}
		}
		// ScalarNode: nothing below. AliasNode: its target is walked where
		// it is written, never from here.
	}
	return nil
}

type keyIdentity struct {
	kind yaml.Kind
	text string
}

// identity is _lib_io._key_identity for a yaml.v3 key node.
func identity(k *yaml.Node) keyIdentity {
	if k.Kind == yaml.AliasNode && k.Alias != nil {
		k = k.Alias // one deref: an anchor is never an alias itself
	}
	if k.Kind == yaml.ScalarNode {
		return keyIdentity{kind: k.Kind, text: k.Value}
	}
	return keyIdentity{kind: k.Kind}
}

// duplicateInMapping is _lib_io.duplicate_in_mapping: the mapping's own
// entries only.
func duplicateInMapping(m *yaml.Node) *Duplicate {
	seen := make(map[keyIdentity]*yaml.Node, len(m.Content)/2)
	for i := 0; i+1 < len(m.Content); i += 2 {
		k := m.Content[i]
		id := identity(k)
		if first, ok := seen[id]; ok {
			key := id.text
			if id.kind != yaml.ScalarNode {
				key = "<non-scalar>"
			}
			return &Duplicate{Key: key, Line: k.Line, Column: k.Column, FirstLine: first.Line}
		}
		seen[id] = k
	}
	return nil
}

// FindDuplicateKeyIn is FindDuplicateKey over every document of data. A
// stream yaml.v3 cannot parse returns nil: the reader that parses it reports
// that error itself.
func FindDuplicateKeyIn(data []byte) *Duplicate {
	dec := yaml.NewDecoder(bytes.NewReader(data))
	for {
		var doc yaml.Node
		if dec.Decode(&doc) != nil { // io.EOF included
			return nil
		}
		if d := FindDuplicateKey(&doc); d != nil {
			return d
		}
	}
}
