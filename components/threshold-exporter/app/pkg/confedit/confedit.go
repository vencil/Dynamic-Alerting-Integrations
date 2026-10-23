// Package confedit is the one writer of conf.d (ADR-036 §2 "寫 conf.d：Go 是唯一
// 的寫者", §4 "批次 patch 的合併"): edits on a file's yaml.v3 node tree, the
// re-encode, and the invariant every such write is held to — outside what
// the edit replaces, the result is the same written YAML as the file it
// replaces.
//
// Its users: tenant-api's batch patch and PUT .../custom-alerts merges, and
// `da-guard edit` / `da-guard emit`, which every Python tool that writes
// conf.d hands its intent to (ADR-036 step 10).
//
// ⛔ EDITS ARE NODE SURGERY, never a struct round trip. A file is decoded into
// yaml.v3 nodes, only the edited value nodes are replaced (or a key added /
// removed), and the whole tree is re-encoded: every node outside the edit —
// and the comments yaml.v3 attaches to it — is written back as it came.
// Verify then proves it on the bytes: anything else is refused before the
// write.
//
// ⛔ VALUE ENCODING (ADR-036 §4): a string the writer writes is double-quoted,
// whatever it holds, so no reader takes it for a timestamp, a number or a
// boolean; a string with a line break under `_custom_alerts` is a literal
// block (`|`) when that block reads back as the same string. Before the
// re-encode the tags yaml.v3 filled in are cleared (DropImplicitTags), or a
// `<<` merge key would come back as `!!merge <<`.
package confedit

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"reflect"
	"strings"

	"gopkg.in/yaml.v3"
)

// CustomAlertsKey is the tenant key whose multi-line strings are written as
// literal blocks (ADR-036 §4), so a diff shows a rule's lines.
const CustomAlertsKey = "_custom_alerts"

// MergeKey is the YAML merge key. An edit never names it: written plain it
// merges a mapping in, which no key-level edit means.
const MergeKey = "<<"

// ---------------------------------------------------------------- mappings

// MapValue returns the value node for key in mapping m, or nil: the first
// pair whose key node's Value is key.
func MapValue(m *yaml.Node, key string) *yaml.Node {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			return m.Content[i+1]
		}
	}
	return nil
}

// SetMapValue replaces the value node for key (the key node, its comments
// and its position kept), or appends a new pair whose key is written plain.
func SetMapValue(m *yaml.Node, key string, val *yaml.Node) {
	setMapValue(m, key, val, func(k string) *yaml.Node {
		return &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: k}
	})
}

func setMapValue(m *yaml.Node, key string, val *yaml.Node, keyNode func(string) *yaml.Node) {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			m.Content[i+1] = val
			return
		}
	}
	m.Content = append(m.Content, keyNode(key), val)
}

// DeleteMapValue removes key and its value from mapping m (a comment
// attached to the key goes with it) and reports whether it was there; a key
// the mapping does not carry is a no-op.
func DeleteMapValue(m *yaml.Node, key string) bool {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			m.Content = append(m.Content[:i], m.Content[i+2:]...)
			return true
		}
	}
	return false
}

// CarriesAny reports whether mapping m carries at least one of keys.
func CarriesAny(m *yaml.Node, keys []string) bool {
	for _, k := range keys {
		if MapValue(m, k) != nil {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------- documents

// ParseAll decodes every document of data into yaml.v3 nodes.
func ParseAll(data []byte) ([]*yaml.Node, error) {
	dec := yaml.NewDecoder(bytes.NewReader(data))
	var docs []*yaml.Node
	for {
		n := new(yaml.Node)
		if err := dec.Decode(n); err != nil {
			if errors.Is(err, io.EOF) {
				return docs, nil
			}
			return nil, err
		}
		docs = append(docs, n)
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

// Encode re-encodes doc the way every conf.d write does: implicit tags
// cleared, a 2-space indent — the conf.d convention, under which a file
// round-trips unchanged. A file authored with another indent reflows to 2
// spaces (its comments survive); Verify does not compare positions.
func Encode(doc *yaml.Node) ([]byte, error) {
	DropImplicitTags(doc)
	var buf bytes.Buffer
	enc := yaml.NewEncoder(&buf)
	enc.SetIndent(2)
	if err := enc.Encode(doc); err != nil {
		return nil, err
	}
	if err := enc.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// ---------------------------------------------------------------- values

// StringNode is s as the writer writes a string: double-quoted.
func StringNode(s string) *yaml.Node {
	return &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: s, Style: yaml.DoubleQuotedStyle}
}

// BlockStringNode is s as the writer writes a string under `_custom_alerts`:
// a literal block when s holds a line break and that block reads back as s
// (LiteralBlockKeeps), else double-quoted.
func BlockStringNode(s string) *yaml.Node {
	n := StringNode(s)
	if strings.Contains(s, "\n") && LiteralBlockKeeps(s) {
		n.Style = yaml.LiteralStyle
	}
	return n
}

// LiteralBlockKeeps reports whether s, written as a literal block, parses
// and reads back as s. Some texts do not: one opening with a tab is written
// as YAML that does not parse. (A text a block cannot carry at all, e.g. one
// with trailing spaces, the encoder itself writes double-quoted. One opening
// with a line break lost it under yaml.v3 v3.0.1's encoder; go.yaml.in/yaml/v3
// v3.0.5's writes the blank line, #3206.)
func LiteralBlockKeeps(s string) bool {
	// An encode error leaves out short, and a text that does not parse leaves
	// back without it: either way the comparison fails, so neither error is
	// checked on its own. The block is probed at the top level; the indent a
	// block scalar carries is relative, so a nested one reads the same.
	out, _ := yaml.Marshal(&yaml.Node{Kind: yaml.MappingNode, Tag: "!!map", Content: []*yaml.Node{
		{Kind: yaml.ScalarNode, Tag: "!!str", Value: "v"},
		{Kind: yaml.ScalarNode, Tag: "!!str", Value: s, Style: yaml.LiteralStyle}}})
	var back map[string]string
	_ = yaml.Unmarshal(out, &back)
	return reflect.DeepEqual(back, map[string]string{"v": s})
}

// KeyNode is a new mapping key k as the writer writes it: plain when yaml.v3
// reads the plain form back as the key k (the text is what every reader keys
// on: `010:` is the key "010"), else double-quoted — `~`, `null` and the empty
// key would read as a null key, `a: b` does not parse plain.
func KeyNode(k string) *yaml.Node {
	plain := &yaml.Node{Kind: yaml.ScalarNode, Value: k}
	out, err := yaml.Marshal(&yaml.Node{Kind: yaml.MappingNode, Content: []*yaml.Node{plain,
		{Kind: yaml.ScalarNode, Value: "0"}}})
	if err == nil {
		var back yaml.Node
		if yaml.Unmarshal(out, &back) == nil && len(back.Content) == 1 {
			m := back.Content[0]
			if m.Kind == yaml.MappingNode && len(m.Content) == 2 {
				kn := m.Content[0]
				if kn.Kind == yaml.ScalarNode && kn.Style == 0 && kn.Value == k && kn.ShortTag() != "!!null" && k != MergeKey {
					return &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: k}
				}
			}
		}
	}
	return StringNode(k)
}

// ---------------------------------------------------------------- one file

// Doc is one conf.d file being edited: its single document, and every edit
// made to it (what Verify is given).
type Doc struct {
	doc   *yaml.Node
	edits []Edit
}

// PathError is an edit that cannot be made on the file as it is: At is the
// path of the node that stops it.
type PathError struct {
	At     []string
	Reason string
}

func (e *PathError) Error() string {
	return fmt.Sprintf("%s: %s", JoinPath(e.At), e.Reason)
}

// JoinPath writes a key path for a message: keys joined with '.', the top
// level as "(top level)".
func JoinPath(p []string) string {
	if len(p) == 0 {
		return "(top level)"
	}
	return strings.Join(p, ".")
}

// ParseDoc decodes data for editing: one document whose top level is a
// mapping. An empty file (or one holding only whitespace) is an empty
// mapping; a file holding only comments is refused (its comments have no
// node to stay on).
func ParseDoc(data []byte) (*Doc, error) {
	docs, err := ParseAll(data)
	if err != nil {
		return nil, fmt.Errorf("the file does not parse: %w", err)
	}
	switch {
	case len(docs) == 0 && len(bytes.TrimSpace(data)) == 0:
		return &Doc{doc: &yaml.Node{Kind: yaml.DocumentNode,
			Content: []*yaml.Node{{Kind: yaml.MappingNode, Tag: "!!map"}}}}, nil
	case len(docs) == 0:
		return nil, errors.New("the file holds no document (only comments)")
	case len(docs) > 1:
		return nil, fmt.Errorf("the file holds %d documents; only one is edited", len(docs))
	}
	d := docs[0]
	if d.Kind != yaml.DocumentNode || len(d.Content) != 1 || d.Content[0].Kind != yaml.MappingNode {
		return nil, errors.New("the top level is not a mapping")
	}
	return &Doc{doc: d}, nil
}

// NewDoc is a new file whose top level is root (a mapping).
func NewDoc(root *yaml.Node) *Doc {
	return &Doc{doc: &yaml.Node{Kind: yaml.DocumentNode, Content: []*yaml.Node{root}}}
}

// Node is the document node.
func (d *Doc) Node() *yaml.Node { return d.doc }

// Edits is every edit made so far, in order.
func (d *Doc) Edits() []Edit { return append([]Edit(nil), d.edits...) }

// Bytes re-encodes the document (Encode).
func (d *Doc) Bytes() ([]byte, error) { return Encode(d.doc) }

func checkKeys(path []string) error {
	for i, k := range path {
		if k == MergeKey {
			return &PathError{At: path[:i+1], Reason: "the merge key `<<` is not a key an edit sets, removes or descends"}
		}
	}
	return nil
}

// mappingAt walks path from the top level through mappings; create adds
// each missing mapping (recording an ensure edit for it).
func (d *Doc) mappingAt(path []string, create bool) (*yaml.Node, error) {
	cur := d.doc.Content[0]
	for i, k := range path {
		next := MapValue(cur, k)
		if next == nil {
			if !create {
				return nil, &PathError{At: path[:i+1], Reason: "is not in the file"}
			}
			next = &yaml.Node{Kind: yaml.MappingNode, Tag: "!!map"}
			setMapValue(cur, k, next, KeyNode)
			d.edits = append(d.edits, Edit{Path: clonePath(path[:i+1]), Op: OpEnsure})
		}
		if next.Kind != yaml.MappingNode {
			return nil, &PathError{At: path[:i+1], Reason: "is not a mapping (" + kindName(next) + ")"}
		}
		cur = next
	}
	return cur, nil
}

// EnsureMap makes path a mapping: every missing mapping on it is created;
// a node on it that is not a mapping (an alias included) refuses.
func (d *Doc) EnsureMap(path []string) error {
	if err := checkKeys(path); err != nil {
		return err
	}
	_, err := d.mappingAt(path, true)
	if err == nil {
		// Recorded even when it was there: Verify then descends it as
		// unedited, which is what it is.
		d.edits = append(d.edits, Edit{Path: clonePath(path), Op: OpEnsure})
	}
	return err
}

// Set sets the key at path (its last element) to val, in the mapping at the
// rest of path, which must exist (EnsureMap first). An existing key keeps
// its key node and position; a new one is appended.
func (d *Doc) Set(path []string, val *yaml.Node) error {
	if len(path) == 0 {
		return &PathError{Reason: "set needs a key: the top level itself is not set"}
	}
	if err := checkKeys(path); err != nil {
		return err
	}
	parent, err := d.mappingAt(path[:len(path)-1], false)
	if err != nil {
		return err
	}
	if old := MapValue(parent, path[len(path)-1]); old != nil && old.Kind != yaml.AliasNode {
		// The comments yaml.v3 hangs on the value it replaces (a trailing
		// `# …` on the key's line) stay with the key.
		if val.HeadComment == "" && val.LineComment == "" && val.FootComment == "" {
			val.HeadComment, val.LineComment, val.FootComment = old.HeadComment, old.LineComment, old.FootComment
		}
	}
	setMapValue(parent, path[len(path)-1], val, KeyNode)
	d.edits = append(d.edits, Edit{Path: clonePath(path), Op: OpSet})
	return nil
}

// Unset removes the key at path; it reports whether the key was there (a
// key, or a parent, that is not there is a no-op, as RFC 7396 treats
// removing an absent member). A parent that is not a mapping refuses.
func (d *Doc) Unset(path []string) (bool, error) {
	if len(path) == 0 {
		return false, &PathError{Reason: "unset needs a key: the top level itself is not removed"}
	}
	if err := checkKeys(path); err != nil {
		return false, err
	}
	parent, err := d.mappingAt(path[:len(path)-1], false)
	var pe *PathError
	if errors.As(err, &pe) && pe.Reason == "is not in the file" {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	removed := DeleteMapValue(parent, path[len(path)-1])
	if removed {
		d.edits = append(d.edits, Edit{Path: clonePath(path), Op: OpUnset})
	}
	return removed, nil
}

func clonePath(p []string) []string { return append([]string{}, p...) }

func kindName(n *yaml.Node) string {
	switch n.Kind {
	case yaml.ScalarNode:
		return "a scalar"
	case yaml.SequenceNode:
		return "a sequence"
	case yaml.AliasNode:
		return "an alias"
	case yaml.MappingNode:
		return "a mapping"
	}
	return "a node"
}
