package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"

	"gopkg.in/yaml.v3"
)

// The manifest reading: what `kubectl apply -f` makes of one converted
// document (#3426). Measured against k8s.io/apimachinery v0.37.0
// (unstructured.UnstructuredJSONScheme) and k8s.io/cli-runtime v0.37.0 (the
// local builder, unstructured, flattened — what `kubectl apply -f` visits):
//
//   - A document must decode into one object: a mapping ("cannot unmarshal
//     array into Go value of type unstructured.detector" otherwise) whose
//     `kind` is a non-empty string ("Object 'Kind' is missing" for a missing,
//     non-string, null or empty kind). The kind is checked first: `{items: []}`
//     is refused for its kind.
//   - A document with a top-level `items` key is a list, whatever its kind
//     (`List`, `ConfigMapList`, even `ConfigMap`), and `items: null` too.
//     kubectl applies its items, in order, and nothing of the list itself;
//     an empty or null `items` applies nothing. `items` that is not a list is
//     refused ("cannot unmarshal … into Go struct field decodeList.items").
//   - Each item must be an object with a non-empty string `kind` AND a
//     non-empty string `apiVersion` (kubectl asks every item for its type:
//     "Object 'apiVersion' is missing", which it does not ask of a document).
//   - An item that is itself a list — any item whose `items` is a JSON array,
//     an empty one and a nested `kind: List` included — is refused by kubectl
//     ("is not a list: no Items field in this object"), and is refused here.
//     An item whose `items` is null, a string, a number or an object is an
//     ordinary object to kubectl (measured: it is applied), and here.
//
// The as-written comparison (aswritten.go) is made on the whole list
// document; each of its differences is given to the item its path starts
// in (`items`, i), with that prefix taken off. A difference anywhere else —
// in the list's own keys, or one at the list or at `items` that stops the
// comparison of everything below it — cannot be given to an item, so the
// document is refused rather than have it lost.

// notObject prefixes every refusal of a document or item that is no
// Kubernetes object.
const notObject = "it is not a Kubernetes object: "

// objectKind checks that *raw* is an object with a non-empty string `kind`
// and returns its top-level fields.
func objectKind(raw json.RawMessage) (map[string]json.RawMessage, error) {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(raw, &obj); err != nil || obj == nil {
		return nil, fmt.Errorf(notObject+"the document is %s, not a mapping "+
			"(kubectl: cannot unmarshal %s into Go value of type unstructured.detector)",
			jsonShape(raw), jsonShape(raw))
	}
	if err := nonEmptyString(obj, "kind", "Object 'Kind' is missing"); err != nil {
		return nil, err
	}
	return obj, nil
}

// nonEmptyString refuses *obj* unless its *key* is a non-empty string.
func nonEmptyString(obj map[string]json.RawMessage, key, kubectl string) error {
	raw, ok := obj[key]
	var s string
	switch {
	case !ok:
		return fmt.Errorf(notObject+"it has no `%s` (kubectl: %s)", key, kubectl)
	case bytes.Equal(bytes.TrimSpace(raw), []byte("null")) || json.Unmarshal(raw, &s) != nil:
		return fmt.Errorf(notObject+"its `%s` is %s, not a string (kubectl: %s)", key, jsonShape(raw), kubectl)
	case s == "":
		return fmt.Errorf(notObject+"its `%s` is empty (kubectl: %s)", key, kubectl)
	}
	return nil
}

// manifestDocuments is what kubectl applies of the converted document *doc*
// (its source *source*, at the file's line numbers, and its differences
// *differs*): the document itself, or a list's items. See the comment at
// the top of this file.
func manifestDocuments(source []byte, doc json.RawMessage, differs []difference) (
	[]json.RawMessage, [][]difference, error) {
	obj, err := objectKind(doc)
	if err != nil {
		return nil, nil, err
	}
	rawItems, isList := obj["items"]
	if !isList {
		return []json.RawMessage{doc}, [][]difference{differs}, nil
	}
	// `items: null` decodes to no items (json.Unmarshal leaves the slice
	// nil) and goes the way of `items: []`: the list's own differences are
	// still checked below, so one is never lost because there is no item.
	var items []json.RawMessage
	if err := json.Unmarshal(rawItems, &items); err != nil {
		return nil, nil, fmt.Errorf("a list document's `items` is %s, not a list "+
			"(kubectl: cannot unmarshal %s into Go struct field decodeList.items)",
			jsonShape(rawItems), jsonShape(rawItems))
	}
	perItem, err := itemDifferences(differs, len(items))
	if err != nil {
		return nil, nil, err
	}
	lines := itemLines(source, len(items))
	for i, item := range items {
		if err := listItem(item); err != nil {
			where := fmt.Sprintf("items[%d]", i)
			if lines[i] > 0 {
				where += fmt.Sprintf(" (line %d)", lines[i])
			}
			return nil, nil, fmt.Errorf("in this list, %s: %w", where, err)
		}
	}
	return items, perItem, nil
}

// listItem refuses an item of a list that kubectl would not apply.
func listItem(item json.RawMessage) error {
	obj, err := objectKind(item)
	if err != nil {
		return err
	}
	if err := nonEmptyString(obj, "apiVersion", "Object 'apiVersion' is missing"); err != nil {
		return err
	}
	if inner, ok := obj["items"]; ok && jsonShape(inner) == "array" {
		return errors.New("a list inside a list is not read " +
			"(kubectl: is not a list: no Items field in this object); write its items as items of the outer list")
	}
	return nil
}

// itemDifferences gives each difference of a list document to the item its
// path starts in, with the (`items`, i) prefix taken off. A difference
// outside every item is an error: it would be lost.
func itemDifferences(differs []difference, n int) ([][]difference, error) {
	out := make([][]difference, n)
	for i := range out {
		out[i] = []difference{}
	}
	for _, d := range differs {
		path, _ := d["path"].([]interface{})
		i, ok := -1, false
		if len(path) >= 2 && path[0] == "items" {
			i, ok = path[1].(int)
		}
		if !ok || i < 0 || i >= n {
			return nil, fmt.Errorf("a list document whose own text is not read as written "+
				"(at %s) cannot have its items compared with their text; write its items "+
				"as separate documents", pathText(path))
		}
		item := difference{}
		for k, v := range d {
			item[k] = v
		}
		item["path"] = append([]interface{}{}, path[2:]...)
		out[i] = append(out[i], item)
	}
	return out, nil
}

// pathText writes a difference path for a message: `.` for the document.
func pathText(path []interface{}) string {
	if len(path) == 0 {
		return "."
	}
	var b bytes.Buffer
	for _, seg := range path {
		if i, ok := seg.(int); ok {
			fmt.Fprintf(&b, "[%d]", i)
		} else {
			fmt.Fprintf(&b, ".%v", seg)
		}
	}
	return b.String()
}

// itemLines is the line each of a list document's *n* items starts on in
// *source*, 0 where it cannot be told (the items come in through an alias
// or a merge, say); the refusal then names the document only.
func itemLines(source []byte, n int) []int {
	lines := make([]int, n)
	var root yaml.Node
	if err := yaml.Unmarshal(source, &root); err != nil || root.Kind != yaml.DocumentNode ||
		len(root.Content) != 1 {
		return lines
	}
	top := root.Content[0]
	if top.Kind != yaml.MappingNode {
		return lines
	}
	for k := 0; k+1 < len(top.Content); k += 2 {
		if key := top.Content[k]; key.Kind == yaml.ScalarNode && key.Value == "items" {
			seq := top.Content[k+1]
			if seq.Kind != yaml.SequenceNode || len(seq.Content) != n {
				return lines
			}
			for i, item := range seq.Content {
				lines[i] = item.Line
			}
		}
	}
	return lines
}
