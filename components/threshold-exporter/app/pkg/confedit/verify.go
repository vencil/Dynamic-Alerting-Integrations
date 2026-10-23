package confedit

// The write invariant (ADR-036 §4, #2903). A write decodes the file into
// yaml.v3 nodes, edits some keys and re-encodes the WHOLE file. A reader
// that judges only the result cannot tell a value the edit meant to change
// from one the re-encode changed on its own (a block scalar's leading blank
// line, a plain scalar the encoder now quotes, an alias that now names
// another anchor, an anchored block another tenant aliases).
//
// So the result must keep, outside what the edit replaces, the very same
// written YAML: parsed back into nodes, every node has the same kind, style
// (flow or block, plain or which quotes), tag as written (explicit or
// implicit), scalar text, anchor and — for an alias — the same target, node
// for node; the documents are as many, and the file defines the same anchors
// in the same order (yaml.v3 lets an anchor be defined twice, so removing one
// definition can re-point an alias to an equal-looking node). The same
// characters mean the same to every reader, whichever one reads them. On top
// of that, the edited keys must, decoded, be exactly what the edit says (a
// set key is there — holding its value when the edit names one —, an unset
// key is gone): a `<<` merge key can still supply a key whose explicit
// entry was removed. Comments and positions are not compared.

import (
	"bytes"
	"fmt"
	"reflect"

	"gopkg.in/yaml.v3"
)

// Op is what an edit does to the key at its path.
type Op int

const (
	// OpSet: the key holds the edit's value (replaced or added).
	OpSet Op = iota
	// OpUnset: the key is gone.
	OpUnset
	// OpEnsure: the key holds a mapping — added when the file had none
	// there, else left as it was (and compared as unedited).
	OpEnsure
)

// Edit is one key a write changes, by its path from the top level of the
// file's first document (mapping keys, by their text).
type Edit struct {
	Path []string
	Op   Op
	// Value (OpSet only, optional): the string the key must hold, written
	// double-quoted (a batch patch value). Nil: the value is the write's
	// own and is not checked here.
	Value *string
}

// DiffKind is what Verify found different.
type DiffKind string

// The closed set of DiffKind values.
const (
	DiffBaseUnparseable   DiffKind = "base_unparseable"   // the file being replaced does not parse
	DiffMergedUnparseable DiffKind = "merged_unparseable" // the text to be written does not parse
	DiffDocumentCount     DiffKind = "document_count"     // a different number of YAML documents
	DiffNoDocument        DiffKind = "no_document"        // the file being replaced holds no document
	DiffAnchors           DiffKind = "anchors"            // the anchors defined, or their order
	DiffNode              DiffKind = "node"               // a node outside the edit is written differently
	DiffEditParent        DiffKind = "edit_parent"        // an edited key's mapping would not be a mapping
	DiffEditDecode        DiffKind = "edit_decode"        // an edited key's mapping would not decode
	DiffEditKey           DiffKind = "edit_key"           // an edited key would not read as the edit says
)

// Difference is the first place a write breaks the invariant.
type Difference struct {
	Kind DiffKind
	// Path: DiffNode — where the walk found the difference (mapping keys by
	// their text, "[i]" for a sequence item); DiffEditParent / DiffEditDecode
	// — the edited key's mapping; DiffEditKey — the edited key.
	Path []string
	// Docs (DiffDocumentCount): the base's and the result's document counts.
	Docs [2]int
}

// String describes the difference for a person (the path in full: a
// caller that must not name other tenants' paths renders its own text).
func (d *Difference) String() string {
	switch d.Kind {
	case DiffBaseUnparseable:
		return "the current file does not parse"
	case DiffMergedUnparseable:
		return "the written file would not parse"
	case DiffDocumentCount:
		return fmt.Sprintf("the number of YAML documents (%d, would be %d)", d.Docs[0], d.Docs[1])
	case DiffNoDocument:
		return "the current file has no document"
	case DiffAnchors:
		return "the anchors the file defines (the edit would remove or add one)"
	case DiffEditParent:
		return JoinPath(d.Path) + " would not be a mapping"
	case DiffEditDecode:
		return JoinPath(d.Path) + " would not decode"
	case DiffEditKey:
		return JoinPath(d.Path) + " would not read as the edit says"
	}
	return "a node outside the edit would be written differently, at " + JoinPath(d.Path)
}

// Verify returns nil when merged keeps base's written YAML everywhere
// outside edits, and the edited keys read as the edits say; else the first
// difference. An empty base (a new file) has nothing to keep.
//
// The edits are located in the first document; every other document is
// compared whole. Along an edit's path the walk follows mapping keys by
// their text: an edited key (OpSet, OpUnset) is skipped on both sides; an
// OpEnsure key the base lacks is skipped in merged (refused if the base
// carries it as an alias key — the edit would add a second key for it);
// any other key is compared.
func Verify(base, merged []byte, edits []Edit) *Difference {
	if len(bytes.TrimSpace(base)) == 0 {
		return nil
	}
	bDocs, err := ParseAll(base)
	if err != nil {
		return &Difference{Kind: DiffBaseUnparseable}
	}
	mDocs, err := ParseAll(merged)
	if err != nil {
		return &Difference{Kind: DiffMergedUnparseable}
	}
	if len(bDocs) != len(mDocs) {
		return &Difference{Kind: DiffDocumentCount, Docs: [2]int{len(bDocs), len(mDocs)}}
	}
	if len(bDocs) == 0 {
		return &Difference{Kind: DiffNoDocument}
	}
	if !reflect.DeepEqual(anchorsOf(bDocs), anchorsOf(mDocs)) {
		return &Difference{Kind: DiffAnchors}
	}
	c := &compare{edits: edits, seen: map[[2]*yaml.Node]bool{}}
	for i := range bDocs {
		var at []string
		if i == 0 {
			at = []string{} // the edits' paths start here
		}
		if path, ok := c.eq(bDocs[i], mDocs[i], nil, at); !ok {
			return &Difference{Kind: DiffNode, Path: path}
		}
	}
	return checkEdits(mDocs[0], edits)
}

type compare struct {
	edits []Edit
	seen  map[[2]*yaml.Node]bool
}

// direct is the edits whose key is a key of the mapping at at.
func (c *compare) direct(at []string) map[string]Op {
	out := map[string]Op{}
	for _, e := range c.edits {
		if len(e.Path) == len(at)+1 && hasPrefix(e.Path, at) {
			k := e.Path[len(at)]
			// A set or unset of a key wins over an ensure of it.
			if prev, ok := out[k]; !ok || prev == OpEnsure {
				out[k] = e.Op
			}
		}
	}
	return out
}

// below reports whether an edit's path runs through at.
func (c *compare) below(at []string) bool {
	for _, e := range c.edits {
		if len(e.Path) > len(at) && hasPrefix(e.Path, at) {
			return true
		}
	}
	return false
}

func hasPrefix(p, prefix []string) bool {
	if len(p) < len(prefix) {
		return false
	}
	for i := range prefix {
		if p[i] != prefix[i] {
			return false
		}
	}
	return true
}

// emptiedByUnset: b and m are the mapping an unset edits, the unset left it
// empty, and only block-or-flow differs — an empty block mapping is written
// `{}`. That mapping is part of the edit; everything else about it (the
// tag, written or not, and the anchor) is still compared.
//
// No `at == nil` guard: off every edit path (at nil) a mapping that b holds
// keys in and m none fails the pair count below whatever its style, and an
// empty b is already `{}`.
func (c *compare) emptiedByUnset(b, m *yaml.Node, at []string) bool {
	if b.Kind != yaml.MappingNode || m.Kind != yaml.MappingNode || len(m.Content) != 0 ||
		b.Style|yaml.FlowStyle != m.Style|yaml.FlowStyle ||
		b.Tag != m.Tag || b.Value != m.Value || b.Anchor != m.Anchor {
		return false
	}
	for _, op := range c.direct(at) {
		if op == OpUnset {
			return true
		}
	}
	return false
}

func sameNode(b, m *yaml.Node) bool {
	return b.Kind == m.Kind && b.Style == m.Style && b.Tag == m.Tag && b.Value == m.Value && b.Anchor == m.Anchor
}

func extend(path []string, seg string) []string {
	return append(append([]string{}, path...), seg)
}

// eq reports whether b and m are the same written YAML. path is where the
// walk is (for the report); at is the same position as an edit path while
// the walk is on one (nil once it leaves every edit's path, or follows an
// alias or a sequence).
func (c *compare) eq(b, m *yaml.Node, path, at []string) ([]string, bool) {
	if b == nil || m == nil {
		return path, b == m
	}
	if b.Kind == yaml.DocumentNode && m.Kind == yaml.DocumentNode {
		if len(b.Content) != len(m.Content) {
			return path, false
		}
		for i := range b.Content {
			if p, ok := c.eq(b.Content[i], m.Content[i], path, at); !ok {
				return p, false
			}
		}
		return nil, true
	}
	if !sameNode(b, m) && !c.emptiedByUnset(b, m, at) {
		return path, false
	}
	if b.Kind == yaml.AliasNode {
		if b.Alias == nil || m.Alias == nil {
			return path, b.Alias == m.Alias
		}
		key := [2]*yaml.Node{b.Alias, m.Alias}
		if c.seen[key] {
			return nil, true
		}
		c.seen[key] = true
		// The target, written the same, wherever it is — not just its name.
		return c.eq(b.Alias, m.Alias, path, nil)
	}
	if b.Kind != yaml.MappingNode {
		if len(b.Content) != len(m.Content) {
			return path, false
		}
		for i := range b.Content {
			if p, ok := c.eq(b.Content[i], m.Content[i], extend(path, fmt.Sprintf("[%d]", i)), nil); !ok {
				return p, false
			}
		}
		return nil, true
	}
	bp, mp := pairs(b), pairs(m)
	if at != nil {
		for k, op := range c.direct(at) {
			switch op {
			case OpSet, OpUnset:
				bp, mp = without(bp, k), without(mp, k)
			case OpEnsure:
				if keyIndex(bp, k) >= 0 {
					continue // there: compared, and descended
				}
				// ...unless the key is already there, written as an
				// alias: the write would add a second key for it.
				if resolvedKeyCount(bp, k) > 0 {
					return path, false
				}
				mp = without(mp, k)
			}
		}
	}
	if len(bp) != len(mp) {
		return path, false
	}
	for i := range bp {
		kb, km := bp[i][0], mp[i][0]
		sub := extend(path, kb.Value)
		if p, ok := c.eq(kb, km, sub, nil); !ok {
			return p, false
		}
		var next []string
		if at != nil && kb.Kind == yaml.ScalarNode {
			if n := extend(at, kb.Value); c.below(n) {
				next = n
			}
		}
		if p, ok := c.eq(bp[i][1], mp[i][1], sub, next); !ok {
			return p, false
		}
	}
	return nil, true
}

func pairs(m *yaml.Node) [][2]*yaml.Node {
	out := make([][2]*yaml.Node, 0, len(m.Content)/2)
	for i := 0; i+1 < len(m.Content); i += 2 {
		out = append(out, [2]*yaml.Node{m.Content[i], m.Content[i+1]})
	}
	return out
}

// without drops the pairs whose key is the scalar text k.
func without(ps [][2]*yaml.Node, k string) [][2]*yaml.Node {
	out := make([][2]*yaml.Node, 0, len(ps))
	for _, p := range ps {
		if p[0].Kind == yaml.ScalarNode && p[0].Value == k {
			continue
		}
		out = append(out, p)
	}
	return out
}

func keyIndex(ps [][2]*yaml.Node, text string) int {
	for i, p := range ps {
		if p[0].Kind == yaml.ScalarNode && p[0].Value == text {
			return i
		}
	}
	return -1
}

// resolvedKeyCount is how many keys of ps are text, an alias followed.
func resolvedKeyCount(ps [][2]*yaml.Node, text string) int {
	n := 0
	for _, p := range ps {
		k := p[0]
		for k != nil && k.Kind == yaml.AliasNode {
			k = k.Alias
		}
		if k != nil && k.Kind == yaml.ScalarNode && k.Value == text {
			n++
		}
	}
	return n
}

// mappingIn is the mapping at path in the merged first document, following
// scalar keys by their text (the last pair wins, as the walk's does), or nil.
func mappingIn(doc *yaml.Node, path []string) *yaml.Node {
	if doc.Kind != yaml.DocumentNode || len(doc.Content) == 0 {
		return nil
	}
	cur := doc.Content[0]
	for _, k := range path {
		if cur.Kind != yaml.MappingNode {
			return nil
		}
		var next *yaml.Node
		for _, p := range pairs(cur) {
			if p[0].Kind == yaml.ScalarNode && p[0].Value == k {
				next = p[1]
			}
		}
		if next == nil {
			return nil
		}
		cur = next
	}
	return cur
}

// checkEdits: in the merged first document, decoded (so a `<<` merge key
// counts), every set key is there (holding its value, a double-quoted
// string, when the edit names one) and every unset key is gone.
func checkEdits(doc *yaml.Node, edits []Edit) *Difference {
	for _, e := range edits {
		if e.Op == OpEnsure || len(e.Path) == 0 {
			continue
		}
		parentPath, key := e.Path[:len(e.Path)-1], e.Path[len(e.Path)-1]
		parent := mappingIn(doc, parentPath)
		if parent == nil || parent.Kind != yaml.MappingNode {
			return &Difference{Kind: DiffEditParent, Path: clonePath(parentPath)}
		}
		var decoded map[string]any
		if err := decodeSafely(parent, &decoded); err != nil {
			return &Difference{Kind: DiffEditDecode, Path: clonePath(parentPath)}
		}
		mp := pairs(parent)
		if resolvedKeyCount(mp, key) > 1 { // written twice, one an alias
			return &Difference{Kind: DiffEditKey, Path: clonePath(e.Path)}
		}
		v, present := decoded[key]
		bad := false
		switch {
		case e.Op == OpUnset:
			bad = present
		case !present:
			bad = true
		case e.Value != nil:
			j := keyIndex(mp, key)
			s, ok := v.(string)
			bad = !ok || s != *e.Value || j < 0 || mp[j][1].Style != yaml.DoubleQuotedStyle
		}
		if bad {
			return &Difference{Kind: DiffEditKey, Path: clonePath(e.Path)}
		}
	}
	return nil
}

// decodeSafely is Node.Decode that turns a yaml.v3 panic into an error.
func decodeSafely(n *yaml.Node, v any) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("decode: %v", r)
		}
	}()
	return n.Decode(v)
}

// anchorsOf is every anchor name the documents define, in document order
// (keys included; an alias is not followed).
func anchorsOf(docs []*yaml.Node) []string {
	out := []string{}
	var walk func(n *yaml.Node)
	walk = func(n *yaml.Node) {
		if n == nil || n.Kind == yaml.AliasNode {
			return
		}
		if n.Anchor != "" {
			out = append(out, n.Anchor)
		}
		for _, k := range n.Content {
			walk(k)
		}
	}
	for _, d := range docs {
		walk(d)
	}
	return out
}
