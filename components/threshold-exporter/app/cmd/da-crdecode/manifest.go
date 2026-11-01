package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	utiljson "k8s.io/apimachinery/pkg/util/json"
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
//   - Then a document's `apiVersion` and `metadata` must decode into the
//     types kubectl reads them as: cli-runtime's metadata decoder
//     (pkg/resource/metadata_decoder.go) unmarshals every document, a list
//     included, into metadataOnlyObject below (metav1.TypeMeta and
//     metav1.ObjectMeta) with k8s.io/apimachinery/pkg/util/json, and refuses
//     the document when that fails: an `apiVersion` that is not a string, a
//     `metadata` that is not a mapping, a `name`, `namespace`, label or
//     annotation value that is not a string, a `creationTimestamp` that is
//     not RFC 3339 ("json: cannot unmarshal number into Go struct field
//     metadataOnlyObject.TypeMeta.apiVersion of type string", `parsing time
//     "garbage" as …`). Null is accepted wherever it stands, keys are
//     matched with their case (`Metadata:` is no `metadata`), and fields the
//     types do not have are ignored. The check here is the same library
//     call on the same type, so it accepts and refuses what kubectl does,
//     with kubectl's words; when kubectl's error is a value put into a
//     field that holds text (an unquoted `010` or `yes` as a name, a label
//     value; quoteHint), one sentence is added saying that quoting makes it
//     text. It is made on the document's JSON with its keys
//     sorted, as kubectl's conversion gives them, so that of two wrong
//     fields the one kubectl names is the one named here. A list is
//     checked after its items are decoded (an `items` that is no list, an
//     item that is no mapping is refused first, as kubectl refuses it) and
//     before anything is asked of its items, and before its own content
//     (below), which kubectl does not refuse.
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
//     stricter than kubectl. It is checked after every refusal kubectl
//     makes of the list itself (its `items`, an item that is no mapping,
//     its metadata), so that a list kubectl refuses is refused for
//     kubectl's reason.
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
//   - Last, each item as it is given (with the type it took from the list)
//     must pass the same metadataOnlyObject check as a document. kubectl
//     does not make it of an item when it reads the file (an item with
//     `name: 5` or `metadata: 5` is visited), but applying such an item does
//     not apply it as written: it is refused or changed. Refused, for
//     example: a `name` that is not a string by the client ("resource name
//     may not be empty"), most metadata of the wrong type by kubectl's
//     schema validation or by the API server. Changed, for example: an
//     annotation value that is not a string makes client-side apply
//     (`kubectl apply`) report success with every annotation the item
//     wrote dropped, only kubectl's own last-applied annotation left; a
//     `namespace` that is not a string is replaced by the one kubectl runs
//     with (`-n`, or the context's default); server-side apply
//     (`--server-side`) of a custom resource drops a time field written
//     wrong (`creationTimestamp`, …).
//     Refusing every such item here is stricter than kubectl's reading of
//     the file.
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
		if err := metadataTypes(doc); err != nil {
			return nil, nil, documentMetadataError(err)
		}
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
	lines := itemLines(source, len(items))
	// kubectl decodes the whole list before it checks the list's metadata:
	// an item that is no mapping refuses the list first.
	for i, item := range items {
		if _, _, err := itemObject(item); err != nil {
			return nil, nil, inItem(i, lines, err)
		}
	}
	if err := metadataTypes(doc); err != nil {
		return nil, nil, documentMetadataError(err)
	}
	// The list's own content is refused here, not by kubectl: after every
	// refusal kubectl makes of the list itself, so that one is the one told.
	if own := listOwnContent(obj); len(own) > 0 {
		return nil, nil, fmt.Errorf("a list document (it has `items`) with content of its "+
			"own: `%s` — kubectl applies only a list's items and discards the rest without "+
			"a word; move it into an item, or drop `items`", strings.Join(own, "`, `"))
	}
	perItem, err := itemDifferences(differs, len(items))
	if err != nil {
		return nil, nil, err
	}
	listKind, listAPIVersion := stringField(obj, "kind"), stringField(obj, "apiVersion")
	for i, item := range items {
		typed, took, err := listItem(item, listKind, listAPIVersion)
		if err != nil {
			return nil, nil, inItem(i, lines, err)
		}
		if err := metadataTypes(typed); err != nil {
			return nil, nil, inItem(i, lines, itemMetadataError(err))
		}
		items[i] = typed
		if took {
			perItem[i] = outsideTypeFields(perItem[i])
		}
	}
	return items, perItem, nil
}

// inItem names the list item *i* (and the line it starts on, from *lines*,
// when that can be told) in the refusal *err*.
func inItem(i int, lines []int, err error) error {
	where := fmt.Sprintf("items[%d]", i)
	if lines[i] > 0 {
		where += fmt.Sprintf(" (line %d)", lines[i])
	}
	return fmt.Errorf("in this list, %s: %w", where, err)
}

// metadataOnlyObject is the type cli-runtime's metadata decoder
// (pkg/resource/metadata_decoder.go) unmarshals every document into, with
// the same name and tags: its name is in the errors kubectl prints
// ("Go struct field metadataOnlyObject.TypeMeta.apiVersion").
type metadataOnlyObject struct {
	metav1.TypeMeta   `json:""`
	metav1.ObjectMeta `json:"metadata,omitempty"`
}

// metadataTypes checks the object *raw* the way kubectl's metadata decoder
// does: utiljson.Unmarshal into a metadataOnlyObject. The object is first
// written with its keys sorted and its numbers as they stand, as
// sigs.k8s.io/yaml.YAMLToJSON gives kubectl the document, so that of
// several fields of the wrong type the first one kubectl meets is the one
// reported. See the comment at the top of this file.
func metadataTypes(raw json.RawMessage) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v interface{}
	if err := dec.Decode(&v); err != nil {
		return err
	}
	sorted, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return utiljson.Unmarshal(sorted, &metadataOnlyObject{})
}

// documentMetadataError is the refusal of a document whose `apiVersion` or
// `metadata` kubectl's metadata decoder refuses with *err*.
func documentMetadataError(err error) error {
	return fmt.Errorf(notObject+"its `apiVersion` or `metadata` does not decode into the types "+
		"kubectl reads them as, metav1.TypeMeta and metav1.ObjectMeta (kubectl: %v)%s", err, quoteHint(err))
}

// quoteHint is a sentence for a metadata refusal *err* that is a value put
// into a field that holds text, the type error kubectl reports for an
// unquoted `010`, `1.5` or `yes` the YAML reads as a number or a boolean;
// "" for any other refusal. The error is told by its type, not its words:
// a *json.UnmarshalTypeError whose Type is a string kind and whose Field,
// followed through metadataOnlyObject (fieldType), ends at a string kind
// too. The second test is what keeps the sentence off a time: kubectl
// reports `creationTimestamp: 5` with Type string (metav1.Time decodes
// from a string), but quoting gives a string that is still no time.
func quoteHint(err error) string {
	var typeErr *json.UnmarshalTypeError
	if !errors.As(err, &typeErr) || typeErr.Type == nil || typeErr.Type.Kind() != reflect.String {
		return ""
	}
	t, ok := fieldType(reflect.TypeOf(metadataOnlyObject{}), typeErr.Field)
	if !ok || t.Kind() != reflect.String {
		return ""
	}
	return "; a value the YAML reads as a number or a boolean (an unquoted 010, 1.5, yes) " +
		"is not text: quote it if text is meant"
}

// fieldType is the type of the value at *field* — an UnmarshalTypeError's
// Field, dot-separated JSON names, with an embedded struct named by its
// type (`TypeMeta.apiVersion`, `metadata.labels`,
// `metadata.ownerReferences.uid`) — starting at the struct type *t*:
// pointers, slices and maps are followed to their elements, at every step
// and at the end (a label's value, not the labels map). false when a step
// names no field.
func fieldType(t reflect.Type, field string) (reflect.Type, bool) {
	elem := func(t reflect.Type) reflect.Type {
		for t.Kind() == reflect.Pointer || t.Kind() == reflect.Slice ||
			t.Kind() == reflect.Array || t.Kind() == reflect.Map {
			t = t.Elem()
		}
		return t
	}
	for _, name := range strings.Split(field, ".") {
		t = elem(t)
		if t.Kind() != reflect.Struct {
			return nil, false
		}
		found := false
		for i := 0; i < t.NumField(); i++ {
			f := t.Field(i)
			jsonName, _, _ := strings.Cut(f.Tag.Get("json"), ",")
			if jsonName == "-" {
				continue
			}
			if jsonName == "" {
				jsonName = f.Name
			}
			if jsonName == name || (f.Anonymous && f.Name == name) {
				t, found = f.Type, true
				break
			}
		}
		if !found {
			return nil, false
		}
	}
	return elem(t), true
}

// itemMetadataError is the refusal of a list item whose `apiVersion` or
// `metadata` does not decode as a document's must (*err*), which kubectl
// does not check of an item when it reads the file.
func itemMetadataError(err error) error {
	return fmt.Errorf(notObject+"its `apiVersion` or `metadata` does not decode into the types "+
		"kubectl reads a document's as, metav1.TypeMeta and metav1.ObjectMeta (%v); kubectl does "+
		"not check this of an item when it reads the file, but applying the item would not apply "+
		"it as written: it is refused by kubectl or the API server (a `name` that is not a string, "+
		"most metadata of the wrong type) or changed (the item's own annotations dropped by "+
		"client-side apply, the namespace replaced by the one kubectl uses, a time field dropped "+
		"by server-side apply of a custom resource)%s",
		err, quoteHint(err))
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
	item, obj, err := itemObject(item)
	if err != nil {
		return nil, false, err
	}
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

// itemObject is the list item *item* as kubectl decodes it (a null item is
// `{}`) and its top-level fields; an error for an item that is no mapping.
func itemObject(item json.RawMessage) (json.RawMessage, map[string]json.RawMessage, error) {
	shape := jsonShape(item)
	if shape == "null" {
		item = json.RawMessage("{}") // kubectl's reading of a null item
	}
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(item, &obj); err != nil || obj == nil {
		return nil, nil, fmt.Errorf(notObject+"the item is %s, not a mapping (kubectl: cannot "+
			"unmarshal %s into Go value of type map[string]interface {}, and reads none of this "+
			"list's items)", withArticle(shape), shape)
	}
	return item, obj, nil
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
