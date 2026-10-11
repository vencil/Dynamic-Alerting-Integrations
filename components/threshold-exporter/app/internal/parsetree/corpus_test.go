package parsetree

// Go half of tests/shared/parse_tree_corpus.json (#2897; Python half:
// tests/shared/test_parse_tree_corpus.py, which freezes today's route
// generator verdict on every row).
//
// ADR-036's acceptance condition, as a test: a file the generator refuses
// today under --strict, parse-tree must call unreadable — unless the row
// names a written exception. The other direction (parse-tree refusing a
// file the generator reads) is allowed: stricter, never looser.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"
	"unicode/utf16"
)

type corpusRow struct {
	Name      string `json:"name"`
	File      string `json:"file"`
	Doc       string `json:"doc"`
	Encoding  string `json:"encoding"`
	ID        string `json:"id"`
	Generator string `json:"generator"`
	Expect    string `json:"expect"`
	Exception string `json:"exception"`
}

type corpus struct {
	TextKinds    []string          `json:"text_kinds"`
	Exceptions   map[string]string `json:"exceptions"`
	Rows         []corpusRow       `json:"rows"`
	MergeKeyRows []corpusRow       `json:"merge_key_rows"`
}

func repoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		if fi, err := os.Stat(filepath.Join(dir, "tests", "shared")); err == nil && fi.IsDir() {
			if _, err := os.Stat(filepath.Join(dir, "Makefile")); err == nil {
				return dir
			}
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("repo root not found")
		}
		dir = parent
	}
}

func readJSON(t *testing.T, path string, v any) {
	t.Helper()
	raw, err := os.ReadFile(path) // #nosec G304 -- repo test fixture
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, v); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
}

// encode is test_merge_key_policy_corpus._encode: the file a row stands for.
func encode(t *testing.T, doc, encoding string) []byte {
	t.Helper()
	switch encoding {
	case "":
		return []byte(doc)
	case "latin-1":
		out := make([]byte, 0, len(doc))
		for _, r := range doc {
			if r > 0xFF {
				t.Fatalf("latin-1 row holds %q", r)
			}
			out = append(out, byte(r))
		}
		return out
	case "utf-16-le-bom", "utf-16-be-bom":
		le := encoding == "utf-16-le-bom"
		out := []byte{0xFE, 0xFF}
		if le {
			out = []byte{0xFF, 0xFE}
		}
		for _, u := range utf16.Encode([]rune(doc)) {
			if le {
				out = append(out, byte(u), byte(u>>8))
			} else {
				out = append(out, byte(u>>8), byte(u))
			}
		}
		return out
	}
	t.Fatalf("unknown encoding %q", encoding)
	return nil
}

func verdict(data []byte) string {
	if _, bad := Parse(data); bad != nil {
		return string(bad.Kind)
	}
	return "read"
}

// TestCorpusNotLooserThanTheGenerator is the ADR-036 §2 condition over
// every row of both corpora.
func TestCorpusNotLooserThanTheGenerator(t *testing.T) {
	t.Parallel()
	root := repoRoot(t)
	var c corpus
	readJSON(t, filepath.Join(root, "tests", "shared", "parse_tree_corpus.json"), &c)
	var mk struct {
		Rows []corpusRow `json:"rows"`
	}
	readJSON(t, filepath.Join(root, "tests", "shared", "merge_key_policy_corpus.json"), &mk)
	docs := make(map[string]corpusRow, len(mk.Rows))
	for _, r := range mk.Rows {
		docs[r.ID] = r
	}
	if len(c.MergeKeyRows) != len(mk.Rows) || len(c.MergeKeyRows) < 1000 {
		t.Fatalf("merge_key_rows: %d rows, merge_key_policy_corpus.json %d", len(c.MergeKeyRows), len(mk.Rows))
	}

	rejected, excused := 0, 0
	// handPicked: a row of `rows`, whose exception must still be needed. A
	// merge_key row's exception is assigned from the generator's own reason
	// (the Python half), so parse-tree may refuse such a row on another
	// ground (a TAB row that also writes `!!merge`) — that is no stale entry.
	check := func(where string, r corpusRow, data []byte, handPicked bool) {
		sum := sha256.Sum256(data)
		if id := hex.EncodeToString(sum[:])[:16]; id != r.ID {
			t.Fatalf("%s: the bytes hash to %s, the row says %s (encoding %q)", where, id, r.ID, r.Encoding)
		}
		got := verdict(data)
		if r.Exception != "" {
			if c.Exceptions[r.Exception] == "" {
				t.Errorf("%s: exception %q has no written reason", where, r.Exception)
			}
			if r.Generator != "rejected" || handPicked && got != "read" {
				t.Errorf("%s: carries exception %q, but the generator %s it and parse-tree gives %s — "+
					"an exception is only for a rejected row parse-tree reads; drop it", where, r.Exception, r.Generator, got)
			}
			excused++
			return
		}
		if r.Generator == "rejected" {
			rejected++
			if got == "read" {
				t.Errorf("%s: today's generator refuses this file, parse-tree reads it (looser; ADR-036 §2)", where)
			}
		}
	}
	for _, r := range c.Rows {
		data := encode(t, r.Doc, r.Encoding)
		check("row "+r.Name, r, data, true)
		if got := verdict(data); got != r.Expect {
			t.Errorf("row %s: parse-tree gives %s, the corpus expects %s", r.Name, got, r.Expect)
		}
	}
	for _, r := range c.MergeKeyRows {
		src, ok := docs[r.ID]
		if !ok {
			t.Fatalf("merge_key_rows %s: no such row in merge_key_policy_corpus.json", r.ID)
		}
		r.Doc, r.Encoding = src.Doc, src.Encoding
		check("merge_key row "+r.ID, r, encode(t, r.Doc, r.Encoding), false)
	}
	// Anti-vacuity: a corpus with nothing rejected proves nothing.
	if rejected < 100 || excused < 3 {
		t.Fatalf("only %d rejected rows and %d exceptions were checked", rejected, excused)
	}
}

// TestCorpusCoversEveryTextKind: every kind Parse can give a text has a row
// that expects it, and the Python half lists exactly those kinds.
func TestCorpusCoversEveryTextKind(t *testing.T) {
	t.Parallel()
	var c corpus
	readJSON(t, filepath.Join(repoRoot(t), "tests", "shared", "parse_tree_corpus.json"), &c)
	var want []string
	for _, k := range Kinds {
		if k != KindReadFailure && k != KindNotRegularFile {
			want = append(want, string(k))
		}
	}
	got := append([]string(nil), c.TextKinds...)
	sort.Strings(want)
	sort.Strings(got)
	if len(want) != len(got) {
		t.Fatalf("text_kinds %v, Kinds gives %v", got, want)
	}
	for i := range want {
		if want[i] != got[i] {
			t.Fatalf("text_kinds %v, Kinds gives %v", got, want)
		}
	}
	expected := map[string]bool{}
	for _, r := range c.Rows {
		expected[r.Expect] = true
	}
	for _, k := range want {
		if !expected[k] {
			t.Errorf("no corpus row expects %s", k)
		}
	}
}
