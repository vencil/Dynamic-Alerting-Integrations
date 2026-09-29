package pyyamlcompat

import (
	"encoding/json"
	"math/big"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

type scalarRow struct {
	Text   string `json:"text"`
	PyYAML string `json:"pyyaml"`
}

// loadScalars reads testdata/pyyaml_plain_scalars.json: PyYAML SafeLoader's
// resolved tag for each candidate written as the plain value of `v: `, or
// "error" when PyYAML cannot compose it
// (tests/shared/test_receiver_spec_parity.py generates and pins it,
// REGEN_PYYAML_SCALARS=1).
func loadScalars(t *testing.T) []scalarRow {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", "pyyaml_plain_scalars.json"))
	if err != nil {
		t.Fatalf("read table: %v", err)
	}
	var rows []scalarRow
	if err := json.Unmarshal(data, &rows); err != nil || len(rows) < 100 {
		t.Fatalf("parse table: %v (n=%d)", err, len(rows))
	}
	return rows
}

// valueNode parses "v: <text>" and returns the value node, or nil when
// yaml.v3 rejects the document.
func valueNode(t *testing.T, doc string) *yaml.Node {
	t.Helper()
	var n yaml.Node
	if yaml.Unmarshal([]byte(doc), &n) != nil {
		return nil
	}
	if len(n.Content) != 1 || n.Content[0].Kind != yaml.MappingNode || len(n.Content[0].Content) != 2 {
		t.Fatalf("%q: not a one-key mapping", doc)
	}
	return n.Content[0].Content[1]
}

// TestDecode_MatchesPyYAML holds Decode to PyYAML's verdict on every
// candidate, in both directions: the value is a string exactly when PyYAML
// resolves the plain scalar to str. For a plain scalar the resolved tag must
// match too.
func TestDecode_MatchesPyYAML(t *testing.T) {
	rows := loadScalars(t)
	compared := 0
	for _, r := range rows {
		n := valueNode(t, "v: "+r.Text)
		if n == nil || r.PyYAML == "error" {
			// One of the two parsers fails the document: a loud failure on
			// that side, not a value the other reads differently.
			continue
		}
		compared++
		if n.Kind == yaml.ScalarNode && n.Style == 0 {
			if got := Resolve(n.Value); got != r.PyYAML {
				t.Errorf("%q: Resolve = %s, PyYAML = %s", r.Text, got, r.PyYAML)
			}
		}
		_, isString := Decode(n).(string)
		if isString != (r.PyYAML == TagStr) {
			t.Errorf("%q: Decode gives %T (%v), PyYAML resolves %s", r.Text, Decode(n), Decode(n), r.PyYAML)
		}
	}
	if compared < 250 {
		t.Fatalf("only %d of %d rows compared; the table no longer exercises Decode", compared, len(rows))
	}
}

// TestScalarTable_CoversEveryTag: the table exercises every tag Resolve can
// return (the Python side asserts the same against PyYAML's resolver list).
func TestScalarTable_CoversEveryTag(t *testing.T) {
	seen := map[string]bool{}
	for _, r := range loadScalars(t) {
		seen[r.PyYAML] = true
	}
	for _, r := range implicitResolvers {
		if !seen[r.tag] {
			t.Errorf("no row resolves to %s", r.tag)
		}
	}
}

// TestDecode_NonPlainIsString: the same candidates quoted, as a block
// literal or tagged !!str are strings with their text — what PyYAML reads.
func TestDecode_NonPlainIsString(t *testing.T) {
	for _, r := range loadScalars(t) {
		dq, _ := json.Marshal(r.Text)
		forms := map[string]string{
			"double-quoted": "v: " + string(dq),
			"single-quoted": "v: '" + strings.ReplaceAll(r.Text, "'", "''") + "'",
			"literal":       "v: |-\n  " + r.Text + "\n",
			"!!str":         "v: !!str " + string(dq),
		}
		for form, doc := range forms {
			n := valueNode(t, doc)
			if n == nil {
				t.Errorf("%s %q: yaml.v3 rejects %q", form, r.Text, doc)
				continue
			}
			if got := Decode(n); got != r.Text {
				t.Errorf("%s %q: Decode = %T %v, want the string", form, r.Text, got, got)
			}
		}
	}
}

func decodeDoc(t *testing.T, doc string) any {
	t.Helper()
	var n yaml.Node
	if err := yaml.Unmarshal([]byte(doc), &n); err != nil {
		t.Fatalf("%q: %v", doc, err)
	}
	return Decode(&n)
}

// TestDecode_Structures pins collections, aliases, merge keys and explicit
// tags to what yaml.safe_load returns for the same text (PyYAML 6.0.3,
// measured; the Python value is in each comment).
func TestDecode_Structures(t *testing.T) {
	big66, _ := new(big.Int).SetString("73786976294838206464", 10)
	cases := []struct {
		doc  string
		want any
	}{
		// {'v': {'a': 2}}
		{"v: {<<: {a: 1}, a: 2}", map[string]any{"v": map[string]any{"a": 2}}},
		// {'a': {'k': 1, 'j': 2}, 'b': {'k': 3, 'j': 9}} — earlier source wins, own key wins
		{"a: &x {k: 1, j: 2}\nb: {<<: [{k: 3}, *x], j: 9}", map[string]any{
			"a": map[string]any{"k": 1, "j": 2}, "b": map[string]any{"k": 3, "j": 9}}},
		// {'v': '<<'}
		{`v: "<<"`, map[string]any{"v": "<<"}},
		// {'=': 1}
		{"=: 1", map[string]any{"=": 1}},
		// {'a': True, 'b': 'on', 'c': True}
		{"a: &o on\nb: 'on'\nc: *o", map[string]any{"a": true, "b": "on", "c": true}},
		// {'v': 83}, {'v': True}, {'v': None}
		{`v: !!int "0123"`, map[string]any{"v": 83}},
		{`v: !!bool "yes"`, map[string]any{"v": true}},
		{"v: !!null abc", map[string]any{"v": nil}},
		// {True: 1, 'x': [1, 1.5, None, '1']}
		{"on: 1\nx: [1, 1.5, ~, '1']", map[any]any{true: 1, "x": []any{1, 1.5, nil, "1"}}},
		// {'v': 5430, 'w': -83, 'x': 73786976294838206464, 'y': 5}
		{"v: 1:30:30\nw: -0123\nx: 0x4_0000_0000_0000_0000\ny: 0b101", map[string]any{
			"v": 5430, "w": -83, "x": big66, "y": 5}},
		// {'v': datetime.date(2024, 1, 1), 'w': datetime.datetime(2001, 12, 14, 21, 59, 43, 100000, tzinfo=-05:00)}
		{"v: 2024-01-01\nw: 2001-12-14t21:59:43.10-05:00", map[string]any{
			"v": time.Date(2024, 1, 1, 0, 0, 0, 0, time.UTC),
			"w": time.Date(2001, 12, 14, 21, 59, 43, 100000000, time.FixedZone("", -5*3600))}},
	}
	for _, tc := range cases {
		got := decodeDoc(t, tc.doc)
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%q: Decode = %#v, want %#v", tc.doc, got, tc.want)
		}
	}
}

// TestDecode_NotAString: what PyYAML fails on or builds as a type not
// modelled here is never a string.
func TestDecode_NotAString(t *testing.T) {
	for _, doc := range []string{
		"v: =",             // no constructor for tag:yaml.org,2002:value
		"v: <<",            // nor for merge, as a value
		"v: 2024-13-01",    // resolved a timestamp, month out of range
		`v: !!bool "y"`,    // bool_values has no 'y'
		"v: !!binary aGk=", // bytes
		"v: !foo x",        // unknown tag
		"v: {<<: 5}",       // merge source not a mapping
		"v: !!set {a, b}",  // set
		"v: {[1]: x}",      // unhashable key
	} {
		v := decodeDoc(t, doc).(map[string]any)["v"]
		if _, isString := v.(string); isString {
			t.Errorf("%q: Decode gives the string %q", doc, v)
		}
	}
}
