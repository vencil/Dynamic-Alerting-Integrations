package main

import (
	"strings"
	"testing"
)

// #3490: every document's `apiVersion` and `metadata` must decode into the
// types kubectl reads them as (cli-runtime's metadata decoder:
// metadataOnlyObject, metav1.TypeMeta and metav1.ObjectMeta, unmarshalled
// with k8s.io/apimachinery/pkg/util/json), a list included; so must every
// item as it is given.
//
// Every row's `kubectl` is what k8s.io/cli-runtime v0.37.0 made of the same
// input, measured as in list_item_test.go (the local builder, unstructured,
// flattened: what `kubectl apply -f` visits): ACCEPT, or the error it
// stopped at. The refusals here carry the same words, from the same library
// call. Rows marked STRICTER are items kubectl visits without a word when
// it reads the file; applying them does not apply them as written (the
// client or the API server refuses them, or the item is rewritten), and
// here they are refused.
func TestManifestMetadataDecodesAsKubectlReadsIt(t *testing.T) {
	t.Parallel()
	for _, tc := range metadataCases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rc, out, errOut := runManifest(t, tc.in)
			if tc.wantErr == "" {
				if rc != exitOK || errOut != "" || !strings.HasPrefix(out, `{"documents":[{`) {
					t.Fatalf("kubectl: %s\nrc=%d stdout=%q stderr=%q; want rc 0 and the document",
						tc.kubectl, rc, out, errOut)
				}
				return
			}
			if rc != exitCallerErr || out != "" || !strings.Contains(errOut, tc.wantErr) {
				t.Fatalf("kubectl: %s\nrc=%d stdout=%q stderr=%q; want rc 2, stderr with %q",
					tc.kubectl, rc, out, errOut, tc.wantErr)
			}
			if strings.Contains(errOut, quoteHintText) != tc.hint {
				t.Fatalf("stderr=%q; want the quoting hint: %v", errOut, tc.hint)
			}
		})
	}
}

const (
	// metaDoc is the start of a document's refusal for its metadata, up to
	// kubectl's own error.
	metaDoc = "it is not a Kubernetes object: its `apiVersion` or `metadata` does not decode into the " +
		"types kubectl reads them as, metav1.TypeMeta and metav1.ObjectMeta (kubectl: "
	// metaItem is the same for a list item, after its `items[i] (line N): `.
	metaItem = "it is not a Kubernetes object: its `apiVersion` or `metadata` does not decode into the " +
		"types kubectl reads a document's as, metav1.TypeMeta and metav1.ObjectMeta ("
	// cmHead starts a ConfigMap document, before its `metadata`.
	cmHead = "apiVersion: v1\nkind: ConfigMap\n"
	// nameErr is kubectl's error for a `name` that is a number.
	nameErr = "json: cannot unmarshal number into Go struct field ObjectMeta.metadata.name of type string"
	// apiVersionErr is kubectl's error for an `apiVersion` that is a number.
	apiVersionErr = "json: cannot unmarshal number into Go struct field metadataOnlyObject.TypeMeta.apiVersion of type string"
	// metadataNumberErr is kubectl's error for a `metadata` that is a number.
	metadataNumberErr = "json: cannot unmarshal number into Go struct field metadataOnlyObject.metadata of type v1.ObjectMeta"
)

// quoteHintText is the sentence a refusal carries when kubectl's error is a
// value put into a string field (*json.UnmarshalTypeError, a string Type):
// an unquoted 010 or yes. Not for a time that does not parse, a mapping
// field given a number, an integer field given text.
const quoteHintText = "; a value the YAML reads as a number or a boolean (an unquoted 010, 1.5, yes) " +
	"is not text: quote it if text is meant"

// metadataCases are the rows of TestManifestMetadataDecodesAsKubectlReadsIt.
var metadataCases = []struct {
	name, in string
	kubectl  string
	// wantErr is a part of stderr on a refusal; "" for a success.
	wantErr string
	// hint: the refusal carries quoteHintText.
	hint bool
}{
	// A document: refused.
	{name: "apiversion-number", hint: true, in: "apiVersion: 5\nkind: ConfigMap\nmetadata: {name: a}\n",
		kubectl: apiVersionErr, wantErr: metaDoc + apiVersionErr + ")"},
	{name: "name-number", hint: true, in: cmHead + "metadata: {name: 5}\n",
		kubectl: nameErr, wantErr: metaDoc + nameErr + ")"},
	{name: "metadata-number", in: cmHead + "metadata: 5\n",
		kubectl: metadataNumberErr, wantErr: metaDoc + metadataNumberErr + ")"},
	{name: "metadata-array", in: cmHead + "metadata: []\n",
		kubectl: "json: cannot unmarshal array into Go struct field metadataOnlyObject.metadata of type v1.ObjectMeta",
		wantErr: "(kubectl: json: cannot unmarshal array into Go struct field metadataOnlyObject.metadata of type v1.ObjectMeta)"},
	{name: "label-value-number", hint: true, in: cmHead + "metadata: {name: a, labels: {x: 5}}\n",
		kubectl: "json: cannot unmarshal number into Go struct field ObjectMeta.metadata.labels of type string",
		wantErr: "(kubectl: json: cannot unmarshal number into Go struct field ObjectMeta.metadata.labels of type string)"},
	{name: "namespace-number", hint: true, in: cmHead + "metadata: {name: a, namespace: 5}\n",
		kubectl: "json: cannot unmarshal number into Go struct field ObjectMeta.metadata.namespace of type string",
		wantErr: "(kubectl: json: cannot unmarshal number into Go struct field ObjectMeta.metadata.namespace of type string)"},
	{name: "creationtimestamp-garbage", in: cmHead + "metadata: {name: a, creationTimestamp: garbage}\n",
		kubectl: `parsing time "garbage" as "2006-01-02T15:04:05Z07:00": cannot parse "garbage" as "2006"`,
		wantErr: `(kubectl: parsing time "garbage" as "2006-01-02T15:04:05Z07:00": cannot parse "garbage" as "2006")`},
	{name: "creationtimestamp-number", hint: true, in: cmHead + "metadata: {name: a, creationTimestamp: 5}\n",
		kubectl: "json: cannot unmarshal number into Go struct field ObjectMeta.metadata.creationTimestamp of type string",
		wantErr: "(kubectl: json: cannot unmarshal number into Go struct field ObjectMeta.metadata.creationTimestamp of type string)"},
	// A date without a time is no RFC 3339 time.
	{name: "creationtimestamp-date", in: cmHead + "metadata: {name: a, creationTimestamp: 2024-01-01}\n",
		kubectl: `parsing time "2024-01-01" as "2006-01-02T15:04:05Z07:00": cannot parse "" as "T"`,
		wantErr: `(kubectl: parsing time "2024-01-01" as "2006-01-02T15:04:05Z07:00": cannot parse "" as "T")`},
	{name: "generation-string", in: cmHead + "metadata: {name: a, generation: x}\n",
		kubectl: "json: cannot unmarshal string into Go struct field ObjectMeta.metadata.generation of type int64",
		wantErr: "(kubectl: json: cannot unmarshal string into Go struct field ObjectMeta.metadata.generation of type int64)"},
	{name: "generation-fraction", in: cmHead + "metadata: {name: a, generation: 1.5}\n",
		kubectl: "json: cannot unmarshal number 1.5 into Go struct field ObjectMeta.metadata.generation of type int64",
		wantErr: "(kubectl: json: cannot unmarshal number 1.5 into Go struct field ObjectMeta.metadata.generation of type int64)"},
	{name: "ownerreferences-number", in: cmHead + "metadata: {name: a, ownerReferences: [5]}\n",
		kubectl: "json: cannot unmarshal number into Go struct field ObjectMeta.metadata.ownerReferences of type v1.OwnerReference",
		wantErr: "(kubectl: json: cannot unmarshal number into Go struct field ObjectMeta.metadata.ownerReferences of type v1.OwnerReference)"},
	{name: "ownerreference-uid-number", hint: true, in: cmHead + "metadata: {name: a, ownerReferences: [{uid: 5}]}\n",
		kubectl: "json: cannot unmarshal number into Go struct field OwnerReference.metadata.ownerReferences.uid of type types.UID",
		wantErr: "(kubectl: json: cannot unmarshal number into Go struct field OwnerReference.metadata.ownerReferences.uid of type types.UID)"},
	{name: "finalizer-number", hint: true, in: cmHead + "metadata: {name: a, finalizers: [5]}\n",
		kubectl: "json: cannot unmarshal number into Go struct field ObjectMeta.metadata.finalizers of type string",
		wantErr: "(kubectl: json: cannot unmarshal number into Go struct field ObjectMeta.metadata.finalizers of type string)"},
	{name: "managedfields-time-bad", in: cmHead + "metadata: {name: a, managedFields: [{time: bad}]}\n",
		kubectl: `parsing time "bad" as "2006-01-02T15:04:05Z07:00": cannot parse "bad" as "2006"`,
		wantErr: `(kubectl: parsing time "bad" as "2006-01-02T15:04:05Z07:00": cannot parse "bad" as "2006")`},
	{name: "deletiongraceperiodseconds-string", in: cmHead + "metadata: {name: a, deletionGracePeriodSeconds: x}\n",
		kubectl: "json: cannot unmarshal string into Go struct field ObjectMeta.metadata.deletionGracePeriodSeconds of type int64",
		wantErr: "(kubectl: json: cannot unmarshal string into Go struct field ObjectMeta.metadata.deletionGracePeriodSeconds of type int64)"},
	{name: "annotation-value-bool", hint: true, in: cmHead + "metadata: {name: a, annotations: {x: true}}\n",
		kubectl: "json: cannot unmarshal bool into Go struct field ObjectMeta.metadata.annotations of type string",
		wantErr: "(kubectl: json: cannot unmarshal bool into Go struct field ObjectMeta.metadata.annotations of type string)"},

	// A document: accepted. Null anywhere, keys of another case, fields
	// the types do not have, and a whole-number exponent.
	{name: "metadata-null", in: cmHead + "metadata: null\n", kubectl: "ACCEPT"},
	{name: "label-value-null", in: cmHead + "metadata: {name: a, labels: {x: null}}\n", kubectl: "ACCEPT"},
	{name: "name-null", in: cmHead + "metadata: {name: null}\n", kubectl: "ACCEPT (name \"\")"},
	{name: "creationtimestamp-null", in: cmHead + "metadata: {name: a, creationTimestamp: null}\n", kubectl: "ACCEPT"},
	{name: "apiversion-null", in: "apiVersion: null\nkind: ConfigMap\nmetadata: {name: a}\n", kubectl: "ACCEPT"},
	{name: "managedfields-fieldsv1-number", in: cmHead + "metadata: {name: a, managedFields: [{fieldsV1: 5}]}\n",
		kubectl: "ACCEPT"},
	{name: "managedfields-fieldsv1-array", in: cmHead + "metadata: {name: a, managedFields: [{fieldsV1: [1]}]}\n",
		kubectl: "ACCEPT"},
	{name: "managedfields-fieldsv1-string", in: cmHead + "metadata: {name: a, managedFields: [{fieldsV1: x}]}\n",
		kubectl: "ACCEPT"},
	{name: "Metadata-key", in: cmHead + "Metadata: 5\nmetadata: {name: a}\n", kubectl: "ACCEPT"},
	{name: "APIVersion-key", in: "APIVersion: 5\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\n",
		kubectl: "ACCEPT"},
	{name: "Name-key", in: cmHead + "metadata: {Name: 5, name: a}\n", kubectl: "ACCEPT"},
	{name: "unknown-metadata-field", in: cmHead + "metadata: {name: a, bogus: 5}\n", kubectl: "ACCEPT"},
	{name: "generation-exponent", in: cmHead + "metadata: {name: a, generation: 1e3}\n", kubectl: "ACCEPT"},

	// Two fields of the wrong type: kubectl names the first in its JSON,
	// whose keys are sorted (apiVersion before metadata, name before
	// namespace), not the first the file writes.
	{name: "first-in-sorted-keys-apiversion", hint: true, in: "metadata: {name: 5}\nkind: ConfigMap\napiVersion: 5\n",
		kubectl: apiVersionErr, wantErr: metaDoc + apiVersionErr + ")"},
	{name: "first-in-sorted-keys-name", hint: true, in: "metadata: {namespace: 5, name: 5}\nkind: ConfigMap\napiVersion: v1\n",
		kubectl: nameErr, wantErr: metaDoc + nameErr + ")"},
	// The second document is refused, named by its first line (kubectl
	// applies the first, then stops at it; here the file is refused).
	{name: "second-document", hint: true, in: cmHead + "metadata: {name: a}\n---\n" + cmHead + "metadata: {name: 5}\n",
		kubectl: "ACCEPT the first, then " + nameErr,
		wantErr: "the document starting at line 5: " + metaDoc + nameErr + ")"},

	// A list's own `apiVersion` and `metadata`: refused, as kubectl
	// refuses the list.
	{name: "list-metadata-name-number", hint: true, in: "apiVersion: v1\nkind: List\nmetadata: {name: 5}\n" +
		"items:\n- {apiVersion: v1, kind: ConfigMap, metadata: {name: a}}\n",
		kubectl: nameErr, wantErr: metaDoc + nameErr + ")"},
	{name: "list-apiversion-number", hint: true, in: "apiVersion: 5\nkind: List\n" +
		"items:\n- {apiVersion: v1, kind: ConfigMap, metadata: {name: a}}\n",
		kubectl: apiVersionErr, wantErr: metaDoc + apiVersionErr + ")"},
	{name: "list-metadata-number", in: "apiVersion: v1\nkind: List\nmetadata: 5\n" +
		"items:\n- {apiVersion: v1, kind: ConfigMap, metadata: {name: a}}\n",
		kubectl: metadataNumberErr, wantErr: metaDoc + metadataNumberErr + ")"},
	// Decoding the list comes first: an `items` that is no list, an item
	// that is no mapping is what kubectl names, not the list's metadata.
	{name: "items-number-before-apiversion", in: "apiVersion: 5\nkind: List\nitems: 5\n",
		kubectl: "json: cannot unmarshal number into Go struct field decodeList.items of type []json.RawMessage",
		wantErr: "a list document's `items` is number, not a list (kubectl: cannot unmarshal number into Go struct field decodeList.items)"},
	{name: "item-number-before-list-metadata", in: "apiVersion: v1\nkind: List\nmetadata: 5\nitems:\n- 5\n",
		kubectl: "json: cannot unmarshal number into Go value of type map[string]interface {}",
		wantErr: "in this list, items[0] (line 5): it is not a Kubernetes object: the item is a number, not a mapping"},
	{name: "items-null-list-metadata", in: "apiVersion: v1\nkind: List\nmetadata: 5\nitems: null\n",
		kubectl: metadataNumberErr, wantErr: metaDoc + metadataNumberErr + ")"},
	// The list's metadata comes before anything asked of its items: their
	// type, a list inside a list.
	{name: "list-metadata-before-item-type", in: "apiVersion: v1\nkind: List\nmetadata: 5\nitems:\n- {metadata: {name: a}}\n",
		kubectl: metadataNumberErr, wantErr: metaDoc + metadataNumberErr + ")"},
	{name: "list-metadata-before-nested-list", in: "apiVersion: v1\nkind: List\nmetadata: 5\n" +
		"items:\n- {kind: ConfigMap, apiVersion: v1, items: []}\n",
		kubectl: metadataNumberErr, wantErr: metaDoc + metadataNumberErr + ")"},

	// STRICTER: an item's own `apiVersion` and `metadata`. kubectl reads the
	// file without checking them; applying the item refuses or rewrites it.
	{name: "item-name-number", hint: true, in: "apiVersion: v1\nkind: List\n" +
		"items:\n- {apiVersion: v1, kind: ConfigMap, metadata: {name: 5}}\n",
		kubectl: "ACCEPT (name \"\"); STRICTER: apply refuses it (resource name may not be empty)",
		wantErr: "in this list, items[0] (line 4): " + metaItem + nameErr + "); kubectl does not check this of an " +
			"item when it reads the file, but applying the item would not apply it as written"},
	{name: "item-metadata-number", in: "apiVersion: v1\nkind: List\n" +
		"items:\n- {apiVersion: v1, kind: ConfigMap, metadata: 5}\n",
		kubectl: "ACCEPT (name \"\"); STRICTER: apply refuses it",
		wantErr: "in this list, items[0] (line 4): " + metaItem + metadataNumberErr + ")"},
	{name: "item-label-value-number", hint: true, in: "apiVersion: v1\nkind: List\n" +
		"items:\n- {apiVersion: v1, kind: ConfigMap, metadata: {name: a, labels: {x: 5}}}\n",
		kubectl: "ACCEPT; STRICTER: the API server refuses it",
		wantErr: "in this list, items[0] (line 4): " + metaItem +
			"json: cannot unmarshal number into Go struct field ObjectMeta.metadata.labels of type string)"},
	{name: "item-creationtimestamp-bad", in: "apiVersion: v1\nkind: List\n" +
		"items:\n- {apiVersion: v1, kind: ConfigMap, metadata: {name: a, creationTimestamp: bad}}\n",
		kubectl: "ACCEPT; STRICTER: the API server refuses it",
		wantErr: "in this list, items[0] (line 4): " + metaItem +
			`parsing time "bad" as "2006-01-02T15:04:05Z07:00": cannot parse "bad" as "2006")`},
	{name: "item-namespace-number-second", hint: true, in: "apiVersion: v1\nkind: List\n" +
		"items:\n- {apiVersion: v1, kind: ConfigMap, metadata: {name: a}}\n" +
		"- {apiVersion: v1, kind: ConfigMap, metadata: {name: b, namespace: 5}}\n",
		kubectl: "ACCEPT, ACCEPT (namespace \"\"); STRICTER: apply puts item 1 in the default namespace",
		wantErr: "in this list, items[1] (line 5): " + metaItem +
			"json: cannot unmarshal number into Go struct field ObjectMeta.metadata.namespace of type string)"},
	// The item is checked as it is given: an `apiVersion` the list's type
	// replaced is not checked, the one it took is.
	{name: "item-apiversion-replaced", in: "apiVersion: v1\nkind: ConfigMapList\n" +
		"items:\n- {apiVersion: 5, metadata: {name: a}}\n",
		kubectl: "ACCEPT v1/ConfigMap"},
}
