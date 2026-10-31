package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"

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
//   - A list's own content — any top-level key besides `apiVersion`, `kind`,
//     `metadata` and `items` (a ConfigMap's `data`, a PrometheusRule's
//     `spec`) — is discarded by kubectl without a word. That is no reading
//     of the file a caller can stand on (rules written there never deploy),
//     so here the document is refused, naming the keys (#3438), which is
//     stricter than kubectl.
//   - An item that is itself a list — any item whose `items` is a JSON array,
//     an empty one and a nested `kind: List` included — is refused by kubectl
//     ("is not a list: no Items field in this object"), and is refused here.
//     This is checked before the item's type: an item with `items: [...]` and
//     no `kind` is refused as a list inside a list. An item whose `items` is
//     null, a string, a number or an object is an ordinary object to kubectl
//     (measured: it is applied), and here.
//   - An item takes its type from the list when it has neither: when its
//     `kind` and its `apiVersion` both read as "" — missing, null, not a
//     string, or empty, as unstructured's GetKind / GetAPIVersion read them —
//     both are set (replacing what was there) to the list's `kind` with one
//     trailing "List" taken off and the list's `apiVersion` ("" when that is
//     not a string). apimachinery's decodeToList does this
//     (unstructured/helpers.go). An item that lacks only one of the two takes
//     nothing. So an item of a `ConfigMapList` with neither is a `v1`
//     `ConfigMap`, of a `kind: ConfigMap` list one too, of a
//     `ConfigMapListList` a `ConfigMapList`; of a `kind: List` it takes kind
//     "" and is refused for it, and of a list with no `apiVersion` it is
//     refused for its apiVersion.
//   - Then each item must have a non-empty string `kind` AND a non-empty
//     string `apiVersion` (kubectl asks every item for its type: "Object
//     'apiVersion' is missing", which it does not ask of a document). The
//     item given to the caller is the item as kubectl applies it: with the
//     type it took from the list. Keys it did not have follow its own keys
//     (`apiVersion` before `kind`), as a key the file does not write follows
//     the written ones (aswritten.go); its differences (aswritten.go) under a
//     `kind` or `apiVersion` it replaced are dropped, since that value is no
//     longer in it. The comparison is made on the file's text, so the type
//     the item took adds no difference.
//   - kubectl goes on past an item (or a document) refused for its type and
//     applies the others before it reports the error; an item that is no
//     mapping (a number, a string, a bool, an array: "cannot unmarshal …
//     into Go value of type map[string]interface {}") makes it drop the
//     whole list (none of its items), the file's other documents still
//     applied. Here the first refused one stops the whole file, which is
//     stricter.
//   - A null item is an empty object to kubectl (decodeToList unmarshals it
//     into an empty map), and is read here as `{}`: it takes the list's type
//     like any item with neither, and is given as `{}` with that type.
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

// object checks that *raw* is an object and returns its top-level fields.
func object(raw json.RawMessage) (map[string]json.RawMessage, error) {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(raw, &obj); err != nil || obj == nil {
		return nil, fmt.Errorf(notObject+"the document is %s, not a mapping "+
			"(kubectl: cannot unmarshal %s into Go value of type unstructured.detector)",
			jsonShape(raw), jsonShape(raw))
	}
	return obj, nil
}

// objectKind checks that *raw* is an object with a non-empty string `kind`
// and returns its top-level fields.
func objectKind(raw json.RawMessage) (map[string]json.RawMessage, error) {
	obj, err := object(raw)
	if err != nil {
		return nil, err
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
	if own := listOwnContent(obj); len(own) > 0 {
		return nil, nil, fmt.Errorf("a list document (it has `items`) with content of its "+
			"own: `%s` — kubectl applies only a list's items and discards the rest without "+
			"a word; move it into an item, or drop `items`", strings.Join(own, "`, `"))
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
	listKind, listAPIVersion := stringField(obj, "kind"), stringField(obj, "apiVersion")
	for i, item := range items {
		typed, took, err := listItem(item, listKind, listAPIVersion)
		if err != nil {
			where := fmt.Sprintf("items[%d]", i)
			if lines[i] > 0 {
				where += fmt.Sprintf(" (line %d)", lines[i])
			}
			return nil, nil, fmt.Errorf("in this list, %s: %w", where, err)
		}
		items[i] = typed
		if took {
			perItem[i] = outsideTypeFields(perItem[i])
		}
	}
	return items, perItem, nil
}

// listOwnKeys are the keys a list document carries that are no content:
// what `kubectl get … -o yaml` prints beside `items`.
var listOwnKeys = map[string]bool{"apiVersion": true, "kind": true, "metadata": true, "items": true}

// listOwnContent is the sorted top-level keys of the list document *obj*
// that kubectl discards (anything but listOwnKeys).
func listOwnContent(obj map[string]json.RawMessage) []string {
	var own []string
	for k := range obj {
		if !listOwnKeys[k] {
			own = append(own, k)
		}
	}
	sort.Strings(own)
	return own
}

// stringField is *obj*'s *key* as unstructured's GetKind / GetAPIVersion
// read it (getNestedString): the string, or "" when it is missing, null or
// not a string.
func stringField(obj map[string]json.RawMessage, key string) string {
	var s string
	if raw, ok := obj[key]; ok && json.Unmarshal(raw, &s) == nil {
		return s
	}
	return ""
}

// listItem is the item *item* of a list whose `kind` and `apiVersion` read
// *listKind* and *listAPIVersion*, as kubectl applies it, and whether it took
// its type from the list; an error for an item kubectl would not apply. See
// the comment at the top of this file.
func listItem(item json.RawMessage, listKind, listAPIVersion string) (json.RawMessage, bool, error) {
	shape := jsonShape(item)
	if shape == "null" {
		item = json.RawMessage("{}") // kubectl's reading of a null item
	}
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(item, &obj); err != nil || obj == nil {
		return nil, false, fmt.Errorf(notObject+"the item is %s, not a mapping (kubectl: cannot "+
			"unmarshal %s into Go value of type map[string]interface {}, and reads none of this "+
			"list's items)", withArticle(shape), shape)
	}
	var err error
	if inner, ok := obj["items"]; ok && jsonShape(inner) == "array" {
		return nil, false, errors.New("a list inside a list is not read " +
			"(kubectl: is not a list: no Items field in this object); write its items as items of the outer list")
	}
	if stringField(obj, "kind") != "" || stringField(obj, "apiVersion") != "" {
		return item, false, typeFields(obj)
	}
	kind, apiVersion := strings.TrimSuffix(listKind, "List"), listAPIVersion
	if item, err = withType(item, kind, apiVersion); err != nil {
		return nil, false, err
	}
	if obj, err = object(item); err != nil {
		return nil, false, err
	}
	if err := typeFields(obj); err != nil {
		return nil, false, fmt.Errorf("%w — an item with neither a string `kind` nor a string "+
			"`apiVersion` takes the list's: here kind %q (the list's, one trailing \"List\" "+
			"taken off) and apiVersion %q", err, kind, apiVersion)
	}
	return item, true, nil
}

// withArticle is the JSON type name *shape* with its indefinite article.
func withArticle(shape string) string {
	if shape == "array" || shape == "object" {
		return "an " + shape
	}
	return "a " + shape
}

// typeFields refuses an item unless its `kind` and its `apiVersion` are
// non-empty strings, `kind` first, as kubectl asks.
func typeFields(obj map[string]json.RawMessage) error {
	if err := nonEmptyString(obj, "kind", "Object 'Kind' is missing"); err != nil {
		return err
	}
	return nonEmptyString(obj, "apiVersion", "Object 'apiVersion' is missing")
}

// withType is the object *raw* with its `kind` and `apiVersion` set to *kind*
// and *apiVersion*: a key it has keeps its place, a key it has not follows
// its own keys, `apiVersion` first.
func withType(raw json.RawMessage, kind, apiVersion string) (json.RawMessage, error) {
	set := map[string]string{"apiVersion": apiVersion, "kind": kind}
	dec := json.NewDecoder(bytes.NewReader(raw))
	if _, err := dec.Token(); err != nil { // the object's `{`
		return nil, err
	}
	var out bytes.Buffer
	out.WriteByte('{')
	write := func(key string, value []byte) error {
		if out.Len() > 1 {
			out.WriteByte(',')
		}
		kb, err := json.Marshal(key)
		if err != nil {
			return err
		}
		out.Write(kb)
		out.WriteByte(':')
		out.Write(value)
		return nil
	}
	done := map[string]bool{}
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return nil, err
		}
		key, _ := tok.(string)
		var value json.RawMessage
		if err := dec.Decode(&value); err != nil {
			return nil, err
		}
		if v, ok := set[key]; ok {
			if value, err = json.Marshal(v); err != nil {
				return nil, err
			}
			done[key] = true
		}
		if err := write(key, value); err != nil {
			return nil, err
		}
	}
	for _, key := range []string{"apiVersion", "kind"} {
		if done[key] {
			continue
		}
		value, err := json.Marshal(set[key])
		if err != nil {
			return nil, err
		}
		if err := write(key, value); err != nil {
			return nil, err
		}
	}
	out.WriteByte('}')
	return json.RawMessage(out.Bytes()), nil
}

// outsideTypeFields is *differs* without the differences under an item's
// `kind` or `apiVersion`, whose values the item no longer holds once it took
// its type from the list.
func outsideTypeFields(differs []difference) []difference {
	out := []difference{}
	for _, d := range differs {
		path, _ := d["path"].([]interface{})
		if len(path) > 0 && (path[0] == "kind" || path[0] == "apiVersion") {
			continue
		}
		out = append(out, d)
	}
	return out
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
