package confedit

// A map entry's comments (#3206): written on the key as `# …` lines, never
// content — the value's Content is the same with or without them.

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
