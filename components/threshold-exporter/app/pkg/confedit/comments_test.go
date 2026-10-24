package confedit

// A map entry's comments and a sequence item's (#3206): written on the key /
// the item as `# …` lines, never content — the value's Content is the same
// with or without them.

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestCommentText(t *testing.T) {
	for _, tc := range []struct {
		text      string
		multiline bool
		want      string
		bad       bool
	}{
		{"a", false, "# a", false},
		{"a\n\nb", true, "# a\n#\n# b", false},
		{"key: v\n- x", true, "# key: v\n# - x", false},
		{"tab\there", false, "# tab\there", false},
		{"a\nb", false, "", true},
		{"a\rb", true, "", true},
		{"a\x00", true, "", true},
		{"a\x7f", true, "", true},
		{"a\u0085b", true, "", true},
		{"a b", true, "", true},
		{"a" + string(rune(0xfeff)) + "b", true, "", true}, // a byte order mark
	} {
		got, err := CommentText(tc.text, tc.multiline)
		if (err != nil) != tc.bad || got != tc.want {
			t.Errorf("CommentText(%q, %v) = %q, %v", tc.text, tc.multiline, got, err)
		}
	}
}

func TestValue_CommentsAreNoContent(t *testing.T) {
	var with, without Value
	if err := json.Unmarshal([]byte(`{"map":[["a",{"str":"1"},{"head":"h","line":"l"}]]}`), &with); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(`{"map":[["a",{"str":"1"}]]}`), &without); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(with.Content(), without.Content()) {
		t.Fatalf("content differs: %v vs %v", with.Content(), without.Content())
	}
	n := with.Node(false)
	if n.Content[0].HeadComment != "# h" || n.Content[0].LineComment != "# l" {
		t.Fatalf("key comments %q %q", n.Content[0].HeadComment, n.Content[0].LineComment)
	}
}

// A sequence item's comments (#3206): the pair [<value>, {comments}], the
// map entry's third element without the key; never content.
func TestValue_ItemComments(t *testing.T) {
	var with, without Value
	if err := json.Unmarshal([]byte(`{"seq":[{"str":"a"},[{"str":"b"},{"head":"h1\nh2","line":"l"}],[{"map":[]},{"line":"e"}]]}`), &with); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(`{"seq":[{"str":"a"},{"str":"b"},{"map":[]}]}`), &without); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(with.Content(), without.Content()) {
		t.Fatalf("content differs: %v vs %v", with.Content(), without.Content())
	}
	out, err := Encode(NewDoc(Value{Type: TypeMap, Entries: []Entry{{Key: "k", Value: with}}}.Node(false)).Node())
	if err != nil {
		t.Fatal(err)
	}
	if want := "k:\n  - \"a\"\n  # h1\n  # h2\n  - \"b\" # l\n  - {} # e\n"; string(out) != want {
		t.Fatalf("written\n%s\nwant\n%s", out, want)
	}
}

func TestValue_ItemCommentsRefused(t *testing.T) {
	for name, in := range map[string]string{
		"a pair without comments":        `{"seq":[[{"str":"a"}]]}`,
		"a third element":                `{"seq":[[{"str":"a"},{},{}]]}`,
		"comments that are no object":    `{"seq":[[{"str":"a"},"h"]]}`,
		"an unknown comment member":      `{"seq":[[{"str":"a"},{"foot":"x"}]]}`,
		"a line break in a line comment": `{"seq":[[{"str":"a"},{"line":"x\ny"}]]}`,
		"an array where the value goes":  `{"seq":[[[{"str":"a"}],{}]]}`,
		"a line after a block mapping":   `{"seq":[[{"map":[["a",{"int":"1"}]]},{"line":"l"}]]}`,
		"a line after a block sequence":  `{"seq":[[{"seq":[{"int":"1"}]},{"line":"l"}]]}`,
	} {
		var v Value
		if err := json.Unmarshal([]byte(in), &v); err == nil {
			t.Errorf("%s: %s accepted", name, in)
		}
	}
}

// TestItemLineCommentAfterABlockIsMisplaced pins why a line comment is
// refused after a non-empty mapping or sequence item: yaml.v3 writes it on
// the next item's line, or (the last item) not at all. When it stops doing
// so, the refusal can go.
func TestItemLineCommentAfterABlockIsMisplaced(t *testing.T) {
	block := Value{Type: TypeMap, Entries: []Entry{{Key: "a", Value: Value{Type: TypeInt, Text: "1"}}}}
	for _, tc := range []struct {
		items []Item
		want  string
	}{
		{[]Item{{Value: block, Line: "# l"}, {Value: Value{Type: TypeStr, Text: "x"}}}, "k:\n  - a: 1\n  - \"x\" # l\n"},
		{[]Item{{Value: block, Line: "# l"}}, "k:\n  - a: 1\n"},
	} {
		v := Value{Type: TypeMap, Entries: []Entry{{Key: "k", Value: Value{Type: TypeSeq, Items: tc.items}}}}
		out, err := Encode(NewDoc(v.Node(false)).Node())
		if err != nil || string(out) != tc.want {
			t.Errorf("written %q (%v), want %q", out, err, tc.want)
		}
	}
}
