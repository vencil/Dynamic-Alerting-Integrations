package confedit

// A set of the whole top level (an empty path, #3206 step 10c): the writer
// of a generated file sends the file in full. Nothing of the old top level
// is kept, Verify has nothing outside the edit to compare (an anchor the old
// file defined may go), and the edits after it are still read back.

import (
	"testing"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

func TestDoc_SetTheWholeTopLevel(t *testing.T) {
	const base = "# old\nold: &a {x: \"1\"}\nkeep: *a\n"
	d := mustDoc(t, base)
	root := &yaml.Node{Kind: yaml.MappingNode, Tag: "!!map", Content: []*yaml.Node{
		KeyNode("tenants"), {Kind: yaml.MappingNode, Tag: "!!map"}}}
	if err := d.Set([]string{}, root); err != nil {
		t.Fatal(err)
	}
	if err := d.Set([]string{"tenants", "t1"}, StringNode("v")); err != nil {
		t.Fatal(err)
	}
	out := encode(t, d)
	if want := "tenants:\n  t1: \"v\"\n"; out != want {
		t.Fatalf("got %q, want %q", out, want)
	}
	if diff := Verify([]byte(base), []byte(out), d.Edits()); diff != nil {
		t.Fatalf("Verify: %s", diff)
	}
	// An edit after the whole set is still read back: one the text does not
	// carry is refused.
	if diff := Verify([]byte(base), []byte("tenants: {}\n"), d.Edits()); diff == nil || diff.Kind != DiffEditKey {
		t.Fatalf("Verify of a text without the later edit: %v", diff)
	}
	// Only a mapping is a top level.
	if err := mustDoc(t, base).Set(nil, StringNode("x")); err == nil {
		t.Fatal("a scalar top level: no error")
	}
}

func TestContentSet_TheWholeTopLevel(t *testing.T) {
	c := parsetree.Content{"old": "x", "keep": map[string]any{"a": "1"}}
	if err := ContentSet(c, nil, map[string]any{"tenants": map[string]any{}}); err != nil {
		t.Fatal(err)
	}
	if p, w, g, ok := ContentDifference(map[string]any{"tenants": map[string]any{}}, c); !ok {
		t.Fatalf("at %v: want %v, got %v", p, w, g)
	}
	if err := ContentSet(c, nil, "x"); err == nil {
		t.Fatal("a scalar top level: no error")
	}
}
