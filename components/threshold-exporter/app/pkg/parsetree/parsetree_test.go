package parsetree

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// TestADR036Section2Example is the example of ADR-036 §2, value for value.
func TestADR036Section2Example(t *testing.T) {
	t.Parallel()
	got, bad := Parse([]byte("tenants:\n  010:\n    _routing:\n      receiver:\n        type: slack\n" +
		"        api_url: \"https://hooks.slack.com/x\"\n        send_resolved: yes\n      group_wait: 30s\n"))
	if bad != nil {
		t.Fatal(bad)
	}
	want := `{"tenants":{"010":{"_routing":{"group_wait":{"text":"30s","tag":"!!str","value":"30s"},` +
		`"receiver":{"api_url":{"text":"https://hooks.slack.com/x","tag":"!!str","value":"https://hooks.slack.com/x"},` +
		`"send_resolved":{"text":"yes","tag":"!!str","value":"yes"},"type":{"text":"slack","tag":"!!str","value":"slack"}}}}}}`
	if s := strings.ReplaceAll(mustJSON(t, got), `\u003c`, "<"); s != want {
		t.Fatalf("got  %s\nwant %s", s, want)
	}
}

// TestScalars pins text, tag and value per tag: decimal text for numbers,
// JSON literals for bool / null, no value for timestamps and non-finite
// floats.
func TestScalars(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct{ in, want string }{
		{"0777", `{"text":"0777","tag":"!!int","value":"511"}`},
		{"0x1F", `{"text":"0x1F","tag":"!!int","value":"31"}`},
		{"1_000", `{"text":"1_000","tag":"!!int","value":"1000"}`},
		{"-12", `{"text":"-12","tag":"!!int","value":"-12"}`},
		{"18446744073709551615", `{"text":"18446744073709551615","tag":"!!int","value":"18446744073709551615"}`},
		{"1e3", `{"text":"1e3","tag":"!!float","value":"1000"}`},
		{"0.1", `{"text":"0.1","tag":"!!float","value":"0.1"}`},
		{"!!float 1", `{"text":"1","tag":"!!float","value":"1"}`},
		{"99999999999999999999", `{"text":"99999999999999999999","tag":"!!float","value":"100000000000000000000"}`},
		{".inf", `{"text":".inf","tag":"!!float"}`},
		{".nan", `{"text":".nan","tag":"!!float"}`},
		{"2001-12-14", `{"text":"2001-12-14","tag":"!!timestamp"}`},
		{"true", `{"text":"true","tag":"!!bool","value":true}`},
		{"yes", `{"text":"yes","tag":"!!str","value":"yes"}`},
		{"off", `{"text":"off","tag":"!!str","value":"off"}`},
		{"~", `{"text":"~","tag":"!!null","value":null}`},
		{"", `{"text":"","tag":"!!null","value":null}`},
		{`"1"`, `{"text":"1","tag":"!!str","value":"1"}`},
		{"!!str 1", `{"text":"1","tag":"!!str","value":"1"}`},
		{"1e400", `{"text":"1e400","tag":"!!str","value":"1e400"}`},
	} {
		got, bad := Parse([]byte("a: " + tc.in + "\n"))
		if bad != nil {
			t.Errorf("%q: %v", tc.in, bad)
			continue
		}
		if s := mustJSON(t, got["a"]); s != tc.want {
			t.Errorf("%q: got %s, want %s", tc.in, s, tc.want)
		}
	}
}

// TestMergeExpansion: explicit keys win, then each source in order, the first
// source to set a key winning; nested merges and aliases are expanded; a
// quoted "<<" is a plain key; keys are source text.
func TestMergeExpansion(t *testing.T) {
	t.Parallel()
	got, bad := Parse([]byte("a: &a {x: 1, y: 1}\nb: &b {y: 2, z: 2, <<: {w: 3, x: 3}}\n" +
		"t: {<<: [*a, *b], x: 0, 010: k}\nq: {\"<<\": *a}\n"))
	if bad != nil {
		t.Fatal(bad)
	}
	tm := got["t"].(map[string]any)
	want := map[string]string{"x": "0", "y": "1", "z": "2", "w": "3", "010": "k"}
	if len(tm) != len(want) {
		t.Fatalf("keys %v, want %v", tm, want)
	}
	for k, v := range want {
		if s, ok := tm[k].(Scalar); !ok || s.Text != v {
			t.Errorf("t.%s = %v, want text %s", k, tm[k], v)
		}
	}
	if q := got["q"].(map[string]any); len(q) != 1 || q["<<"] == nil {
		t.Errorf("a quoted \"<<\" is a plain key: got %v", q)
	}
}

// TestKinds pins one text per kind (the corpus holds many more).
func TestKinds(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		in   string
		want Kind
	}{
		{"a: \xff\n", KindInvalidUTF8},
		{"a: 1 # \uFEFF\n", KindBOMNotAtStart},
		{"a: [\n", KindSyntaxError},
		{"a: 1\n---\nb: 1\n", KindMultipleDocuments},
		{"a: ! x\n", KindNonSpecificTag},
		{"a: !foo x\n", KindTagNotAllowed},
		{"? [a]\n: 1\n", KindNonScalarKey},
		{"~: 1\n", KindNullKey},
		{"a: 1\na: 2\n", KindDuplicateKey},
		{"a: !!int x\n", KindDecodeError},
		{"- a\n", KindTopLevelNotMapping},
	} {
		if _, bad := Parse([]byte(tc.in)); bad == nil || bad.Kind != tc.want {
			t.Errorf("%q: got %v, want %s", tc.in, bad, tc.want)
		}
	}
}

// TestTagTokenBelongsToItsOwnNode: a tag written on the next node is not the
// tag of an empty value or of an anchored collection before it.
func TestTagTokenBelongsToItsOwnNode(t *testing.T) {
	t.Parallel()
	for _, in := range []string{
		"a:\n!!str b: 1\n", "x: &a\n  !!str k: 1\n", "!!str k: 1\n", "a:\n- \n- !!str x\n",
		"{a: , !!str b: 1}\n", "a: &x\nb: !!int 1\n",
	} {
		if _, bad := Parse([]byte(in)); bad != nil {
			t.Errorf("%q: %v", in, bad)
		}
	}
}

// upstreamTags rewrites a parsed tree the way upstream yaml.v3 builds it,
// without the vendored patch that keeps the non-specific tag on a quoted or
// block scalar: there parser.node drops `!` and the node takes its default
// tag (!!str for a quoted or block scalar). Returns how many nodes changed.
func upstreamTags(n *yaml.Node) int {
	changed := 0
	if n.Kind == yaml.ScalarNode && n.Tag == "!" {
		n.Tag = "!!str"
		changed++
	}
	for _, c := range n.Content {
		changed += upstreamTags(c)
	}
	return changed
}

// TestNonSpecificTagWithoutThePatch: the refusal of `!` does not read the
// tag the vendored patch keeps — a tree built as upstream yaml.v3 builds it
// (ADR-036 §11 removes the patch) is refused the same way. The quoted
// spellings are the ones the patch changes; the rewrite must touch them
// (else this test would not simulate anything).
func TestNonSpecificTagWithoutThePatch(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		in      string
		patched bool // the vendored yaml.v3 keeps Tag "!" on some node
	}{
		{"a: ! \"x\"\n", true},
		{"x: &b {a: 1}\ny: {! \"<<\": *b}\n", true},
		{"slo:\n  objective: ! \"99.9\"\n", true},
		{"a: ! |\n  x\n", true},
		{"a: ! 1\n", false},
		{"a: ! {b: 1}\n", false},
		{"a: !<!> x\n", false},
	} {
		data := []byte(tc.in)
		doc, bad := parseDocument(data)
		if bad != nil || doc == nil {
			t.Fatalf("%q: %v", tc.in, bad)
		}
		if n := upstreamTags(doc); (n > 0) != tc.patched {
			t.Fatalf("%q: the rewrite changed %d nodes, want patched=%v", tc.in, n, tc.patched)
		}
		for _, verdictOnly := range []bool{false, true} { // Parse's walk, and Judge's
			if _, bad := judge(data, doc, verdictOnly); bad == nil || bad.Kind != KindNonSpecificTag {
				t.Errorf("%q without the patch (verdictOnly %v): got %v, want %s", tc.in, verdictOnly, bad, KindNonSpecificTag)
			}
		}
		if _, bad := Parse(data); bad == nil || bad.Kind != KindNonSpecificTag {
			t.Errorf("%q with the patch: got %v, want %s", tc.in, bad, KindNonSpecificTag)
		}
	}
}

// TestEmptyIsEmptyMapping: no document, comments only, `---` alone, null.
func TestEmptyIsEmptyMapping(t *testing.T) {
	t.Parallel()
	for _, in := range []string{"", "# c\n", "---\n", "~\n", "\uFEFF", "null\n"} {
		got, bad := Parse([]byte(in))
		if bad != nil || got == nil || len(got) != 0 {
			t.Errorf("%q: got %v, %v", in, got, bad)
		}
	}
}

// encodedLen is the length parse-tree writes for content (encoding/json,
// SetEscapeHTML(false), no trailing newline).
func encodedLen(t *testing.T, content Content) int {
	t.Helper()
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(content); err != nil {
		t.Fatal(err)
	}
	return b.Len() - 1
}

// TestContentByteCount: the count the bound is checked against is the JSON
// actually written, byte for byte — escapes, merges, aliases included.
func TestContentByteCount(t *testing.T) {
	t.Parallel()
	for _, in := range []string{
		"a: 1\n",
		"a: {}\nb: []\nc: [1, \"x\\ty\\u0001\\\"\\\\\\u2028\\b\\f<&>é\"]\n",
		"x: &x {a: 1, b: [2, 3]}\ny: {<<: *x, a: 9, c: .inf}\nz: [*x, *x]\nt: 2001-01-01\n\"k\\n\": ~\n",
		"",
	} {
		data := []byte(in)
		doc, bad := parseDocument(data)
		if bad != nil {
			t.Fatalf("%q: %v", in, bad)
		}
		if doc == nil {
			continue
		}
		c := &checker{src: newSource(data), values: map[*yaml.Node]json.RawMessage{}}
		c.src.index(doc.Content[0])
		if bad := c.checkNode(doc.Content[0], false); bad != nil {
			t.Fatalf("%q: %v", in, bad)
		}
		conv := &converter{values: c.values, active: map[*yaml.Node]bool{}, bytes: 2}
		out := Content{}
		if bad := conv.fill(out, doc.Content[0]); bad != nil {
			t.Fatalf("%q: %v", in, bad)
		}
		if want := encodedLen(t, out); conv.bytes != want {
			t.Errorf("%q: counted %d bytes, the encoder writes %d", in, conv.bytes, want)
		}
	}
}

// TestContentByteBound: one bound on the JSON bytes of a file's content —
// a long string repeated by aliases, a fan-out of short lists (both pass
// yaml.v3's own alias ratio), and a large plain file just under the bound.
func TestContentByteBound(t *testing.T) {
	t.Parallel()
	join := func(item string, n int) string { return strings.TrimSuffix(strings.Repeat(item+",", n), ",") }
	longString := "s: &s \"" + strings.Repeat("a", 100_000) + "\"\nl: [" + join("*s", 300) + "]\n"
	fanOut := "l0: &l0 [" + join(strings.Repeat("b", 60), 100) + "]\nl1: [" + join("*l0", 1000) + "]\n"
	for name, in := range map[string]string{"long string": longString, "fan-out": fanOut} {
		_, bad := Parse([]byte(in))
		if bad == nil || bad.Kind != KindDecodeError || !strings.Contains(bad.Message, "more than 8388608 bytes") {
			t.Errorf("%s (%d bytes): got %v", name, len(in), bad)
		}
	}
	if len(fanOut) > 12_000 {
		t.Fatalf("fan-out input is %d bytes; the point is a small file", len(fanOut))
	}

	// A plain file, no alias: N keys of 80-character values. Find the largest
	// N under the bound by its encoded size, then check it is read and N+1000
	// is not.
	plain := func(n int) []byte {
		var b strings.Builder
		for i := 0; i < n; i++ {
			fmt.Fprintf(&b, "k%07d: %s\n", i, strings.Repeat("v", 80))
		}
		return []byte(b.String())
	}
	n := 40_000
	content, bad := Parse(plain(n))
	if bad != nil {
		t.Fatalf("plain %d keys: %v", n, bad)
	}
	size := encodedLen(t, content)
	if size > MaxContentBytes || size < MaxContentBytes*9/10 {
		t.Fatalf("plain %d keys encode to %d bytes; the case must sit just under %d", n, size, MaxContentBytes)
	}
	t.Logf("a plain file of %d bytes encodes to %d bytes, under the bound", len(plain(n)), size)
	if _, bad := Parse(plain(n + 1000)); bad == nil || bad.Kind != KindDecodeError {
		t.Fatalf("plain %d keys: got %v, want over the bound", n+1000, bad)
	}
}
