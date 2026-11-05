package main

import (
	"bytes"
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// #3556: key-spellings prints config.SpellingsOf for each key, in input
// order — the retired spelling taken from the alias table, so this file need
// not spell it.
func TestKeySpellings_EveryShapeInOrder(t *testing.T) {
	t.Parallel()
	var legacy, canon string
	for l, c := range config.DeprecatedKeyAliases() {
		legacy, canon = l, c
	}
	if legacy == "" {
		t.Skip("no #1231 alias left")
	}
	keys := []string{
		legacy, canon + "_critical", legacy + `{db="a"}`,
		legacy + `_critical{db="a"}`, // #3556: both derived shapes at once
		"x", "_state_" + legacy, legacy + "_util",
	}
	want := [][]string{
		{canon, legacy}, {canon + "_critical", legacy + "_critical"}, {canon + `{db="a"}`, legacy + `{db="a"}`},
		{canon + `_critical{db="a"}`, legacy + `_critical{db="a"}`},
		{"x"}, {"_state_" + legacy}, {legacy + "_util"},
	}
	raw, _ := json.Marshal(keys)
	var out, errOut bytes.Buffer
	if code := runKeySpellings(nil, bytes.NewReader(raw), &out, &errOut); code != exitOK {
		t.Fatalf("exit %d, stderr %q", code, errOut.String())
	}
	var got []keySpellingOut
	if err := json.Unmarshal(out.Bytes(), &got); err != nil || len(got) != len(keys) {
		t.Fatalf("stdout %s (%v)", out.String(), err)
	}
	for i, g := range got {
		if g.Key != keys[i] || g.Canonical != want[i][0] || !reflect.DeepEqual(g.Spellings, want[i]) {
			t.Errorf("answer %d = %+v, want key %q canonical %q spellings %q", i, g, keys[i], want[i][0], want[i])
		}
	}
}

func TestKeySpellings_RefusesWhatIsNotAKeyList(t *testing.T) {
	t.Parallel()
	for _, in := range []string{``, `null`, `{}`, `["a", 1]`, `["a", null]`, `["a"] []`, `[`, "[\"\xff\"]"} {
		var out, errOut bytes.Buffer
		if code := runKeySpellings(nil, strings.NewReader(in), &out, &errOut); code != exitCallerErr || out.Len() != 0 {
			t.Errorf("stdin %q: exit %d, stdout %q; want exit %d and no output", in, code, out.String(), exitCallerErr)
		}
	}
	var out, errOut bytes.Buffer
	if code := runKeySpellings([]string{"x"}, strings.NewReader(`[]`), &out, &errOut); code != exitCallerErr {
		t.Errorf("an argument: exit %d, want %d", code, exitCallerErr)
	}
	out.Reset()
	if code := runKeySpellings(nil, strings.NewReader(`[]`), &out, &errOut); code != exitOK || strings.TrimSpace(out.String()) != "[]" {
		t.Errorf("empty list: exit %d, stdout %q", code, out.String())
	}
}
