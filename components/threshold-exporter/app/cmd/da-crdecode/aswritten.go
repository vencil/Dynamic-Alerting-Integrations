package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"

	"gopkg.in/yaml.v3"
)

// The file as written, set against the conversion's reading of it (ADR-036
// §7 step 11, #3206).
//
// YAMLToJSON reads `010:` as the key "8" and `yes:` as "true": a consumer that
// takes a key or a string from the converted JSON as a NAME (a tenant id, an
// object name) gets another name than the author wrote. Whether that happened
// is a question about the source text, so it is answered here, in Go, beside
// the conversion — the caller does not read the YAML a second time.
//
// The source is composed with yaml.v3 into nodes (the node tree, not a decode:
// no value is typed, every scalar keeps its text). `<<` merges are applied to
// those nodes the way go-yaml v2 applies them when YAMLToJSON decodes the file
// (decoder.mapping / decoder.merge in go.yaml.in/yaml/v2 v2.4.4): in written
// order, a later key replaces an earlier one — a `<<` written after an explicit
// key replaces it too — and in a merged sequence an earlier mapping takes
// precedence. TestMergeOrderIsTheConversions measures that against YAMLToJSON.
// Keys are compared by their text, so `8:` and `010:` are two keys as written
// and one (the int 8, "8") as read.
//
// The two readings are walked side by side. What is reported (`differs_from_
// text`, one list per document) is where they part:
//
//	{"kind": "keys", "path": P, "written_keys": [...], "read_keys": [...]}
//	    the mapping at P: the keys as written that are not keys of the
//	    reading, and the keys of the reading that are not written so (both
//	    sorted); nothing below P is compared
//	{"kind": "string", "path": P, "text": T}
//	    the scalar at P is read as a string that is not its text T
//	    (`!!binary b2s=` is "ok")
//	{"kind": "structure", "path": P, "written": W, "read": R}
//	    the node at P is not of the shape the reading has there (W and R:
//	    "a mapping", "a list of N", "a scalar"); nothing below P is compared
//
// "Nothing below P is compared": with `8: a` and `010: b` in one mapping, the
// read key "8" holds b, and which written key it came from cannot be told from
// the text — pairing the values by text would report a difference that is not
// there, or miss one. So a path below a "keys" or "structure" entry has no
// verdict of its own; a caller that needs one treats it as not as written.
//
// A scalar read as a number, a boolean or null is not reported: the reading
// itself says so. P is a list of object keys (strings, as read) and list
// indexes (integers); [] is the document itself.
//
// The same walk writes each document with every object's keys in the order
// written (YAMLToJSON sorts them): a key matched to a written one by its text
// takes that key's place; the rest follow, sorted.

// errNotAsWritten wraps the reason the source could not be composed or paired.
var errNotAsWritten = errors.New("the conversion reads the file, but it cannot be read as written to compare with that reading")

// difference is one entry of differs_from_text.
type difference map[string]interface{}

// writtenMap is a mapping node's entries after `<<` merges, keyed by key text.
type writtenMap struct {
	order []string
	value map[string]*yaml.Node
}

type asWritten struct {
	merged  map[*yaml.Node]*writtenMap
	inMerge map[*yaml.Node]bool
	out     bytes.Buffer
	differs []difference
}

// compareAsWritten returns the converted document *doc* with its objects'
// keys in the order *data* writes them, and where the two readings differ.
func compareAsWritten(data []byte, doc json.RawMessage) (json.RawMessage, []difference, error) {
	var root yaml.Node
	dec := yaml.NewDecoder(bytes.NewReader(data))
	if err := dec.Decode(&root); err != nil && !errors.Is(err, io.EOF) {
		return nil, nil, fmt.Errorf("%w: %v", errNotAsWritten, err)
	}
	var top *yaml.Node
	if root.Kind == yaml.DocumentNode && len(root.Content) == 1 {
		top = root.Content[0]
	}
	jd := json.NewDecoder(bytes.NewReader(doc))
	jd.UseNumber()
	var read interface{}
	if err := jd.Decode(&read); err != nil {
		return nil, nil, err
	}
	w := &asWritten{merged: map[*yaml.Node]*writtenMap{}, inMerge: map[*yaml.Node]bool{}}
	if err := w.walk(top, read, []interface{}{}); err != nil {
		return nil, nil, err
	}
	if w.differs == nil {
		w.differs = []difference{}
	}
	return json.RawMessage(w.out.Bytes()), w.differs, nil
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

// entries is *m*'s keys and values as go-yaml v2 builds the map from it.
func (w *asWritten) entries(m *yaml.Node) (*writtenMap, error) {
	if got, ok := w.merged[m]; ok {
		return got, nil
	}
	if w.inMerge[m] {
		return nil, fmt.Errorf("%w: a mapping merges itself (line %d)", errNotAsWritten, m.Line)
	}
	w.inMerge[m] = true
	defer delete(w.inMerge, m)
	wm := &writtenMap{value: map[string]*yaml.Node{}}
	set := func(text string, v *yaml.Node) {
		if _, seen := wm.value[text]; !seen {
			wm.order = append(wm.order, text)
		}
		wm.value[text] = v
	}
	apply := func(src *yaml.Node, setOrder, setValue bool) error {
		sub, err := w.entries(src)
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
						return nil, fmt.Errorf("%w: a << merge of a non-mapping (line %d)", errNotAsWritten, it.Line)
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
				return nil, fmt.Errorf("%w: a << merge of a non-mapping (line %d)", errNotAsWritten, v.Line)
			}
			continue
		}
		kd := deref(k)
		if kd == nil || kd.Kind != yaml.ScalarNode {
			return nil, fmt.Errorf("%w: a key that is not a scalar (line %d)", errNotAsWritten, k.Line)
		}
		set(kd.Value, v)
	}
	w.merged[m] = wm
	return wm, nil
}

func shapeOfRead(v interface{}) string {
	switch t := v.(type) {
	case map[string]interface{}:
		return "a mapping"
	case []interface{}:
		return fmt.Sprintf("a list of %d", len(t))
	}
	return "a scalar"
}

func shapeOfNode(n *yaml.Node) string {
	switch {
	case n == nil:
		return "nothing"
	case n.Kind == yaml.MappingNode:
		return "a mapping"
	case n.Kind == yaml.SequenceNode:
		return fmt.Sprintf("a list of %d", len(n.Content))
	}
	return "a scalar"
}

func extend(path []interface{}, seg interface{}) []interface{} {
	out := make([]interface{}, len(path), len(path)+1)
	copy(out, path)
	return append(out, seg)
}

// plain writes *v* as encoding/json does: objects' keys sorted.
func (w *asWritten) plain(v interface{}) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	w.out.Write(b)
	return nil
}

func (w *asWritten) structure(path []interface{}, n *yaml.Node, v interface{}) error {
	w.differs = append(w.differs, difference{"kind": "structure", "path": path,
		"written": shapeOfNode(n), "read": shapeOfRead(v)})
	return w.plain(v)
}

func (w *asWritten) walk(node *yaml.Node, read interface{}, path []interface{}) error {
	n := deref(node)
	switch v := read.(type) {
	case map[string]interface{}:
		if n == nil || n.Kind != yaml.MappingNode {
			return w.structure(path, n, v)
		}
		wm, err := w.entries(n)
		if err != nil {
			return err
		}
		keys := make([]string, 0, len(v))
		matched := map[string]bool{}
		writtenOnly := []string{}
		for _, t := range wm.order {
			if _, ok := v[t]; ok {
				keys = append(keys, t)
				matched[t] = true
			} else {
				writtenOnly = append(writtenOnly, t)
			}
		}
		readOnly := []string{}
		for k := range v {
			if !matched[k] {
				readOnly = append(readOnly, k)
			}
		}
		sort.Strings(readOnly)
		sort.Strings(writtenOnly)
		differ := len(writtenOnly) > 0 || len(readOnly) > 0
		if differ {
			w.differs = append(w.differs, difference{"kind": "keys", "path": path,
				"written_keys": writtenOnly, "read_keys": readOnly})
		}
		keys = append(keys, readOnly...)
		w.out.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				w.out.WriteByte(',')
			}
			kb, err := json.Marshal(k)
			if err != nil {
				return err
			}
			w.out.Write(kb)
			w.out.WriteByte(':')
			if matched[k] && !differ {
				err = w.walk(wm.value[k], v[k], extend(path, k))
			} else {
				err = w.plain(v[k])
			}
			if err != nil {
				return err
			}
		}
		w.out.WriteByte('}')
		return nil
	case []interface{}:
		if n == nil || n.Kind != yaml.SequenceNode || len(n.Content) != len(v) {
			return w.structure(path, n, v)
		}
		w.out.WriteByte('[')
		for i, item := range v {
			if i > 0 {
				w.out.WriteByte(',')
			}
			if err := w.walk(n.Content[i], item, extend(path, i)); err != nil {
				return err
			}
		}
		w.out.WriteByte(']')
		return nil
	}
	if n == nil {
		// An empty file: no node, and the reading is null.
		if read == nil {
			return w.plain(read)
		}
		return w.structure(path, n, read)
	}
	if n.Kind != yaml.ScalarNode {
		return w.structure(path, n, read)
	}
	if s, ok := read.(string); ok && s != n.Value {
		w.differs = append(w.differs, difference{"kind": "string", "path": path, "text": n.Value})
	}
	return w.plain(read)
}
