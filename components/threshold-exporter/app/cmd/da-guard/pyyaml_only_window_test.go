package main

// pyyaml_only_window_test.go — the window ADR-036 §9 recorded, closed by
// step 4 (#2906): a file only PyYAML refuses, in a tree with no routing key,
// passes da-guard's default run (exit 0, nothing undetermined). Before step 4
// the route generator still read conf.d with PyYAML and refused the whole
// file; since step 4 it reads parse-tree's JSON, so it reads these files as
// da-guard does. This test still pins da-guard's half — which rows it passes
// — so a row that enters or leaves the set shows up; the generator's half is
// test_pyyaml_only_rows_are_read_by_generate_routes in
// tests/shared/test_parse_tree_corpus.py, over the same predicate, and now
// asserts that the generator reads each of them too.
//
// ⛔ NO HAND-COPIED LIST. The set is MEASURED: every `rows` entry of
// tests/shared/parse_tree_corpus.json that the generator rejected before
// step 4 (the corpus's frozen verdict) is run through da-guard, and the rows
// da-guard passes (exit 0, nothing undetermined, nothing in parse_failed)
// must be exactly the rows the corpus itself defines — category pyyaml_only
// and a file name not starting with `_` (a tenant file: an `_` file the
// generator reads is undetermined evidence by name). The corpus's
// merge_key_rows are all `_domain_policy.yaml`, always undetermined by name,
// so they cannot be in the set and are not run.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"testing"
	"unicode/utf16"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

type windowCorpusRow struct {
	Name      string `json:"name"`
	File      string `json:"file"`
	Doc       string `json:"doc"`
	Encoding  string `json:"encoding"`
	Category  string `json:"category"`
	Generator string `json:"generator"`
}

// loadWindowCorpus reads the `rows` of parse_tree_corpus.json.
func loadWindowCorpus(t *testing.T) []windowCorpusRow {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "parse_tree_corpus.json")
	raw, err := os.ReadFile(path) // #nosec G304 -- repo test fixture
	if err != nil {
		t.Fatalf("read corpus: %v", err)
	}
	var c struct {
		Rows []windowCorpusRow `json:"rows"`
	}
	if err := json.Unmarshal(raw, &c); err != nil {
		t.Fatalf("decode corpus: %v", err)
	}
	return c.Rows
}

// windowEncode is the file a corpus row stands for (internal/parsetree's
// corpus_test.go encode; test_merge_key_policy_corpus._encode in Python).
func windowEncode(t *testing.T, r windowCorpusRow) string {
	t.Helper()
	switch r.Encoding {
	case "":
		return r.Doc
	case "latin-1":
		out := make([]byte, 0, len(r.Doc))
		for _, c := range r.Doc {
			if c > 0xFF {
				t.Fatalf("row %s: latin-1 row holds %q", r.Name, c)
			}
			out = append(out, byte(c))
		}
		return string(out)
	case "utf-16-le-bom", "utf-16-be-bom":
		le := r.Encoding == "utf-16-le-bom"
		out := []byte{0xFE, 0xFF}
		if le {
			out = []byte{0xFF, 0xFE}
		}
		for _, u := range utf16.Encode([]rune(r.Doc)) {
			if le {
				out = append(out, byte(u), byte(u>>8))
			} else {
				out = append(out, byte(u>>8), byte(u))
			}
		}
		return string(out)
	}
	t.Fatalf("row %s: unknown encoding %q", r.Name, r.Encoding)
	return ""
}

// inPyYAMLOnlyWindow is the corpus's own definition of the (closed) window.
func inPyYAMLOnlyWindow(r windowCorpusRow) bool {
	return r.Category == "pyyaml_only" && !strings.HasPrefix(r.File, "_")
}

// passesDefaultRun: da-guard's default run over conf.d/_defaults.yaml, a
// threshold-only tenant, and the row's file is exit 0 with nothing
// undetermined and nothing in parse_failed.
func passesDefaultRun(t *testing.T, r windowCorpusRow) bool {
	t.Helper()
	if r.File == "_defaults.yaml" || r.File == "tenant-x.yaml" {
		t.Fatalf("row %s: file %s collides with the tree's own files", r.Name, r.File)
	}
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": rsDefaults,
		"conf.d/tenant-x.yaml":  rsTenant,
		"conf.d/" + r.File:      windowEncode(t, r),
	})
	code, stdout, stderr := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"), "--format", "json")
	var doc struct {
		ParseFailed  []string           `json:"parse_failed"`
		Undetermined []undeterminedItem `json:"undetermined"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("row %s: exit %d, report is not JSON: %v\nstdout=%s\nstderr=%s", r.Name, code, err, stdout, stderr)
	}
	return code == exitOK && len(doc.Undetermined) == 0 && len(doc.ParseFailed) == 0
}

// TestPyYAMLOnlyRows_PassDaGuardDefaultRun: the rows da-guard passes, among
// those the PyYAML generator rejected, are exactly the corpus's pyyaml_only
// tenant-file rows.
func TestPyYAMLOnlyRows_PassDaGuardDefaultRun(t *testing.T) {
	t.Parallel()
	var measured, defined []string
	for _, r := range loadWindowCorpus(t) {
		if r.Generator != "rejected" {
			continue
		}
		if passesDefaultRun(t, r) {
			measured = append(measured, r.Name)
		}
		if inPyYAMLOnlyWindow(r) {
			defined = append(defined, r.Name)
		}
	}
	sort.Strings(measured)
	sort.Strings(defined)
	if len(defined) == 0 {
		t.Fatal("the corpus defines no window row (category pyyaml_only, file not `_`); the predicate or the corpus changed")
	}
	if strings.Join(measured, "\n") != strings.Join(defined, "\n") {
		t.Errorf("rows the generator rejects that da-guard passes (exit 0, nothing undetermined):\n  %v\n"+
			"rows the corpus defines as pyyaml_only tenant-file rows (file not `_`):\n  %v",
			measured, defined)
	}
}
