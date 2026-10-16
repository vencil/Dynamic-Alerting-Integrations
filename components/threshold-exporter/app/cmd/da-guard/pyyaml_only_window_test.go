package main

// pyyaml_only_window_test.go — the known window ADR-036 §9 records: until
// step 4 (#2906) the route generator still reads conf.d with PyYAML, so a file
// only PyYAML refuses, in a tree with no routing key, passes da-guard's default
// run (exit 0, nothing undetermined) while `da-tools generate-routes
// --validate --strict` refuses the whole file. That is why CI must still run
// the generator beside da-guard before step 4.
//
// ⛔ THIS TEST PINS A GAP, NOT A GUARANTEE. Once step 4 makes the generator read
// parse-tree's JSON, these files are read as yaml.v3 reads them on the
// generator's side too and the window is gone: flip this test to whatever the
// generator then decides (or delete it), and re-evaluate whether exit 4 still
// earns its keep — da-guard's condition for 4 would still be a prediction of
// the generator.
//
// ⛔ NO HAND-COPIED LIST. The window is MEASURED: every `rows` entry of
// tests/shared/parse_tree_corpus.json that today's generator rejects is run
// through da-guard, and the rows da-guard passes (exit 0, nothing undetermined,
// nothing in parse_failed) must be exactly the rows the corpus itself defines
// as the window — category pyyaml_only and a file name not starting with `_`
// (a tenant file: an `_` file the generator reads is undetermined evidence by
// name). A corpus row that enters or leaves the window turns this red instead
// of slipping past. The corpus's merge_key_rows are all `_domain_policy.yaml`,
// always undetermined by name, so they cannot be in the window and are not run.
//
// The Python half — every window row refused by
// `generate_alertmanager_routes.py --validate --strict` — is
// test_pyyaml_only_window_is_refused_by_generate_routes in
// tests/shared/test_parse_tree_corpus.py, over the same predicate.

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

// inPyYAMLOnlyWindow is the corpus's own definition of the window.
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

// TestPyYAMLOnlyReject_WindowBeforeStep4: the rows da-guard passes, among
// those today's generator rejects, are exactly the corpus's window rows.
func TestPyYAMLOnlyReject_WindowBeforeStep4(t *testing.T) {
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
			"rows the corpus defines as the ADR-036 §9 window (pyyaml_only, file not `_`):\n  %v",
			measured, defined)
	}
}
