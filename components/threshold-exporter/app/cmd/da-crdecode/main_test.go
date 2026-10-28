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

// runOn writes *content* to a temp file and runs the CLI on it.
func runOn(t *testing.T, content string) (int, string, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "cr.yaml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	rc := run([]string{path}, strings.NewReader(""), &stdout, &stderr)
	return rc, stdout.String(), stderr.String()
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
		"no-args":      nil,
		"two-args":     {"a", "b"},
		"help":         {"-h"},
		"empty":        {""},
		"missing-file": {missing},
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
	rc := run([]string{"-"}, strings.NewReader("a: 1\n---\nb: [x]\n"), &stdout, &stderr)
	if want := `{"documents":[{"a":1},{"b":["x"]}],"differs_from_text":[[],[]]}` + "\n"; rc != exitOK ||
		stdout.String() != want || stderr.Len() != 0 {
		t.Fatalf("rc=%d stdout=%q stderr=%q; want %q", rc, stdout.String(), stderr.String(), want)
	}
	stdout.Reset()
	rc = run([]string{"-"}, strings.NewReader("a: .inf\n"), &stdout, &stderr)
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
	rc, out, errOut := runOn(t, cm)
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
	if rc := run([]string{"-"}, strings.NewReader(inner), &stdout, &stderr); rc != exitOK ||
		stdout.String() != `{"documents":[{"route":{"receiver":"r"},"x":8}],"differs_from_text":[[]]}`+"\n" {
		t.Fatalf("inner: rc=%d stdout=%q stderr=%q", rc, stdout.String(), stderr.String())
	}
}
