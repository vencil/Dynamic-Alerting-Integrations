package main

import (
	"strings"
	"testing"
)

// #3472: an item of a list takes its type from the list when it has neither
// a string `kind` nor a string `apiVersion` (apimachinery's decodeToList,
// unstructured/helpers.go), and the item given to the caller is the one
// kubectl applies, with that type.
//
// Every row's `kubectl` is what k8s.io/cli-runtime v0.37.0 made of the same
// input, measured: resource.NewLocalBuilder().Unstructured().Local()
// .ContinueOnError().Flatten() — what `kubectl apply -f` visits — printing
// each item it accepted (apiVersion/kind) or the error it stopped at. An
// accepted item is one entry of `documents` here, with the same apiVersion
// and kind; a refused one is a refusal naming the item kubectl refused.
func TestListItemsTakeTheListsType(t *testing.T) {
	t.Parallel()
	for _, tc := range listItemCases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if (tc.want == "") == (tc.wantErr == "") {
				t.Fatalf("row %q: set exactly one of want and wantErr", tc.name)
			}
			rc, out, errOut := runManifest(t, tc.in)
			if tc.want != "" {
				if rc != exitOK || out != tc.want+"\n" || errOut != "" {
					t.Fatalf("kubectl: %s\nrc=%d stdout=%q stderr=%q; want rc 0 stdout %q",
						tc.kubectl, rc, out, errOut, tc.want)
				}
				return
			}
			if rc != exitCallerErr || out != "" || !strings.Contains(errOut, tc.wantErr) {
				t.Fatalf("kubectl: %s\nrc=%d stdout=%q stderr=%q; want rc 2, stderr with %q",
					tc.kubectl, rc, out, errOut, tc.wantErr)
			}
		})
	}
}

// nullOrEmptyOfList is the whole refusal of a `null` or `{}` item of a
// `kind: List` (after the file name): the same for both.
const nullOrEmptyOfList = "in this list, items[0] (line 4): it is not a Kubernetes object: " +
	"its `kind` is empty (kubectl: Object 'Kind' is missing) — an item with neither a string `kind` nor a " +
	"string `apiVersion` takes the list's: here kind \"\" (the list's, one trailing \"List\" taken off) " +
	"and apiVersion \"v1\"\n"

const (
	cmListHead = "apiVersion: v1\nkind: ConfigMapList\nitems:\n"
	listHead   = "apiVersion: v1\nkind: List\nitems:\n"
)

// listItemCases are the rows of TestListItemsTakeTheListsType.
var listItemCases = []struct {
	name, in string
	kubectl  string
	// want is stdout (without `reading`) on a success; wantErr a part of
	// stderr on a refusal. Exactly one is set.
	want, wantErr string
}{
	// Neither → both taken from the list, set after the item's own keys.
	{name: "both-missing", in: cmListHead + "- metadata: {name: a}\n  data: {k: v}\n",
		kubectl: "ACCEPT v1/ConfigMap",
		want: `{"documents":[{"metadata":{"name":"a"},"data":{"k":"v"},"apiVersion":"v1","kind":"ConfigMap"}],` +
			`"differs_from_text":[[]]}`},
	// One of the two → nothing taken; refused for the other.
	{name: "only-kind-missing", in: cmListHead + "- apiVersion: v1\n  metadata: {name: a}\n",
		kubectl: "Object 'Kind' is missing",
		wantErr: "items[0] (line 4): it is not a Kubernetes object: it has no `kind` (kubectl: Object 'Kind' is missing)"},
	{name: "only-apiversion-missing", in: cmListHead + "- kind: ConfigMap\n  metadata: {name: a}\n",
		kubectl: "Object 'apiVersion' is missing",
		wantErr: "items[0] (line 4): it is not a Kubernetes object: it has no `apiVersion` (kubectl: Object 'apiVersion' is missing)"},
	{name: "kind-empty-apiversion-set", in: cmListHead + "- kind: \"\"\n  apiVersion: v1\n  metadata: {name: a}\n",
		kubectl: "Object 'Kind' is missing",
		wantErr: "items[0] (line 4): it is not a Kubernetes object: its `kind` is empty"},
	{name: "apiversion-empty-kind-set", in: cmListHead + "- apiVersion: \"\"\n  kind: Secret\n  metadata: {name: a}\n",
		kubectl: "Object 'apiVersion' is missing",
		wantErr: "items[0] (line 4): it is not a Kubernetes object: its `apiVersion` is empty"},
	// A full item keeps its own type, whatever the list's.
	{name: "own-type-and-taken", in: cmListHead + "- apiVersion: v1\n  kind: Secret\n  metadata: {name: s}\n" +
		"- metadata: {name: inherited}\n",
		kubectl: "ACCEPT v1/Secret, ACCEPT v1/ConfigMap",
		want: `{"documents":[{"apiVersion":"v1","kind":"Secret","metadata":{"name":"s"}},` +
			`{"metadata":{"name":"inherited"},"apiVersion":"v1","kind":"ConfigMap"}],"differs_from_text":[[],[]]}`},
	// Not a string, null or empty is "" to GetKind / GetAPIVersion: taken,
	// the value replaced where it stands.
	{name: "kind-number", in: cmListHead + "- kind: 5\n  metadata: {name: a}\n",
		kubectl: "ACCEPT v1/ConfigMap",
		want:    `{"documents":[{"kind":"ConfigMap","metadata":{"name":"a"},"apiVersion":"v1"}],"differs_from_text":[[]]}`},
	{name: "both-null", in: cmListHead + "- kind: null\n  apiVersion: null\n  metadata: {name: a}\n",
		kubectl: "ACCEPT v1/ConfigMap",
		want:    `{"documents":[{"kind":"ConfigMap","apiVersion":"v1","metadata":{"name":"a"}}],"differs_from_text":[[]]}`},
	{name: "both-empty", in: cmListHead + "- kind: \"\"\n  apiVersion: \"\"\n  metadata: {name: a}\n",
		kubectl: "ACCEPT v1/ConfigMap",
		want:    `{"documents":[{"kind":"ConfigMap","apiVersion":"v1","metadata":{"name":"a"}}],"differs_from_text":[[]]}`},
	{name: "apiversion-number", in: cmListHead + "- apiVersion: 5\n  metadata: {name: a}\n",
		kubectl: "ACCEPT v1/ConfigMap",
		want:    `{"documents":[{"apiVersion":"v1","metadata":{"name":"a"},"kind":"ConfigMap"}],"differs_from_text":[[]]}`},
	// A replaced `kind` takes its differences with it: the keys difference
	// at `kind` (`010` read as "8") is not in the output.
	{name: "kind-mapping-differences-dropped", in: cmListHead + "- kind: {010: x}\n  metadata: {labels: {010: z}}\n",
		kubectl: "ACCEPT v1/ConfigMap",
		want: `{"documents":[{"kind":"ConfigMap","metadata":{"labels":{"8":"z"}},"apiVersion":"v1"}],"differs_from_text":[[` +
			`{"kind":"keys","path":["metadata","labels"],"read_keys":["8"],"written_keys":["010"]}]]}`},
	// `kind: List` without "List" is "": the item must carry its own type.
	{name: "list-both-missing", in: listHead + "- metadata: {name: a}\n",
		kubectl: "Object 'Kind' is missing",
		wantErr: "items[0] (line 4): it is not a Kubernetes object: its `kind` is empty (kubectl: Object 'Kind' is missing)" +
			" — an item with neither a string `kind` nor a string `apiVersion` takes the list's: here kind \"\""},
	{name: "list-only-kind-missing", in: listHead + "- apiVersion: v1\n  metadata: {name: a}\n",
		kubectl: "Object 'Kind' is missing",
		wantErr: "items[0] (line 4): it is not a Kubernetes object: it has no `kind`"},
	{name: "list-only-apiversion-missing", in: listHead + "- kind: ConfigMap\n  metadata: {name: a}\n",
		kubectl: "Object 'apiVersion' is missing",
		wantErr: "items[0] (line 4): it is not a Kubernetes object: it has no `apiVersion`"},
	{name: "list-full-item", in: listHead + "- apiVersion: v1\n  kind: ConfigMap\n  metadata: {name: a}\n",
		kubectl: "ACCEPT v1/ConfigMap",
		want:    `{"documents":[{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"a"}}],"differs_from_text":[[]]}`},
	// A list with no `apiVersion` gives "": refused for it.
	{name: "list-no-apiversion-both-missing", in: "kind: ConfigMapList\nitems:\n- metadata: {name: a}\n",
		kubectl: "Object 'apiVersion' is missing",
		wantErr: "items[0] (line 3): it is not a Kubernetes object: its `apiVersion` is empty (kubectl: Object 'apiVersion' is missing)"},
	{name: "list-no-apiversion-full-item", in: "kind: ConfigMapList\nitems:\n- apiVersion: v1\n  kind: ConfigMap\n  metadata: {name: a}\n",
		kubectl: "ACCEPT v1/ConfigMap",
		want:    `{"documents":[{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"a"}}],"differs_from_text":[[]]}`},
	// kubectl refuses this list itself for its apiVersion; here the item is
	// refused for the "" it takes. Both refuse.
	{name: "list-apiversion-number", in: "apiVersion: 5\nkind: ConfigMapList\nitems:\n- metadata: {name: a}\n",
		kubectl: "cannot unmarshal number into Go struct field metadataOnlyObject.TypeMeta.apiVersion",
		wantErr: "items[0] (line 4): it is not a Kubernetes object: its `apiVersion` is empty"},
	// The list's kind decides the item's, whatever it is.
	{name: "kind-configmap-with-items", in: "apiVersion: v1\nkind: ConfigMap\nitems:\n- metadata: {name: a}\n",
		kubectl: "ACCEPT v1/ConfigMap",
		want:    `{"documents":[{"metadata":{"name":"a"},"apiVersion":"v1","kind":"ConfigMap"}],"differs_from_text":[[]]}`},
	{name: "listlist", in: "apiVersion: v1\nkind: ConfigMapListList\nitems:\n- metadata: {name: a}\n",
		kubectl: "ACCEPT v1/ConfigMapList",
		want:    `{"documents":[{"metadata":{"name":"a"},"apiVersion":"v1","kind":"ConfigMapList"}],"differs_from_text":[[]]}`},
	{name: "group", in: "apiVersion: example.com/v1\nkind: FooList\nitems:\n- metadata: {name: f}\n",
		kubectl: "ACCEPT example.com/v1/Foo",
		want:    `{"documents":[{"metadata":{"name":"f"},"apiVersion":"example.com/v1","kind":"Foo"}],"differs_from_text":[[]]}`},
	// A list without `kind` is refused as a document, before its items.
	{name: "list-no-kind", in: "apiVersion: v1\nitems:\n- apiVersion: v1\n  kind: ConfigMap\n  metadata: {name: a}\n",
		kubectl: "Object 'Kind' is missing (the list)",
		wantErr: "it is not a Kubernetes object: it has no `kind`"},
	// A list inside a list is told first, before the item's type.
	{name: "nested-in-list", in: listHead + "- apiVersion: v1\n  kind: ConfigMapList\n  items:\n" +
		"  - metadata: {name: inner-both-missing}\n  - apiVersion: v1\n    kind: ConfigMap\n    metadata: {name: inner-full}\n",
		kubectl: "is not a list: no Items field in this object",
		wantErr: "items[0] (line 4): a list inside a list is not read"},
	{name: "nested-in-cmlist", in: cmListHead + "- apiVersion: v1\n  kind: ConfigMapList\n  items:\n  - metadata: {name: inner-both-missing}\n",
		kubectl: "is not a list: no Items field in this object",
		wantErr: "items[0] (line 4): a list inside a list is not read"},
	{name: "nested-item-both-missing", in: cmListHead + "- items:\n  - metadata: {name: x}\n",
		kubectl: "is not a list: no Items field in this object",
		wantErr: "items[0] (line 4): a list inside a list is not read"},
	{name: "nested-item-kind-missing-in-list", in: listHead + "- apiVersion: v1\n  items: []\n",
		kubectl: "is not a list: no Items field in this object",
		wantErr: "items[0] (line 4): a list inside a list is not read"},
	// Good and bad items: kubectl applies 0 and 2 and refuses 1; the
	// refusal here names 1 (and stops the file, which is stricter).
	{name: "mixed", in: cmListHead + "- metadata: {name: good-inherited}\n- apiVersion: v1\n  metadata: {name: bad-missing-kind}\n" +
		"- apiVersion: v1\n  kind: ConfigMap\n  metadata: {name: good-full}\n",
		kubectl: "ACCEPT v1/ConfigMap, ACCEPT v1/ConfigMap, then Object 'Kind' is missing",
		wantErr: "in this list, items[1] (line 5): it is not a Kubernetes object: it has no `kind`"},
	// A null item is an empty object to kubectl and here: read as the `{}`
	// rows below, with the same output, and no difference for its text.
	{name: "empty-item", in: cmListHead + "- {}\n",
		kubectl: "ACCEPT v1/ConfigMap (name \"\")",
		want:    `{"documents":[{"apiVersion":"v1","kind":"ConfigMap"}],"differs_from_text":[[]]}`},
	{name: "null-item", in: cmListHead + "- null\n",
		kubectl: "ACCEPT v1/ConfigMap (name \"\")",
		want:    `{"documents":[{"apiVersion":"v1","kind":"ConfigMap"}],"differs_from_text":[[]]}`},
	{name: "empty-item-of-list", in: listHead + "- {}\n",
		kubectl: "Object 'Kind' is missing",
		wantErr: nullOrEmptyOfList},
	{name: "null-item-of-list", in: listHead + "- null\n",
		kubectl: "Object 'Kind' is missing",
		wantErr: nullOrEmptyOfList},
	// An item that is no mapping: kubectl refuses the whole list as it
	// decodes it (none of its items, item 0 neither).
	{name: "number-item", in: cmListHead + "- {apiVersion: v1, kind: ConfigMap, metadata: {name: ok}}\n- 5\n",
		kubectl: "cannot unmarshal number into Go value of type map[string]interface {} (0 accepted)",
		wantErr: "items[1] (line 5): it is not a Kubernetes object: the item is a number, not a mapping " +
			"(kubectl: cannot unmarshal number into Go value of type map[string]interface {}, and reads none of this list's items)"},
	{name: "string-item", in: cmListHead + "- {apiVersion: v1, kind: ConfigMap, metadata: {name: ok}}\n- x\n",
		kubectl: "cannot unmarshal string into Go value of type map[string]interface {} (0 accepted)",
		wantErr: "items[1] (line 5): it is not a Kubernetes object: the item is a string, not a mapping " +
			"(kubectl: cannot unmarshal string into Go value of type map[string]interface {}"},
	{name: "bool-item", in: cmListHead + "- {apiVersion: v1, kind: ConfigMap, metadata: {name: ok}}\n- true\n",
		kubectl: "cannot unmarshal bool into Go value of type map[string]interface {} (0 accepted)",
		wantErr: "items[1] (line 5): it is not a Kubernetes object: the item is a bool, not a mapping " +
			"(kubectl: cannot unmarshal bool into Go value of type map[string]interface {}"},
	{name: "array-item", in: cmListHead + "- {apiVersion: v1, kind: ConfigMap, metadata: {name: ok}}\n- [x]\n",
		kubectl: "cannot unmarshal array into Go value of type map[string]interface {} (0 accepted)",
		wantErr: "items[1] (line 5): it is not a Kubernetes object: the item is an array, not a mapping " +
			"(kubectl: cannot unmarshal array into Go value of type map[string]interface {}"},
	// The text is compared, not the item as it is given: the type taken
	// adds no difference, and an item's own differences keep their paths.
	{name: "differences-of-a-typed-item", in: cmListHead + "- metadata: {name: a, labels: {010: x}}\n",
		kubectl: "ACCEPT v1/ConfigMap",
		want: `{"documents":[{"metadata":{"name":"a","labels":{"8":"x"}},"apiVersion":"v1","kind":"ConfigMap"}],` +
			`"differs_from_text":[[{"kind":"keys","path":["metadata","labels"],"read_keys":["8"],"written_keys":["010"]}]]}`},
}
