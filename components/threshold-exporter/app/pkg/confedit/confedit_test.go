package confedit

import (
	"encoding/json"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

// A file in the conf.d convention (2-space indent, one space before a
// trailing comment), under which a re-encode writes every unedited line back
// byte for byte.
const sample = `# platform header
tenants:
  t1:
    # the connection cap
    mysql_connections: "80" # raised in 2026
    redis_memory: "1"
  t2:
    x: 010 # an int as written
    flow: {a: "1", b: [1, 2]}
    block: |
      line one
      line two
# foot of the file
`

func mustDoc(t *testing.T, s string) *Doc {
	t.Helper()
	d, err := ParseDoc([]byte(s))
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func encode(t *testing.T, d *Doc) string {
	t.Helper()
	out, err := d.Bytes()
	if err != nil {
		t.Fatal(err)
	}
	return string(out)
}

// TestDoc_EachOpKeepsEverythingElse: for each primitive, the result is the
// file with only the edited lines changed — every other node and every
// comment byte for byte — and Verify accepts it.
func TestDoc_EachOpKeepsEverythingElse(t *testing.T) {
	cases := []struct {
		name string
		edit func(d *Doc) error
		want string
	}{
		{"set existing key", func(d *Doc) error {
			return d.Set([]string{"tenants", "t1", "mysql_connections"}, StringNode("90"))
		}, strings.Replace(sample, `mysql_connections: "80" # raised`, `mysql_connections: "90" # raised`, 1)},
		{"set new key", func(d *Doc) error {
			return d.Set([]string{"tenants", "t1", "pg_conn"}, StringNode("5"))
		}, strings.Replace(sample, "    redis_memory: \"1\"\n", "    redis_memory: \"1\"\n    pg_conn: \"5\"\n", 1)},
		{"unset", func(d *Doc) error {
			_, err := d.Unset([]string{"tenants", "t1", "redis_memory"})
			return err
		}, strings.Replace(sample, "    redis_memory: \"1\"\n", "", 1)},
		{"ensure-map creates", func(d *Doc) error {
			if err := d.EnsureMap([]string{"tenants", "t1", "_routing"}); err != nil {
				return err
			}
			return d.Set([]string{"tenants", "t1", "_routing", "group_wait"}, StringNode("30s"))
		}, strings.Replace(sample, "    redis_memory: \"1\"\n", "    redis_memory: \"1\"\n    _routing:\n      group_wait: \"30s\"\n", 1)},
		{"ensure-map existing is a no-op", func(d *Doc) error {
			return d.EnsureMap([]string{"tenants", "t2"})
		}, sample},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := mustDoc(t, sample)
			if err := tc.edit(d); err != nil {
				t.Fatal(err)
			}
			got := encode(t, d)
			if got != tc.want {
				t.Fatalf("result is not the file with only the edit applied:\n--- got\n%s--- want\n%s", got, tc.want)
			}
			if diff := Verify([]byte(sample), []byte(got), d.Edits()); diff != nil {
				t.Fatalf("Verify refused a clean edit: %s", diff)
			}
		})
	}
}

func TestDoc_RefusesWhatItCannotEdit(t *testing.T) {
	d := mustDoc(t, "a: &x {k: \"1\"}\nb: *x\ns: \"1\"\n")
	for _, tc := range []struct {
		name string
		err  error
	}{
		{"set below a scalar", d.Set([]string{"s", "k"}, StringNode("1"))},
		{"ensure through an alias", d.EnsureMap([]string{"b"})},
		{"set under a missing parent", d.Set([]string{"nope", "k"}, StringNode("1"))},
		{"set the merge key", d.Set([]string{"<<"}, StringNode("1"))},
	} {
		if tc.err == nil {
			t.Errorf("%s: no error", tc.name)
		}
	}
	if removed, err := d.Unset([]string{"nope", "k"}); err != nil || removed {
		t.Errorf("unset under a missing parent: removed=%v err=%v, want a no-op", removed, err)
	}
	for _, s := range []string{"# only a comment\n", "a: 1\n---\nb: 2\n", "- 1\n"} {
		if _, err := ParseDoc([]byte(s)); err == nil {
			t.Errorf("ParseDoc(%q): no error", s)
		}
	}
}

// TestVerify_RefusesAnyChangeOutsideTheEdit holds the invariant's checks one
// by one: each merged text differs from the base only where it names.
func TestVerify_RefusesAnyChangeOutsideTheEdit(t *testing.T) {
	setA := []Edit{{Path: []string{"tenants", "t1", "a"}, Op: OpSet}}
	cases := []struct {
		name, base, merged string
		edits              []Edit
		want               DiffKind
	}{
		{"style of another node", "tenants:\n  t1:\n    a: \"1\"\n  t2:\n    k: 1\n",
			"tenants:\n  t1:\n    a: \"2\"\n  t2:\n    k: '1'\n", setA, DiffNode},
		{"text of another node", "tenants:\n  t1:\n    a: \"1\"\n    b: \"1\"\n",
			"tenants:\n  t1:\n    a: \"2\"\n    b: \"2\"\n", setA, DiffNode},
		{"tag as written", "tenants:\n  t1:\n    a: \"1\"\n    b: !!str 1\n",
			"tenants:\n  t1:\n    a: \"2\"\n    b: \"1\"\n", setA, DiffNode},
		{"an added key outside", "tenants:\n  t1:\n    a: \"1\"\n",
			"tenants:\n  t1:\n    a: \"2\"\n  t2: {}\n", setA, DiffNode},
		{"document count", "tenants:\n  t1:\n    a: \"1\"\n---\nx: 1\n",
			"tenants:\n  t1:\n    a: \"2\"\n", setA, DiffDocumentCount},
		{"anchors", "x: &a 1\ntenants:\n  t1:\n    a: \"1\"\n",
			"x: 1\ntenants:\n  t1:\n    a: \"2\"\n", setA, DiffAnchors},
		{"anchor removed with an edited value", "tenants:\n  t1:\n    a: &v \"1\"\n",
			"tenants:\n  t1:\n    a: \"2\"\n", setA, DiffAnchors},
		{"alias target re-pointed", "x: &a 1\ny: &a 2\nz: *a\ntenants:\n  t1:\n    a: \"1\"\n",
			"x: &a 1\ny: &a 3\nz: *a\ntenants:\n  t1:\n    a: \"2\"\n", setA, DiffNode},
		{"a merge key still supplies an unset key", "b: &b {a: \"1\"}\ntenants:\n  t1:\n    <<: *b\n    a: \"2\"\n",
			"b: &b {a: \"1\"}\ntenants:\n  t1:\n    <<: *b\n", []Edit{{Path: []string{"tenants", "t1", "a"}, Op: OpUnset}}, DiffEditKey},
		{"a set key not there", "tenants:\n  t1:\n    a: \"1\"\n    b: \"1\"\n",
			"tenants:\n  t1:\n    b: \"1\"\n", setA, DiffEditKey},
		{"a checked value not double-quoted", "tenants:\n  t1:\n    a: \"1\"\n",
			"tenants:\n  t1:\n    a: '2'\n", []Edit{{Path: []string{"tenants", "t1", "a"}, Op: OpSet, Value: strp("2")}}, DiffEditKey},
		{"an ensured key the base holds as an alias key", "k: &k t1\ntenants:\n  *k : {a: \"1\"}\n",
			"k: &k t1\ntenants:\n  *k : {a: \"1\"}\n  t1:\n    a: \"2\"\n",
			[]Edit{{Path: []string{"tenants", "t1"}, Op: OpEnsure}, {Path: []string{"tenants", "t1", "a"}, Op: OpSet}}, DiffNode},
		{"merged does not parse", "a: \"1\"\n", "a: [\n", nil, DiffMergedUnparseable},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := Verify([]byte(tc.base), []byte(tc.merged), tc.edits)
			if d == nil || d.Kind != tc.want {
				t.Fatalf("Verify = %v, want %s", d, tc.want)
			}
		})
	}
	// The same pairs with the edit's own change only pass.
	if d := Verify([]byte("tenants:\n  t1:\n    a: \"1\"\n"), []byte("tenants:\n  t1:\n    a: \"2\"\n"), setA); d != nil {
		t.Fatalf("Verify refused the edit itself: %s", d)
	}
	if d := Verify(nil, []byte("anything: 1\n"), nil); d != nil {
		t.Fatalf("an empty base has nothing to keep: %s", d)
	}
}

func strp(s string) *string { return &s }

func TestKeyNode(t *testing.T) {
	// Plain only for an identifier that no YAML 1.1 reader types: every
	// other key is double-quoted, whatever yaml.v3 alone would make of it.
	for k, plain := range map[string]bool{"t1": true, "mysql_connections": true, "db-a": true, "_routing": true,
		"010": false, "1:20": false, "2001-01-01": false, "no": false, "Yes": false, "ON": false, "y": false,
		"N": false, "off": false, "True": false, "NULL": false, "~": false, "null": false, "": false,
		"a: b": false, "#x": false, "-a": false, "a.b": false} {
		n := KeyNode(k)
		if got := n.Style == 0; got != plain {
			t.Errorf("KeyNode(%q) plain = %v, want %v", k, got, plain)
		}
		out, err := Encode(NewDoc(&yaml.Node{Kind: yaml.MappingNode, Content: []*yaml.Node{n, StringNode("v")}}).Node())
		if err != nil {
			t.Fatal(err)
		}
		c, bad := parsetree.Parse(out)
		if bad != nil {
			t.Fatalf("KeyNode(%q): %s does not read: %v", k, out, bad)
		}
		if _, ok := c[k]; !ok || len(c) != 1 {
			t.Errorf("KeyNode(%q) reads back as %v", k, c)
		}
	}
}

func TestBlockStringNode(t *testing.T) {
	for s, style := range map[string]yaml.Style{
		"a\nb\n": yaml.LiteralStyle, "one line": yaml.DoubleQuotedStyle, "\ta\nb": yaml.DoubleQuotedStyle,
	} {
		if got := BlockStringNode(s).Style; got != style {
			t.Errorf("BlockStringNode(%q).Style = %v, want %v", s, got, style)
		}
	}
	if StringNode("a\nb\n").Style != yaml.DoubleQuotedStyle {
		t.Error("a string outside _custom_alerts is double-quoted, line breaks or not")
	}
}

// TestValue_NodeAndContentAgree: what Node writes, parse-tree reads as
// Content — for every type, in a map whose order is the caller's.
func TestValue_NodeAndContentAgree(t *testing.T) {
	const in = `{"map": [["z", {"str": "yes"}], ["a", {"int": "-12"}], ["m", {"float": "1.50"}],
		["b", {"bool": false}], ["n", {"null": null}], ["010", {"str": "2001-12-14"}],
		["_custom_alerts", {"seq": [{"map": [["expr", {"str": "up\n== 0\n"}]]}]}],
		["s", {"seq": [{"str": "a\nb"}, {"map": []}]}]]}`
	var v Value
	if err := json.Unmarshal([]byte(in), &v); err != nil {
		t.Fatal(err)
	}
	out, err := Encode(NewDoc(v.Node(false)).Node())
	if err != nil {
		t.Fatal(err)
	}
	keys := []string{}
	for _, line := range strings.Split(string(out), "\n") {
		if line != "" && line[0] != ' ' {
			keys = append(keys, strings.SplitN(line, ":", 2)[0])
		}
	}
	if strings.Join(keys, ",") != `z,a,m,b,"n","010",_custom_alerts,s` {
		t.Errorf("keys not in the order given: %v\n%s", keys, out)
	}
	if !strings.Contains(string(out), "expr: |") || !strings.Contains(string(out), `- "a\nb"`) {
		t.Errorf("multi-line strings: a literal block under _custom_alerts only:\n%s", out)
	}
	got, bad := parsetree.Parse(out)
	if bad != nil {
		t.Fatalf("written text does not read: %v\n%s", bad, out)
	}
	if p, w, g, ok := ContentDifference(v.Content(), got); !ok {
		t.Fatalf("Content and parse-tree differ at %v: want %v, got %v\n%s", p, w, g, out)
	}
}

func TestValue_RefusesAmbiguousInput(t *testing.T) {
	for _, in := range []string{
		`{"str": "a", "int": "1"}`, `{}`, `"plain"`, `{"str": 1}`, `{"int": "010"}`, `{"int": "0x1F"}`,
		`{"int": "1e3"}`, `{"int": "-0"}`, `{"float": "1e3"}`, `{"float": ".5"}`, `{"bool": "true"}`,
		`{"null": 0}`, `{"map": {"a": {"str": "1"}}}`, `{"map": [["a", {"str": "1"}], ["a", {"str": "2"}]]}`,
		`{"map": [["<<", {"str": "1"}]]}`, `{"map": [["a"]]}`, `{"seq": {"str": "a"}}`, `{"date": "2001-01-01"}`,
		`{"int": "1234567890123456789"}`,
	} {
		var v Value
		if err := json.Unmarshal([]byte(in), &v); err == nil {
			t.Errorf("%s: accepted", in)
		}
	}
}

func TestContentEdits(t *testing.T) {
	c := parsetree.Content{"tenants": map[string]any{"t1": map[string]any{"a": "x"}}, "s": "x"}
	if err := ContentEnsureMap(c, []string{"tenants", "t2", "_routing"}); err != nil {
		t.Fatal(err)
	}
	if err := ContentSet(c, []string{"tenants", "t1", "b"}, "y"); err != nil {
		t.Fatal(err)
	}
	if err := ContentUnset(c, []string{"tenants", "t1", "a"}); err != nil {
		t.Fatal(err)
	}
	if err := ContentUnset(c, []string{"nope", "a"}); err != nil {
		t.Fatalf("unset under a missing parent is a no-op: %v", err)
	}
	if err := ContentSet(c, []string{"s", "k"}, "y"); err == nil {
		t.Fatal("set below a scalar: no error")
	}
	want := map[string]any{"tenants": map[string]any{"t1": map[string]any{"b": "y"},
		"t2": map[string]any{"_routing": map[string]any{}}}, "s": "x"}
	if p, w, g, ok := ContentDifference(want, c); !ok {
		t.Fatalf("at %v: want %v, got %v", p, w, g)
	}
	if p, _, _, ok := ContentDifference(map[string]any{"a": map[string]any{"b": "1"}}, map[string]any{"a": map[string]any{"b": "2"}}); ok || strings.Join(p, ".") != "a.b" {
		t.Fatalf("difference path = %v, ok = %v", p, ok)
	}
}
