package confedit

import (
	"strings"
	"testing"
)

// TestVerify_EachNodeFieldIsCompared: one pair per field the invariant
// compares, where that field is the only difference the other checks cannot
// see — each fails if its comparison is dropped.
func TestVerify_EachNodeFieldIsCompared(t *testing.T) {
	setA := []Edit{{Path: []string{"tenants", "t1", "a"}, Op: OpSet}}
	cases := []struct {
		name, base, merged string
		edits              []Edit
		want               DiffKind
	}{
		// Same style (tagged, double-quoted) and text; only the tag differs.
		{"the written tag", "tenants:\n  t1:\n    a: \"1\"\n    b: !!str \"1\"\n",
			"tenants:\n  t1:\n    a: \"2\"\n    b: !!int \"1\"\n", setA, DiffNode},
		// The file defines the same anchors in the same order; one moved
		// to another node.
		{"the anchor name on a node", "x: &a 1\ny: 2\ntenants:\n  t1:\n    a: \"1\"\n",
			"x: 1\ny: &a 2\ntenants:\n  t1:\n    a: \"2\"\n", setA, DiffNode},
		// The alias is written the same; its target — inside the edit, its
		// anchor kept — is not.
		{"the alias target", "tenants:\n  t1:\n    a: &v \"1\"\nz: *v\n",
			"tenants:\n  t1:\n    a: &v \"2\"\nz: *v\n", setA, DiffNode},
		// The set key is written a second time, once as an alias key.
		{"a set key also there as an alias key", "k: &k a\ntenants:\n  t1:\n    *k : \"1\"\n",
			"k: &k a\ntenants:\n  t1:\n    *k : \"1\"\n    a: \"2\"\n", setA, DiffEditKey},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := Verify([]byte(tc.base), []byte(tc.merged), tc.edits)
			if d == nil || d.Kind != tc.want {
				t.Fatalf("Verify = %v, want %s", d, tc.want)
			}
		})
	}
}

// TestDoc_UnsetTheOnlyKey: the mapping an unset empties is part of the edit
// — written `{}` — and is accepted; a style change of any other mapping is
// still refused.
func TestDoc_UnsetTheOnlyKey(t *testing.T) {
	const base = "tenants:\n  t1:\n    _routing:\n      group_wait: \"30s\"\n  t2:\n    a: \"1\"\n"
	d := mustDoc(t, base)
	if _, err := d.Unset([]string{"tenants", "t1", "_routing", "group_wait"}); err != nil {
		t.Fatal(err)
	}
	got := encode(t, d)
	if want := "tenants:\n  t1:\n    _routing: {}\n  t2:\n    a: \"1\"\n"; got != want {
		t.Fatalf("got\n%s", got)
	}
	if diff := Verify([]byte(base), []byte(got), d.Edits()); diff != nil {
		t.Fatalf("Verify refused emptying the unset's own mapping: %s", diff)
	}
	other := strings.Replace(got, "  t2:\n    a: \"1\"\n", "  t2: {a: \"1\"}\n", 1)
	if diff := Verify([]byte(base), []byte(other), d.Edits()); diff == nil || diff.Kind != DiffNode {
		t.Fatalf("another mapping's style: Verify = %v", diff)
	}
	// Not emptied: an unset's mapping that still holds keys keeps its style.
	const two = "tenants:\n  t1:\n    a: \"1\"\n    b: \"1\"\n"
	unsetA := []Edit{{Path: []string{"tenants", "t1", "a"}, Op: OpUnset}}
	if diff := Verify([]byte(two), []byte("tenants:\n  t1: {b: \"1\"}\n"), unsetA); diff == nil || diff.Kind != DiffNode {
		t.Fatalf("a non-empty unset mapping turned flow: Verify = %v", diff)
	}
	// Emptied, but not by an unset of its own keys: refused.
	if diff := Verify([]byte(base), []byte(got), []Edit{{Path: []string{"tenants", "t1", "_routing"}, Op: OpEnsure}}); diff == nil {
		t.Fatal("an emptied mapping no unset names: accepted")
	}
}

// TestDoc_CommentsStayWithScalarsOnly: a replaced scalar keeps the comment
// on its line; a scalar replaced by a mapping does not hand it to the
// mapping's first key.
func TestDoc_CommentsStayWithScalarsOnly(t *testing.T) {
	const base = "a:\n  k: \"1\" # about k\n  j: \"1\"\n"
	d := mustDoc(t, base)
	if err := d.Set([]string{"a", "k"}, StringNode("2")); err != nil {
		t.Fatal(err)
	}
	if got := encode(t, d); got != "a:\n  k: \"2\" # about k\n  j: \"1\"\n" {
		t.Fatalf("scalar over scalar:\n%s", got)
	}
	var v Value
	if err := v.UnmarshalJSON([]byte(`{"map": [["x", {"str": "1"}]]}`)); err != nil {
		t.Fatal(err)
	}
	d = mustDoc(t, base)
	if err := d.Set([]string{"a", "k"}, v.Node(false)); err != nil {
		t.Fatal(err)
	}
	if got := encode(t, d); strings.Contains(got, "about k") {
		t.Fatalf("the scalar's comment moved onto the mapping:\n%s", got)
	}
}

func TestKeepFramingAndSameTree(t *testing.T) {
	crlf := "\uFEFFa: \"1\"\r\nb: \"2\"\r\n"
	if got := string(KeepFraming([]byte(crlf), []byte("a: \"1\"\nb: \"3\"\n"))); got != "\uFEFFa: \"1\"\r\nb: \"3\"\r\n" {
		t.Fatalf("CRLF and BOM: %q", got)
	}
	if got := string(KeepFraming([]byte("a: 1\n"), []byte("a: 2\n"))); got != "a: 2\n" {
		t.Fatalf("LF: %q", got)
	}
	if !SameTree([]byte("a:    \"1\"   # c\nb:\n    - 1\n"), []byte("a: \"1\" # c\nb:\n  - 1\n")) {
		t.Fatal("a layout-only difference is not the same tree")
	}
	for _, other := range []string{"a: '1' # c\nb:\n  - 1\n", "a: \"1\" # d\nb:\n  - 1\n", "a: \"1\" # c\nb: [1]\n", "a: \"1\" # c\nb:\n  - 2\n"} {
		if SameTree([]byte("a: \"1\" # c\nb:\n  - 1\n"), []byte(other)) {
			t.Errorf("%q is not the same tree", other)
		}
	}
}
