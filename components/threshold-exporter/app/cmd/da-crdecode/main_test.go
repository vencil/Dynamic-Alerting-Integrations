package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	yamlv2 "go.yaml.in/yaml/v2"
	"sigs.k8s.io/yaml"
)

// runOn writes *content* to a temp file and runs the CLI on it with
// --any-shape: the conversion, split and as-written comparison, which most
// tests here are about, on documents that need not be Kubernetes objects.
func runOn(t *testing.T, content string) (int, string, string) {
	t.Helper()
	return runArgs(t, content, anyShapeFlag)
}

// runManifest runs the CLI on *content* the default way: a manifest, every
// document a Kubernetes object (#3426).
func runManifest(t *testing.T, content string) (int, string, string) {
	t.Helper()
	return runArgs(t, content)
}

func runArgs(t *testing.T, content string, flags ...string) (int, string, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "cr.yaml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	rc := run(append(flags, path), strings.NewReader(""), &stdout, &stderr)
	reading := readingManifest
	if len(flags) > 0 {
		reading = readingAnyShape
	}
	return rc, withoutReading(t, stdout.String(), reading), stderr.String()
}

// withoutReading checks that a successful run's stdout ends with the
// `reading` it was asked for, and drops that field, so the expected outputs
// below spell only documents and differences.
func withoutReading(t *testing.T, out, reading string) string {
	t.Helper()
	if out == "" {
		return out
	}
	suffix := `,"reading":"` + reading + `"}` + "\n"
	if !strings.HasSuffix(out, suffix) {
		t.Fatalf("stdout %q does not end with %q", out, suffix)
	}
	return strings.TrimSuffix(out, suffix) + "}\n"
}

// #3426: without --any-shape, every document must be what kubectl decodes
// into one object (unstructured.UnstructuredJSONScheme, measured at
// apimachinery v0.37.0): a mapping with a non-empty string `kind`. The
// refusal names the document's first line. Lists: TestManifestExpandsLists.
func TestManifestDocumentsMustBeKubernetesObjects(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, wantErr string
	}{
		{"array", "- a\n- b\n", "the document is array, not a mapping (kubectl: cannot unmarshal array"},
		{"scalar", "x\n", "the document is string, not a mapping (kubectl: cannot unmarshal string"},
		{"number", "8\n", "the document is number, not a mapping"},
		{"no-kind", "apiVersion: v1\nmetadata: {name: a}\n", "it has no `kind` (kubectl: Object 'Kind' is missing)"},
		{"groups-no-kind", "groups: []\n", "it has no `kind`"},
		{"kind-not-string", "kind: 1\n", "its `kind` is number, not a string"},
		{"kind-null", "kind: null\n", "its `kind` is null, not a string"},
		{"kind-list-value", "kind: [ConfigMap]\n", "its `kind` is array, not a string"},
		{"kind-empty", "kind: \"\"\n", "its `kind` is empty"},
		// Kind first, as kubectl: `{items: []}` is refused for its kind.
		{"items-without-kind", "items: []\n", "it has no `kind`"},
		// The third document is the one refused, and named by its first line.
		{"third-document", "kind: A\n---\nkind: B\n---\n# note\n- x\n",
			"the document starting at line 5: it is not a Kubernetes object: the document is array"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rc, out, errOut := runManifest(t, tc.in)
			if rc != exitCallerErr || out != "" || !strings.Contains(errOut, tc.wantErr) ||
				!strings.HasPrefix(errOut, programName+": ") {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2, no stdout, stderr with %q",
					rc, out, errOut, tc.wantErr)
			}
			// --any-shape reads every one of them.
			if rc, out, errOut := runOn(t, tc.in); rc != exitOK || errOut != "" ||
				!strings.HasPrefix(out, `{"documents":[`) {
				t.Fatalf("--any-shape: rc=%d stdout=%q stderr=%q; want rc 0", rc, out, errOut)
			}
		})
	}
}

// What kubectl reads as an object passes the manifest reading unchanged;
// documents kubectl skips (empty, null) are still skipped, not refused.
func TestManifestReadsKubernetesObjects(t *testing.T) {
	t.Parallel()
	in := "---\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\ndata: {k: \"010\"}\n" +
		"---\n# only a comment\n---\nnull\n---\nkind: Namespace\nmetadata: {labels: {i: 010}}\n"
	want := `{"documents":[{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"a"},"data":{"k":"010"}},` +
		`{"kind":"Namespace","metadata":{"labels":{"i":8}}}],"differs_from_text":[[],[]]}` + "\n"
	for name, run := range map[string]func(*testing.T, string) (int, string, string){
		"manifest": runManifest, "any-shape": runOn} {
		if rc, out, errOut := run(t, in); rc != exitOK || out != want || errOut != "" {
			t.Fatalf("%s: rc=%d stdout=%q stderr=%q; want %q", name, rc, out, errOut, want)
		}
	}
	if rc, out, errOut := runManifest(t, ""); rc != exitOK ||
		out != `{"documents":[],"differs_from_text":[]}`+"\n" || errOut != "" {
		t.Fatalf("empty file: rc=%d stdout=%q stderr=%q", rc, out, errOut)
	}
}

func TestDecodesAsKubernetesClientsDo(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, want string
	}{
		// YAML 1.1 (PyYAML) reads these names as strings; the Kubernetes
		// client conversion does not (#2476).
		{"octal-0o", "name: 0o17\n", `{"documents":[{"name":15}],"differs_from_text":[[]]}` + "\n"},
		{"exponent", "name: 1e3\n", `{"documents":[{"name":1000}],"differs_from_text":[[]]}` + "\n"},
		{"leading-zero", "name: 08\n", `{"documents":[{"name":8}],"differs_from_text":[[]]}` + "\n"},
		{"y-is-bool", "name: y\n", `{"documents":[{"name":true}],"differs_from_text":[[]]}` + "\n"},
		// Keys become strings, in the order written; `010` is read as "8".
		{"keys", "b: 1\n010: 2\n", `{"documents":[{"b":1,"8":2}],"differs_from_text":[[` +
			`{"kind":"keys","path":[],"read_keys":["8"],"written_keys":["010"]}]]}` + "\n"},
		// kubectl applies nothing from an empty file: no document.
		{"empty-file", "", `{"documents":[],"differs_from_text":[]}` + "\n"},
		{"leading-separator", "---\na: 1\n", `{"documents":[{"a":1}],"differs_from_text":[[]]}` + "\n"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rc, out, errOut := runOn(t, tc.in)
			if rc != exitOK || out != tc.want || errOut != "" {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 0 stdout %q", rc, out, errOut, tc.want)
			}
		})
	}
}

func TestRefusesWhatKubernetesClientsRefuse(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, wantErr string
	}{
		{"null-key", "{null: 1}\n", "unsupported map key"},
		{"tagged-null-with-value", "a: !!null team\n", "as a !!null"},
		{"not-utf8", "a: \xff\n", "invalid leading UTF-8"},
		{"self-referencing-anchor", "a: &a [1, *a]\n", "contains itself"},
		{"inf-has-no-json", "a: .inf\n", "unsupported value"},
		// `...` does not separate documents for kubectl's splitter, and under
		// kubectl the conversion takes only the first document of the piece
		// and drops the rest without a word; da-crdecode refuses it here.
		{"text-after-document-end", "a: 1\n...\nb: 2\n", "did not find expected <document start>"},
		// kubectl's YAMLReader refuses content after `---` on its line.
		{"content-after-separator", "a: 1\n--- b: 2\n", "line 2: invalid YAML document separator: b: 2"},
		{"glued-separator", "---x\n", "invalid YAML document separator: x"},
		// The document a refusal is in is named by its first line, and the
		// parser's line numbers are the file's.
		{"refused-second-document", "a: 1\n---\nb: .inf\n", "the document starting at line 3"},
		{"line-numbers-are-the-files", "a: 1\n---\nb: 1\nc: [\n", "the document starting at line 3: yaml: line 4:"},
		// #2476 F1: the int 8 and the string "8" are one JSON key; which one
		// YAMLToJSON keeps depends on Go's map iteration order.
		{"int-and-string-collide", "010: a\n\"8\": b\n\"010\": c\n", `the same JSON key "8"`},
		{"collide-nested", "spec:\n  tenants:\n    010: {}\n    \"8\": {}\n", "in spec.tenants"},
		{"collide-in-list", "x:\n- {yes: 1, \"true\": 2}\n", "in x[0]"},
		{"float-and-string", "1.5: a\n\"1.5\": b\n", `the same JSON key "1.5"`},
		// The int 8 comes in through a `<<` merge.
		{"collide-through-merge", mergeCollision, `in m, the keys "8" (string) and 8 (int)`},
		// Below a NaN key, which a map lookup by key never finds.
		{"collide-below-nan-key", "a:\n  .nan: {010: x, \"8\": y}\n", "in a..nan"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rc, out, errOut := runOn(t, tc.in)
			if rc != exitCallerErr || out != "" || !strings.Contains(errOut, tc.wantErr) {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2, no stdout, stderr with %q",
					rc, out, errOut, tc.wantErr)
			}
			if !strings.HasPrefix(errOut, programName+": ") {
				t.Fatalf("stderr %q does not start with %q", errOut, programName+": ")
			}
		})
	}
}

// mergeCollision brings the int 8 into `m` through a merge, next to "8".
const mergeCollision = "b: &b {010: x}\nm: {<<: *b, \"8\": y}\n"

// The conversion of a file with colliding keys used to vary from run to run;
// refused, it is the same every time — the stderr included.
func TestCollisionIsRefusedEveryTime(t *testing.T) {
	t.Parallel()
	for _, in := range []string{"010: a\n\"8\": b\n\"010\": c\n", mergeCollision} {
		_, _, first := runOn(t, in)
		for i := 0; i < 30; i++ {
			rc, out, errOut := runOn(t, in)
			if rc != exitCallerErr || out != "" || !strings.Contains(errOut, "same JSON key") {
				t.Fatalf("%q run %d: rc=%d stdout=%q stderr=%q", in, i, rc, out, errOut)
			}
			// The file path differs per run; the message after it must not.
			if tail(errOut) != tail(first) {
				t.Fatalf("%q run %d: stderr %q differs from %q", in, i, errOut, first)
			}
		}
	}
}

// tail drops the "da-crdecode: <path>: " prefix of a stderr line.
func tail(s string) string {
	if i := strings.Index(s, ".yaml: "); i >= 0 {
		return s[i:]
	}
	return s
}

// keyCollision relies on go-yaml v2 applying `<<` merges when it decodes
// into map[interface{}]interface{} (YAMLToJSON's path) — unlike MapSlice,
// which drops the merged keys. Measured here so a go-yaml upgrade that
// changes it turns red.
func TestMapDecodeAppliesMerge(t *testing.T) {
	t.Parallel()
	var doc map[interface{}]interface{}
	if err := yamlv2.Unmarshal([]byte(mergeCollision), &doc); err != nil {
		t.Fatal(err)
	}
	m, ok := doc["m"].(map[interface{}]interface{})
	if !ok || len(m) != 2 || m[8] != "x" || m["8"] != true {
		t.Fatalf("map decode of m = %#v; want the merged int 8 and the string \"8\"", doc["m"])
	}
	var slice yamlv2.MapSlice
	if err := yamlv2.Unmarshal([]byte(mergeCollision), &slice); err != nil {
		t.Fatal(err)
	}
	if ms, ok := slice[1].Value.(yamlv2.MapSlice); !ok || len(ms) != 1 {
		t.Fatalf("MapSlice decode of m = %#v; the merged key was expected to be dropped", slice[1].Value)
	}
}

// Keys that are one YAML value are not a collision: go-yaml keeps the last,
// every time (as before #2476).
func TestSameValueKeysStillDecode(t *testing.T) {
	t.Parallel()
	for in, want := range map[string]string{
		"t1: 1\nt1: 2\n": `{"documents":[{"t1":2}],"differs_from_text":[[]]}` + "\n",
		"8: a\n010: b\n": `{"documents":[{"8":"b"}],"differs_from_text":[[` +
			`{"kind":"keys","path":[],"read_keys":[],"written_keys":["010"]}]]}` + "\n",
		"- {a: 1, a: 2}\n": `{"documents":[[{"a":2}]],"differs_from_text":[[]]}` + "\n",
	} {
		rc, out, errOut := runOn(t, in)
		if rc != exitOK || out != want {
			t.Fatalf("%q: rc=%d stdout=%q stderr=%q; want %q", in, rc, out, errOut, want)
		}
	}
}

// jsonKey is a copy of sigs.k8s.io/yaml's key conversion; re-measure it
// against YAMLToJSON itself for the key types go-yaml v2 produces.
func TestJSONKeyMatchesYAMLToJSON(t *testing.T) {
	t.Parallel()
	for _, key := range []string{"t1", "010", "0x1F", "8", "-5", "1.5", "1.50", "1e3", "0.1",
		"3.14159265358979", "1e+23", "99999999999999999999999", "yes", "off", "y", "true",
		"\"8\"", "1_000", "0b101", "2024-01-01", "1:30", ".5", "-0"} {
		var doc yamlv2.MapSlice
		if err := yamlv2.Unmarshal([]byte(key+": 1\n"), &doc); err != nil || len(doc) != 1 {
			t.Fatalf("%s: %v %v", key, doc, err)
		}
		got, ok := jsonKey(doc[0].Key)
		j, err := yaml.YAMLToJSON([]byte(key + ": 1\n"))
		if err != nil || !ok {
			t.Fatalf("%s: ok=%v err=%v", key, ok, err)
		}
		var m map[string]interface{}
		if err := json.Unmarshal(j, &m); err != nil {
			t.Fatal(err)
		}
		if _, found := m[got]; !found || len(m) != 1 {
			t.Errorf("%s: jsonKey %q, YAMLToJSON %s", key, got, j)
		}
	}
}

func TestCallerErrors(t *testing.T) {
	t.Parallel()
	missing := filepath.Join(t.TempDir(), "missing.yaml")
	for name, args := range map[string][]string{
		"no-args":           nil,
		"two-args":          {"a", "b"},
		"help":              {"-h"},
		"empty":             {""},
		"missing-file":      {missing},
		"any-shape-alone":   {anyShapeFlag},
		"any-shape-two":     {anyShapeFlag, "a", "b"},
		"any-shape-after":   {missing, anyShapeFlag},
		"any-shape-missing": {anyShapeFlag, missing},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			var stdout, stderr bytes.Buffer
			if rc := run(args, strings.NewReader(""), &stdout, &stderr); rc != exitCallerErr || stdout.Len() != 0 || stderr.Len() == 0 {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2, a message, no stdout", rc, stdout.String(), stderr.String())
			}
		})
	}
}

// #3206 (ADR-036 §12): a file of several documents is split as `kubectl
// apply -f` splits it (YAMLReader + StreamVisitor): one entry per document
// kubectl applies, in file order. A piece that converts to nothing or to null
// — only `---`, blank lines, comments, an explicit null — is skipped, as
// kubectl skips it; `--- # note` is a separator.
func TestSeveralDocumentsAsKubectlSplitsThem(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, want string
	}{
		{"two", "a: 1\n---\nb: 2\n",
			`{"documents":[{"a":1},{"b":2}],"differs_from_text":[[],[]]}`},
		{"trailing-separator", "a: 1\n---\n",
			`{"documents":[{"a":1}],"differs_from_text":[[]]}`},
		{"empty-between", "---\n---\na: 1\n---\n\n---\n# only a comment\n---\nnull\n---\nb: 010\n",
			`{"documents":[{"a":1},{"b":8}],"differs_from_text":[[],[]]}`},
		{"separator-with-comment", "a: 1\n--- # next\nb: 2\n",
			`{"documents":[{"a":1},{"b":2}],"differs_from_text":[[],[]]}`},
		{"only-separators", "---\n---\n", `{"documents":[],"differs_from_text":[]}`},
		{"only-comments", "# nothing\n", `{"documents":[],"differs_from_text":[]}`},
		// Each document's differences are its own.
		{"differs-per-document", "x: {010: a}\n---\nw: {b: c}\n---\nz: {yes: d}\n",
			`{"documents":[{"x":{"8":"a"}},{"w":{"b":"c"}},{"z":{"true":"d"}}],"differs_from_text":[` +
				`[{"kind":"keys","path":["x"],"read_keys":["8"],"written_keys":["010"]}],[],` +
				`[{"kind":"keys","path":["z"],"read_keys":["true"],"written_keys":["yes"]}]]}`},
		// Several Namespaces with an unquoted label: Kubernetes reads 8.
		{"namespaces", "apiVersion: v1\nkind: Namespace\nmetadata: {name: a, labels: {instance: 010}}\n" +
			"---\napiVersion: v1\nkind: Namespace\nmetadata: {name: b, labels: {instance: \"010\"}}\n",
			`{"documents":[{"apiVersion":"v1","kind":"Namespace","metadata":{"name":"a","labels":{"instance":8}}},` +
				`{"apiVersion":"v1","kind":"Namespace","metadata":{"name":"b","labels":{"instance":"010"}}}],` +
				`"differs_from_text":[[],[]]}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rc, out, errOut := runOn(t, tc.in)
			if rc != exitOK || out != tc.want+"\n" || errOut != "" {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 0 stdout %q", rc, out, errOut, tc.want)
			}
		})
	}
}

// `-` reads standard input: the inner layer of a ConfigMap is handed over
// that way, with no temporary file.
func TestReadsStandardInput(t *testing.T) {
	t.Parallel()
	var stdout, stderr bytes.Buffer
	rc := run([]string{"-"}, strings.NewReader("kind: A\n---\nkind: B\nb: [x]\n"), &stdout, &stderr)
	if want := `{"documents":[{"kind":"A"},{"kind":"B","b":["x"]}],"differs_from_text":[[],[]],"reading":"manifest"}` + "\n"; rc != exitOK ||
		stdout.String() != want || stderr.Len() != 0 {
		t.Fatalf("rc=%d stdout=%q stderr=%q; want %q", rc, stdout.String(), stderr.String(), want)
	}
	stdout.Reset()
	rc = run([]string{anyShapeFlag, "-"}, strings.NewReader("a: 1\n---\n- x\n"), &stdout, &stderr)
	if want := `{"documents":[{"a":1},["x"]],"differs_from_text":[[],[]],"reading":"any-shape"}` + "\n"; rc != exitOK ||
		stdout.String() != want || stderr.Len() != 0 {
		t.Fatalf("--any-shape: rc=%d stdout=%q stderr=%q; want %q", rc, stdout.String(), stderr.String(), want)
	}
	stdout.Reset()
	rc = run([]string{"-"}, strings.NewReader("a: 1\n"), &stdout, &stderr)
	if rc != exitCallerErr || stdout.Len() != 0 ||
		stderr.String() != programName+": "+stdinName+": it is not a Kubernetes object: it has no `kind` "+
			"(kubectl: Object 'Kind' is missing)\n" {
		t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2 naming %s and the missing kind", rc, stdout.String(), stderr.String(), stdinName)
	}
	stdout.Reset()
	stderr.Reset()
	rc = run([]string{anyShapeFlag, "-"}, strings.NewReader("a: .inf\n"), &stdout, &stderr)
	if rc != exitCallerErr || stdout.Len() != 0 || !strings.HasPrefix(stderr.String(), programName+": "+stdinName+": ") {
		t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2 naming %s", rc, stdout.String(), stderr.String(), stdinName)
	}
}

// The two layers of a ConfigMap: the outer document is read the Kubernetes
// way, and the string it holds is read again from standard input.
func TestConfigMapInnerLayerThroughStandardInput(t *testing.T) {
	t.Parallel()
	cm := "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: am}\ndata:\n  alertmanager.yml: |\n" +
		"    route: {receiver: r}\n    x: 010\n"
	rc, out, errOut := runManifest(t, cm)
	if rc != exitOK {
		t.Fatalf("outer: rc=%d stderr=%q", rc, errOut)
	}
	var outer struct {
		Documents []struct {
			Data map[string]string `json:"data"`
		} `json:"documents"`
	}
	if err := json.Unmarshal([]byte(out), &outer); err != nil || len(outer.Documents) != 1 {
		t.Fatalf("outer: %v %s", err, out)
	}
	inner := outer.Documents[0].Data["alertmanager.yml"]
	var stdout, stderr bytes.Buffer
	if rc := run([]string{anyShapeFlag, "-"}, strings.NewReader(inner), &stdout, &stderr); rc != exitOK ||
		stdout.String() != `{"documents":[{"route":{"receiver":"r"},"x":8}],"differs_from_text":[[]],"reading":"any-shape"}`+"\n" {
		t.Fatalf("inner: rc=%d stdout=%q stderr=%q", rc, stdout.String(), stderr.String())
	}
}

// #3426: a list — any document with a top-level `items` — is replaced by its
// items, in order, as `kubectl apply -f` applies them (cli-runtime v0.37.0,
// local builder, flattened: measured). Each item's differences are its own,
// with paths from the item.
func TestManifestExpandsLists(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, want string
	}{
		{"list", "apiVersion: v1\nkind: List\nitems:\n- {apiVersion: v1, kind: ConfigMap, metadata: {name: a}}\n" +
			"- {apiVersion: v1, kind: Secret, metadata: {name: b}}\n---\nkind: Namespace\n",
			`{"documents":[{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"a"}},` +
				`{"apiVersion":"v1","kind":"Secret","metadata":{"name":"b"}},{"kind":"Namespace"}],` +
				`"differs_from_text":[[],[],[]]}`},
		{"typed-list", "apiVersion: v1\nkind: ConfigMapList\nitems:\n- {apiVersion: v1, kind: ConfigMap}\n",
			`{"documents":[{"apiVersion":"v1","kind":"ConfigMap"}],"differs_from_text":[[]]}`},
		// kubectl reads any document with `items` as a list, whatever its kind.
		{"items-on-a-configmap", "kind: ConfigMap\nitems: []\n", `{"documents":[],"differs_from_text":[]}`},
		{"items-null", "apiVersion: v1\nkind: List\nitems: null\n", `{"documents":[],"differs_from_text":[]}`},
		{"items-empty", "apiVersion: v1\nkind: List\nitems: []\n---\nkind: A\n",
			`{"documents":[{"kind":"A"}],"differs_from_text":[[]]}`},
		// An item whose `items` is no JSON array is an object to kubectl
		// (cli-runtime v0.37.0, measured): null, a string, a number, an object.
		{"item-items-null", "kind: List\nitems:\n- {apiVersion: v1, kind: ConfigMap, items: null}\n",
			`{"documents":[{"apiVersion":"v1","kind":"ConfigMap","items":null}],"differs_from_text":[[]]}`},
		{"item-items-string", "kind: List\nitems:\n- {apiVersion: v1, kind: ConfigMap, items: s}\n",
			`{"documents":[{"apiVersion":"v1","kind":"ConfigMap","items":"s"}],"differs_from_text":[[]]}`},
		{"item-items-number", "kind: List\nitems:\n- {apiVersion: v1, kind: ConfigMap, items: 3}\n",
			`{"documents":[{"apiVersion":"v1","kind":"ConfigMap","items":3}],"differs_from_text":[[]]}`},
		{"item-items-object", "kind: List\nitems:\n- {apiVersion: v1, kind: ConfigMap, items: {x: 1}}\n",
			`{"documents":[{"apiVersion":"v1","kind":"ConfigMap","items":{"x":1}}],"differs_from_text":[[]]}`},
		// The differences of item 1 go to item 1, its path from the item.
		{"differs-per-item", "kind: List\nitems:\n- {apiVersion: v1, kind: A, x: {b: c}}\n" +
			"- {apiVersion: v1, kind: B, metadata: {labels: {010: x, yes: y}}}\n",
			`{"documents":[{"apiVersion":"v1","kind":"A","x":{"b":"c"}},` +
				`{"apiVersion":"v1","kind":"B","metadata":{"labels":{"8":"x","true":true}}}],"differs_from_text":[[],` +
				`[{"kind":"keys","path":["metadata","labels"],"read_keys":["8","true"],"written_keys":["010","yes"]}]]}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rc, out, errOut := runManifest(t, tc.in)
			if rc != exitOK || out != tc.want+"\n" || errOut != "" {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 0 stdout %q", rc, out, errOut, tc.want)
			}
		})
	}
}

// A list item kubectl would not apply is refused, named by its index and the
// line it starts on; so is a list inside a list (kubectl: "is not a list: no
// Items field in this object"), and a list whose own text is not read as
// written, whose differences no item could carry.
func TestManifestRefusesBadListItems(t *testing.T) {
	t.Parallel()
	const head = "apiVersion: v1\nkind: List\nitems:\n- {apiVersion: v1, kind: ConfigMap, metadata: {name: ok}}\n"
	cases := []struct {
		name, in, wantErr string
	}{
		{"item-scalar", head + "- x\n", "in this list, items[1] (line 5): it is not a Kubernetes object: the document is string"},
		{"item-no-kind", head + "- {apiVersion: v1, metadata: {name: b}}\n", "items[1] (line 5): it is not a Kubernetes object: it has no `kind`"},
		{"item-kind-number", head + "- {apiVersion: v1, kind: 3}\n", "items[1] (line 5): it is not a Kubernetes object: its `kind` is number"},
		{"item-no-apiversion", head + "- {kind: ConfigMap}\n",
			"items[1] (line 5): it is not a Kubernetes object: it has no `apiVersion` (kubectl: Object 'apiVersion' is missing)"},
		{"nested-list", head + "- apiVersion: v1\n  kind: List\n  items: []\n", "items[1] (line 5): a list inside a list is not read"},
		{"item-with-items", head + "- {apiVersion: v1, kind: ConfigMap, items: [{kind: A}]}\n", "a list inside a list is not read"},
		// An empty array is a list to kubectl too.
		{"item-with-empty-items", head + "- {apiVersion: v1, kind: ConfigMap, items: []}\n", "items[1] (line 5): a list inside a list is not read"},
		{"items-not-a-list", "kind: List\nitems: {a: 1}\n", "a list document's `items` is object, not a list"},
		{"list-without-kind", "items:\n- {apiVersion: v1, kind: A}\n", "it has no `kind`"},
		{"list-text-differs", "kind: List\nmetadata: {labels: {010: x}}\nitems:\n- {apiVersion: v1, kind: A}\n",
			"a list document whose own text is not read as written (at .metadata.labels)"},
		// `items: null` is no item to give a difference to, as `items: []`.
		{"list-text-differs-items-null", "kind: List\nmetadata: {labels: {010: x}}\nitems: null\n",
			"a list document whose own text is not read as written (at .metadata.labels)"},
		{"list-text-differs-items-empty", "kind: List\nmetadata: {labels: {010: x}}\nitems: []\n",
			"a list document whose own text is not read as written (at .metadata.labels)"},
		{"second-document", "kind: A\n---\n" + head + "- [x]\n",
			"the document starting at line 3: in this list, items[1] (line 7): it is not a Kubernetes object: the document is array"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rc, out, errOut := runManifest(t, tc.in)
			if rc != exitCallerErr || out != "" || !strings.Contains(errOut, tc.wantErr) {
				t.Fatalf("rc=%d stdout=%q stderr=%q; want rc 2, stderr with %q", rc, out, errOut, tc.wantErr)
			}
			if rc, _, errOut := runOn(t, tc.in); rc != exitOK {
				t.Fatalf("--any-shape: rc=%d stderr=%q; want rc 0", rc, errOut)
			}
		})
	}
}
