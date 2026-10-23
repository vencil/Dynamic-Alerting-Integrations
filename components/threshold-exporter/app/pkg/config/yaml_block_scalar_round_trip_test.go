package config

// #3206 — the vendored yaml.v3 writes a block scalar back so that it reads
// as the same string. Writers of conf.d (tenant-api's partial writes, its
// custom-alerts merge) re-encode the yaml.Node tree of a file and refuse the
// write when a node changed (ADR-036 §4), so what the encoder does here
// decides which files can be written at all.
//
// yaml.v3 v3.0.1's emitter left out the leading blank lines of a literal
// block (`"\nteam"` came back as `"team"`); go.yaml.in/yaml/v3 v3.0.5, the
// vendored base since #3206, writes them. Built against v3.0.1, the
// leading-blank cases below fail; a later re-vendor that brings the old
// behaviour back fails here, not in a customer's refused write.

import (
	"testing"

	"gopkg.in/yaml.v3"
)

// reEncode decodes doc into a node tree, encodes it, and decodes the result
// again, returning the value under key `v` before and after.
func reEncodeValue(t *testing.T, doc string) (before, after any, out string) {
	t.Helper()
	var n yaml.Node
	if err := yaml.Unmarshal([]byte(doc), &n); err != nil {
		t.Fatalf("unmarshal %q: %v", doc, err)
	}
	b, err := yaml.Marshal(&n)
	if err != nil {
		t.Fatalf("marshal %q: %v", doc, err)
	}
	var first, second map[string]any
	if err := yaml.Unmarshal([]byte(doc), &first); err != nil {
		t.Fatalf("decode %q: %v", doc, err)
	}
	if err := yaml.Unmarshal(b, &second); err != nil {
		t.Fatalf("decode re-encoded %q: %v", b, err)
	}
	return first["v"], second["v"], string(b)
}

func TestBlockScalarReEncodeKeepsTheString(t *testing.T) {
	for _, c := range []struct{ name, doc string }{
		{"literal leading blank", "v: |\n\n  team\n"},
		{"literal strip leading blank", "v: |-\n\n  team\n"},
		{"literal keep leading blank", "v: |+\n\n  team\n"},
		{"literal indent indicator leading blank", "v: |2\n\n  team\n"},
		{"literal two leading blanks", "v: |-\n\n\n  team\n"},
		{"literal header comment leading blank", "v: | # note\n\n  team\n"},
		{"literal header comment", "v: | # note\n  a\n  b\n"},
		{"nested literal leading blank", "x:\n  y:\n    v: |-\n\n      team\n"},
		{"literal plain", "v: |\n  a\n  b\n"},
		{"folded leading blank", "v: >\n\n  team\n"},
		{"double-quoted newlines", "v: \"\\nteam\\n\\nx\"\n"},
		{"double-quoted leading newline", "v: \"\\nteam\"\n"},
	} {
		t.Run(c.name, func(t *testing.T) {
			before, after, out := reEncodeValue(t, c.doc)
			if c.name == "nested literal leading blank" {
				// The value sits two mappings down; compare the whole tree.
				var a, b map[string]any
				_ = yaml.Unmarshal([]byte(c.doc), &a)
				_ = yaml.Unmarshal([]byte(out), &b)
				before = a["x"].(map[string]any)["y"].(map[string]any)["v"]
				after = b["x"].(map[string]any)["y"].(map[string]any)["v"]
			}
			if before != after {
				t.Errorf("re-encode changed the string: %q -> %q\nsource:\n%s\nre-encoded:\n%s", before, after, c.doc, out)
			}
		})
	}
}

// A string built in Go and written as a literal block (tenant-api's
// custom-alerts merge does this) reads back unchanged, leading line breaks
// included.
func TestLiteralStyleStringReadsBack(t *testing.T) {
	for _, s := range []string{"\nteam", "\n\nteam", "\n", "a\nb", "x\n\ny"} {
		out, err := yaml.Marshal(&yaml.Node{Kind: yaml.MappingNode, Tag: "!!map", Content: []*yaml.Node{
			{Kind: yaml.ScalarNode, Tag: "!!str", Value: "v"},
			{Kind: yaml.ScalarNode, Tag: "!!str", Value: s, Style: yaml.LiteralStyle}}})
		if err != nil {
			t.Fatalf("marshal %q: %v", s, err)
		}
		var back map[string]string
		if err := yaml.Unmarshal(out, &back); err != nil || back["v"] != s {
			t.Errorf("%q written as %q reads back as %q (err %v)", s, out, back["v"], err)
		}
	}
}
