package main

import (
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"

	"sigs.k8s.io/yaml"
)

// decoded runs the CLI on *in* and returns its one document (as written in
// stdout, keys in order) and its differences.
func decoded(t *testing.T, in string) (string, []map[string]interface{}) {
	t.Helper()
	rc, out, errOut := runOn(t, in)
	if rc != exitOK {
		t.Fatalf("%q: rc=%d stderr=%q", in, rc, errOut)
	}
	var got struct {
		Documents       []json.RawMessage          `json:"documents"`
		DiffersFromText [][]map[string]interface{} `json:"differs_from_text"`
	}
	if err := json.Unmarshal([]byte(out), &got); err != nil {
		t.Fatalf("%q: %v in %q", in, err, out)
	}
	if len(got.Documents) != 1 || len(got.DiffersFromText) != 1 {
		t.Fatalf("%q: want one document and one list of differences, got %s", in, out)
	}
	return string(got.Documents[0]), got.DiffersFromText[0]
}

func asJSON(t *testing.T, v interface{}) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// #3206 (ADR-036 step 11): where the conversion's reading is not the text,
// da-crdecode says so; the caller no longer reads the YAML itself to find out.
func TestReportsWhereTheReadingIsNotTheText(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name, in, want string
	}{
		{"octal-key", "spec:\n  tenants:\n    010: {}\n",
			`[{"kind":"keys","path":["spec","tenants"],"read_keys":["8"],"written_keys":["010"]}]`},
		{"bool-key", "t1: {yes: '1'}\n",
			`[{"kind":"keys","path":["t1"],"read_keys":["true"],"written_keys":["yes"]}]`},
		{"key-in-a-list", "a: [{1e3: '1'}]\n",
			`[{"kind":"keys","path":["a",0],"read_keys":["1000"],"written_keys":["1e3"]}]`},
		// Two written keys, one read key: nothing below is compared.
		{"two-keys-one-reading", "t: {8: a, 010: {x: !!binary b2s=}}\n",
			`[{"kind":"keys","path":["t"],"read_keys":[],"written_keys":["010"]}]`},
		{"binary-string", "metadata: {name: !!binary b2s=}\n",
			`[{"kind":"string","path":["metadata","name"],"text":"b2s="}]`},
		{"binary-key", "metadata: {!!binary bmFtZQ==: ok}\n",
			`[{"kind":"keys","path":["metadata"],"read_keys":["name"],"written_keys":["bmFtZQ=="]}]`},
		// Read as a number: the reading says so itself, not reported.
		{"number-value", "t1: {_profile: 010}\n", `[]`},
		{"quoted", "t1: {\"010\": '1', _profile: \"010\"}\n", `[]`},
		{"tagged-date", "metadata: {name: !!timestamp 2024-01-01}\n", `[]`},
		{"same-text-twice", "t1: {a: '1', a: '2'}\n", `[]`},
		{"custom-tag", "x: {cpu: !foo 1}\n", `[]`},
		{"omap-is-a-list-of-maps", "x: !!omap [a: '1']\n", `[]`},
		{"alias", "a: &a {010: x}\nb: *a\n",
			`[{"kind":"keys","path":["a"],"read_keys":["8"],"written_keys":["010"]},` +
				`{"kind":"keys","path":["b"],"read_keys":["8"],"written_keys":["010"]}]`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, differs := decoded(t, tc.in)
			if got := asJSON(t, differs); got != tc.want {
				t.Fatalf("%q: differs_from_text %s; want %s", tc.in, got, tc.want)
			}
		})
	}
}

// The document comes out with each object's keys in the order written.
func TestKeysKeepTheWrittenOrder(t *testing.T) {
	t.Parallel()
	for in, want := range map[string]string{
		"zz: {b: '1', a: '2'}\naa: {cpu: '1'}\n": `{"zz":{"b":"1","a":"2"},"aa":{"cpu":"1"}}`,
		// Matched keys keep their place; a key read but not written so follows.
		"z: 1\n010: 2\na: 3\n":          `{"z":1,"a":3,"8":2}`,
		"m: {<<: {w: 1, x: 2}, a: 3}\n": `{"m":{"w":1,"x":2,"a":3}}`,
		"- {b: 1, a: [{d: 1, c: 2}]}\n": `[{"b":1,"a":[{"d":1,"c":2}]}]`,
	} {
		doc, _ := decoded(t, in)
		if doc != want {
			t.Errorf("%q: document %s; want %s", in, doc, want)
		}
	}
}

// The written side applies `<<` the way go-yaml v2 does inside YAMLToJSON.
// Every value here is a string equal to its text, so ANY other merge order
// would pair a key with another value and report a "string" difference. The
// control row shows that a mispaired value is reported.
func TestMergeOrderIsTheConversions(t *testing.T) {
	t.Parallel()
	for _, in := range []string{
		"m: {x: a, <<: {x: b}}\n",                       // a merge after an explicit key replaces it
		"m: {<<: {x: b}, x: a}\n",                       // an explicit key after a merge replaces it
		"m: {<<: [{x: a}, {x: b, w: c}]}\n",             // in a merged list the earlier map wins
		"m: {<<: [{x: a}, {x: b}], x: c}\n",             // and an explicit key after it wins
		"b: &b {x: a, <<: {x: c}}\nm: {<<: *b, w: d}\n", // a merge inside a merge
		"m: {!!merge <<: {x: a}}\n",
		"m: {\"<<\": a}\n", // a quoted << is a key
		"metadata: {name: ok, <<: {name: other}}\n",
	} {
		_, differs := decoded(t, in)
		if len(differs) != 0 {
			t.Errorf("%q: differs_from_text %s; the merge order is not the conversion's", in, asJSON(t, differs))
		}
		var want map[string]interface{}
		if err := yaml.Unmarshal([]byte(in), &want); err != nil {
			t.Fatal(err)
		}
		doc, _ := decoded(t, in)
		var got map[string]interface{}
		if err := json.Unmarshal([]byte(doc), &got); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("%q: document %v; the conversion reads %v", in, got, want)
		}
	}
	// Control: a value that is read as other text is reported.
	if _, differs := decoded(t, "m: {x: !!binary YQ==}\n"); asJSON(t, differs) !=
		`[{"kind":"string","path":["m","x"],"text":"YQ=="}]` {
		t.Fatalf("control: differs_from_text %s", asJSON(t, differs))
	}
}

// A file the conversion reads but yaml.v3 cannot compose is refused (no input
// that the go-yaml v2 parser accepts and yaml.v3's refuses is known, so the
// branch is driven with one both refuse).
func TestNotReadableAsWrittenIsAnError(t *testing.T) {
	t.Parallel()
	_, _, err := compareAsWritten([]byte("a: [\n"), json.RawMessage(`{"a":[]}`))
	if !errors.Is(err, errNotAsWritten) || !strings.Contains(err.Error(), "cannot be read as written") {
		t.Fatalf("err = %v; want errNotAsWritten", err)
	}
}

// A shape the two readings do not share is reported, nothing below it.
func TestStructureThatCannotBePairedIsReported(t *testing.T) {
	t.Parallel()
	_, differs, err := compareAsWritten([]byte("a: {b: [x]}\n"), json.RawMessage(`{"a":{"b":{"c":"x"}}}`))
	if err != nil {
		t.Fatal(err)
	}
	if got := asJSON(t, differs); got != `[{"kind":"structure","path":["a","b"],"read":"a mapping","written":"a list of 1"}]` {
		t.Fatalf("differs_from_text %s", got)
	}
}
