package parsetree

import (
	"encoding/json"
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
		if _, bad := judge(data, doc); bad == nil || bad.Kind != KindNonSpecificTag {
			t.Errorf("%q without the patch: got %v, want %s", tc.in, bad, KindNonSpecificTag)
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

// TestExpansionBound: a small file whose aliases expand past
// MaxAliasExpansion — one yaml.v3's own alias ratio lets through — is a
// decode_error; one just under the bound is read.
func TestExpansionBound(t *testing.T) {
	t.Parallel()
	doc := func(aliases int) []byte {
		return []byte("l0: &l0 [" + strings.TrimSuffix(strings.Repeat("x,", 999), ",") + "]\nl1: [" +
			strings.TrimSuffix(strings.Repeat("*l0,", aliases), ",") + "]\n")
	}
	// Each alias expands to the list itself plus its 999 items: 1000 values.
	over, under := doc(MaxAliasExpansion/1000+1), doc(MaxAliasExpansion/1000)
	if len(over) > 5_000 {
		t.Fatalf("the input is %d bytes; the point is a small file", len(over))
	}
	if _, bad := Parse(over); bad == nil || bad.Kind != KindDecodeError || !strings.Contains(bad.Message, "aliases expand") {
		t.Fatalf("over the bound: got %v", bad)
	}
	if _, bad := Parse(under); bad != nil {
		t.Fatalf("under the bound: %v", bad)
	}
}
