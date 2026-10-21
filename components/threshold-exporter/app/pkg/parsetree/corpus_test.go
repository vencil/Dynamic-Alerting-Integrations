package parsetree

// Go half of tests/shared/parse_tree_corpus.json (#2897; Python half:
// tests/shared/test_parse_tree_corpus.py, which freezes today's route
// generator verdict on every row and checks each category against it).
//
// ADR-036 §2, owner option B: parse-tree refuses a CLOSED LIST of kinds
// (Kinds) and reads everything else as yaml.v3 does. Every corpus row carries
// a category — unreadable:<kind>, pyyaml_only or both_read — and this test
// holds parse-tree to it exactly, in both directions: a row refused with
// another kind, a refused row that is read, or a read row that is refused,
// is red. The merge_key_policy_corpus.json rows' categories are a snapshot
// (parse_tree_merge_key_categories.json) this test writes with
// UPDATE_PARSE_TREE_CATEGORIES=1; a change shows as a reviewed diff.

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
	Category  string `json:"category"`
}

type corpus struct {
	ClosedList   []string    `json:"closed_list"`
	TextKinds    []string    `json:"text_kinds"`
	Rows         []corpusRow `json:"rows"`
	MergeKeyRows []corpusRow `json:"merge_key_rows"`
}

const categoriesFile = "parse_tree_merge_key_categories.json"

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

// category is parse-tree's verdict on a file, as a corpus category.
// pyyaml_only and both_read are both "read" to parse-tree; which of the two
// a read row is depends on the generator's frozen verdict.
func category(data []byte, generator string) string {
	if _, bad := Parse(data); bad != nil {
		return "unreadable:" + string(bad.Kind)
	}
	if generator == "rejected" {
		return "pyyaml_only"
	}
	return "both_read"
}

func sameSorted(a, b []string) bool {
	a, b = append([]string(nil), a...), append([]string(nil), b...)
	sort.Strings(a)
	sort.Strings(b)
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func loadCorpus(t *testing.T) (corpus, map[string]corpusRow, string) {
	t.Helper()
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
	return c, docs, root
}

func fileBytes(t *testing.T, where string, r corpusRow) []byte {
	t.Helper()
	data := encode(t, r.Doc, r.Encoding)
	sum := sha256.Sum256(data)
	if id := hex.EncodeToString(sum[:])[:16]; id != r.ID {
		t.Fatalf("%s: the bytes hash to %s, the row says %s (encoding %q)", where, id, r.ID, r.Encoding)
	}
	return data
}

// TestCorpusCategories: parse-tree's verdict on every hand-picked row is the
// category written for it, exactly.
func TestCorpusCategories(t *testing.T) {
	t.Parallel()
	c, _, _ := loadCorpus(t)
	seen := map[string]int{}
	for _, r := range c.Rows {
		got := category(fileBytes(t, "row "+r.Name, r), r.Generator)
		if got != r.Category {
			t.Errorf("row %s: parse-tree gives %s, the corpus says %s", r.Name, got, r.Category)
		}
		seen[r.Category]++
	}
	if seen["pyyaml_only"] == 0 || seen["both_read"] == 0 {
		t.Fatalf("categories seen: %v", seen)
	}
}

// TestMergeKeyCorpusCategories: the same over every merge_key_policy_corpus
// row, against the snapshot of categories (UPDATE_PARSE_TREE_CATEGORIES=1
// rewrites it from parse-tree — review the diff).
func TestMergeKeyCorpusCategories(t *testing.T) {
	t.Parallel()
	c, docs, root := loadCorpus(t)
	got := make(map[string]string, len(c.MergeKeyRows))
	for _, r := range c.MergeKeyRows {
		src, ok := docs[r.ID]
		if !ok {
			t.Fatalf("merge_key_rows %s: no such row in merge_key_policy_corpus.json", r.ID)
		}
		r.Doc, r.Encoding = src.Doc, src.Encoding
		got[r.ID] = category(fileBytes(t, "merge_key row "+r.ID, r), r.Generator)
	}
	path := filepath.Join(root, "tests", "shared", categoriesFile)
	if os.Getenv("UPDATE_PARSE_TREE_CATEGORIES") != "" {
		b, err := json.MarshalIndent(map[string]any{
			"_comment": "parse-tree's category for each merge_key_policy_corpus.json row (by id). " +
				"Written by pkg/parsetree's TestMergeKeyCorpusCategories with " +
				"UPDATE_PARSE_TREE_CATEGORIES=1; checked against the generator verdict by " +
				"tests/shared/test_parse_tree_corpus.py. Never edit by hand.",
			"categories": got,
		}, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(b, '\n'), 0o644); err != nil { // #nosec G306 -- repo fixture
			t.Fatal(err)
		}
	}
	var snap struct {
		Categories map[string]string `json:"categories"`
	}
	readJSON(t, path, &snap)
	if len(snap.Categories) != len(got) {
		t.Errorf("%s holds %d rows, the corpus %d", categoriesFile, len(snap.Categories), len(got))
	}
	for id, cat := range got {
		if snap.Categories[id] != cat {
			t.Errorf("merge_key row %s: parse-tree gives %s, %s says %q", id, cat, categoriesFile, snap.Categories[id])
		}
	}
}

// TestCorpusCoversTheClosedList: the corpus's closed list is Kinds, and every
// kind Parse can give a text has a row refused with it.
func TestCorpusCoversTheClosedList(t *testing.T) {
	t.Parallel()
	c, _, _ := loadCorpus(t)
	var all, text []string
	for _, k := range Kinds {
		all = append(all, string(k))
		if k != KindReadFailure && k != KindNotRegularFile {
			text = append(text, string(k))
		}
	}
	if !sameSorted(all, c.ClosedList) {
		t.Fatalf("closed_list %v, Kinds %v", c.ClosedList, all)
	}
	if !sameSorted(text, c.TextKinds) {
		t.Fatalf("text_kinds %v, Kinds gives %v", c.TextKinds, text)
	}
	cats := map[string]bool{}
	for _, r := range c.Rows {
		cats[r.Category] = true
	}
	for _, k := range text {
		if !cats["unreadable:"+k] {
			t.Errorf("no corpus row is refused with %s", k)
		}
	}
}
