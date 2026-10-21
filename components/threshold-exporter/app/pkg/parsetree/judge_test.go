package parsetree

// Judge is Parse's verdict without the content (pkg/config asks it before
// every decode of a conf.d file, ADR-036 step 6): over every corpus row and
// the shapes Judge takes a shortcut on — no alias, so no whole-document
// decode; no content built, so the byte bound counted on the same walk — it
// refuses exactly what Parse refuses, with the same kind and message.

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func assertJudgeIsParse(t *testing.T, where string, data []byte) {
	t.Helper()
	_, want := Parse(data)
	got := Judge(data)
	switch {
	case (want == nil) != (got == nil):
		t.Errorf("%s: Judge %v, Parse %v", where, got, want)
	case want != nil && *got != *want:
		t.Errorf("%s: Judge %+v, Parse %+v", where, *got, *want)
	}
}

func TestJudgeIsParsesVerdict(t *testing.T) {
	t.Parallel()
	c, docs, _ := loadCorpus(t)
	for _, r := range c.Rows {
		assertJudgeIsParse(t, "row "+r.Name, fileBytes(t, "row "+r.Name, r))
	}
	for _, r := range c.MergeKeyRows {
		src := docs[r.ID]
		r.Doc, r.Encoding = src.Doc, src.Encoding
		assertJudgeIsParse(t, "merge_key row "+r.ID, fileBytes(t, "merge_key row "+r.ID, r))
	}
	// The shortcuts' edges, written out.
	// Unique keys with null values: past MaxContentBytes of JSON with no
	// alias (measured: refused at line 166663 of 250,001).
	var b strings.Builder
	b.WriteString("m:\n")
	for i := 0; i < 250000; i++ {
		fmt.Fprintf(&b, "  k%d: ~\n", i)
	}
	big := b.String()
	if bad := Judge([]byte(big)); bad == nil || bad.Kind != KindDecodeError {
		t.Fatalf("the byte-bound row is not refused by the bound: %v", bad)
	}
	for name, doc := range map[string]string{
		"no alias, plain":                   "tenants:\n  t1:\n    x: \"80\"\n",
		"self-containing alias":             "a: &a [*a]\n",
		"alias expansion past the limit":    "a: &a [x, x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a, *a]\nc: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b, *b]\nd: &d [*c, *c, *c, *c, *c, *c, *c, *c, *c, *c]\ne: [*d, *d, *d, *d, *d, *d, *d, *d, *d, *d]\n",
		"inline merge, no alias":            "a:\n  <<: {x: 1}\n  x: 2\n",
		"bad UTF-8 escape in a merged-away": "a:\n  <<: {x: \"\\xff\"}\n  x: 2\n",
		"bad UTF-8 escape in a value":       "a: \"\\xff\"\n",
		"bad UTF-8 escape in a key":         "\"\\xff\": 1\n",
		"content past the byte bound":       big,
		"empty":                             "",
		"null document":                     "~\n",
	} {
		assertJudgeIsParse(t, name, []byte(doc))
	}
}

// marshalLen counts what json.Marshal writes for a string, rune by rune:
// every rune up to U+3000 and a sample above, alone and in text.
func TestMarshalLenIsJSONMarshal(t *testing.T) {
	t.Parallel()
	check := func(s string) {
		b, err := json.Marshal(s)
		if err != nil {
			t.Fatal(err)
		}
		if got := marshalLen(s); got != len(b) {
			t.Errorf("marshalLen(%q) = %d, json.Marshal writes %d (%s)", s, got, len(b), b)
		}
	}
	for r := rune(0); r < 0x3000; r++ {
		check(string(r))
		check("a" + string(r) + "b")
	}
	for _, s := range []string{"", "😀", "\U0010FFFF", "�", "<a href=\"x\">&amp;</a>", "line sep "} {
		check(s)
	}
}
