package parsetree

// Judge is Parse's verdict without the content (pkg/config asks it before
// every decode of a conf.d file, ADR-036 step 6): over every corpus row and
// the shapes Judge takes a shortcut on — no alias, so no whole-document
// decode; no content built, so the byte bound counted on the same walk — it
// refuses exactly what Parse refuses, with the same kind and message.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"unicode/utf8"
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

// The byte bound on plain strings (no tag, resolved !!str): Parse and Judge
// both count their value from the text (marshalLen, scalarLenN), so the file
// whose content is exactly MaxContentBytes of JSON reads and the one a byte
// longer is refused, alike, for text whose JSON escapes differ from its
// bytes — `<`, `>`, `&` (json.Marshal's HTML escape in the value) and a
// newline (`\n` in both). The boundary is found with Judge (cheap) and then
// held on Parse, one byte at a time either side.
func TestPlainStringByteBoundary(t *testing.T) {
	t.Parallel()
	for _, payload := range []string{"<", "&>", `\n<`} {
		build := func(k, n int) []byte {
			return []byte(fmt.Sprintf("k: \"%s\"\ntail: \"%s\"\n", strings.Repeat(payload, k), strings.Repeat("t", n)))
		}
		least := func(f func(int) bool, hi int) int { // the least x in [0, hi] with f(x)
			lo := 0
			for lo < hi {
				m := (lo + hi) / 2
				if f(m) {
					hi = m
				} else {
					lo = m + 1
				}
			}
			return lo
		}
		refused := func(data []byte) bool { return Judge(data) != nil }
		k := least(func(k int) bool { return refused(build(k, 0)) }, MaxContentBytes) - 1
		room := least(func(n int) bool { return refused(build(k, n)) }, MaxContentBytes) - 1
		if k < 1 || room < 0 {
			t.Fatalf("%q: no boundary found (k=%d room=%d)", payload, k, room)
		}
		// The independent half: the content of the last file that reads,
		// written as `da-guard parse-tree` writes it, is exactly the bound.
		content, bad := Parse(build(k, room))
		if bad != nil {
			t.Fatalf("%q k=%d tail=%d: %v", payload, k, room, bad)
		}
		var buf bytes.Buffer
		enc := json.NewEncoder(&buf)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(content); err != nil {
			t.Fatal(err)
		}
		if got := buf.Len() - 1; got != MaxContentBytes { // Encode adds a newline
			t.Errorf("%q: the last content that reads encodes to %d bytes, want exactly %d", payload, got, MaxContentBytes)
		}
		for _, n := range []int{room - 1, room, room + 1, room + 2} {
			if n < 0 {
				continue
			}
			data := build(k, n)
			_, bad := Parse(data)
			if wantRefused := n > room; (bad != nil) != wantRefused {
				t.Errorf("%q k=%d tail=%d: Parse refused=%v, want %v", payload, k, n, bad != nil, wantRefused)
			}
			if bad != nil && bad.Kind != KindDecodeError {
				t.Errorf("%q k=%d tail=%d: refused as %s, want the byte bound", payload, k, n, bad.Kind)
			}
			assertJudgeIsParse(t, fmt.Sprintf("%q k=%d tail=%d", payload, k, n), data)
		}
	}
}

// FuzzParseJudgeSameVerdict: on any bytes Judge and Parse give the same
// verdict. CI runs the seed corpus; `go test -fuzz FuzzParseJudgeSameVerdict`
// explores.
func FuzzParseJudgeSameVerdict(f *testing.F) {
	scalars := []string{"a", "1", "01", "0x1f", ".inf", "~", "null", "true", "yes", "<<", `"<<"`, `"\xff"`,
		`"a&b<>"`, "2001-13-45", "!!str 1", "!!int x", "!!binary aGk=", "! a", "!foo a", `"\t"`, `" "`,
		"| \n  x", "''", "&q v", "*q", "&a [1]", "[a, b]", "{p: 1}", "{<<: *b}", "? [x]", "=", "<", "&"}
	for _, s := range scalars {
		f.Add([]byte("a: " + s + "\n"))
		f.Add([]byte("a: &a x\nb:\n  <<: {c: " + s + "}\n  c: 1\n"))
	}
	f.Add([]byte("a: &a [x]\nb: *a\n"))
	f.Add([]byte("---\n~\n"))
	f.Add([]byte("tenants:\n  t1:\n    x: \"80\"\n---\n"))
	f.Fuzz(func(t *testing.T, data []byte) {
		_, want := Parse(data)
		got := Judge(data)
		if (want == nil) != (got == nil) || (want != nil && got.Kind != want.Kind) {
			t.Errorf("%q: Judge %v, Parse %v", data, got, want)
		}
	})
}

// A message is bounded where it is made (fail): a 1 MiB repeated key keeps
// its head (the line number, what was refused) and its tail (the line the key
// was first written on), stays valid UTF-8 — the cut lands inside multi-byte
// runes here — and is the same for Parse and Judge.
func TestMessageIsCutAtTheSourceKeepingHeadAndTail(t *testing.T) {
	t.Parallel()
	for _, unit := range []string{"k", "é", "😀"} {
		k := strings.Repeat(unit, (1<<20)/len(unit))
		data := []byte("x: 1\n? " + k + "\n: 1\n? " + k + "\n: 2\n")
		_, bad := Parse(data)
		if bad == nil || bad.Kind != KindDuplicateKey {
			t.Fatalf("%q: %v, want duplicate_key", unit, bad)
		}
		m := bad.Message
		if len(m) > MaxMessageBytes || !utf8.ValidString(m) {
			t.Errorf("%q: message %d bytes (bound %d), valid UTF-8 %v", unit, len(m), MaxMessageBytes, utf8.ValidString(m))
		}
		if !strings.HasPrefix(m, "line 4: the key ") || !strings.HasSuffix(m, "(first at line 2)") ||
			!strings.Contains(m, " bytes omitted) …") {
			t.Errorf("%q: head %.40q … tail %q", unit, m, m[len(m)-40:])
		}
		if j := Judge(data); j == nil || j.Message != m {
			t.Errorf("%q: Judge's message differs from Parse's", unit)
		}
	}
	short := "a: 1\na: 2\n"
	if _, bad := Parse([]byte(short)); bad == nil || strings.Contains(bad.Message, "omitted") {
		t.Errorf("a short message is cut: %v", bad)
	}
}

// TestKindsIsACopy: the closed list cannot be changed from outside the
// package — what Kinds returns is the caller's own slice (#3206).
func TestKindsIsACopy(t *testing.T) {
	t.Parallel()
	a := Kinds()
	want := append([]Kind(nil), a...)
	a[0] = "tampered"
	_ = append(a[:1], "appended")
	if got := Kinds(); !reflect.DeepEqual(got, want) {
		t.Fatalf("Kinds changed after a caller wrote its copy: %v, want %v", got, want)
	}
}
