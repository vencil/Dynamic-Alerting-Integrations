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
// The files are not copied here: they are rows of
// tests/shared/parse_tree_corpus.json (category pyyaml_only, generator
// "rejected"), the corpus the parse-tree and migration-audit tests read, so
// the two cannot drift. The Python half — the same trees refused by
// `generate_alertmanager_routes.py --validate --strict` — is
// test_pyyaml_only_window_is_refused_by_generate_routes in
// tests/shared/test_parse_tree_corpus.py; keep the two name lists equal.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// pyyamlOnlyWindowRows: the ADR-036 §2 spellings only PyYAML refuses (its list
// of examples), each a parse_tree_corpus.json row name.
var pyyamlOnlyWindowRows = []string{
	"duplicate-anchor",           // the same anchor name defined twice
	"tab-after-colon",            // a TAB separator
	"invalid-calendar-timestamp", // a date that does not exist (2001-13-45)
	"single-quote-escape",        // `\'` in a double-quoted string
	"block-header-comment",       // `#` right after a block scalar header
	"flow-plain-ending-colon",    // a flow value ending in a colon ({b: 1:})
}

type windowCorpusRow struct {
	Name      string `json:"name"`
	File      string `json:"file"`
	Doc       string `json:"doc"`
	Category  string `json:"category"`
	Generator string `json:"generator"`
	Encoding  string `json:"encoding"`
}

// loadWindowRows reads pyyamlOnlyWindowRows out of parse_tree_corpus.json.
func loadWindowRows(t *testing.T) []windowCorpusRow {
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
	byName := make(map[string]windowCorpusRow, len(c.Rows))
	for _, r := range c.Rows {
		byName[r.Name] = r
	}
	out := make([]windowCorpusRow, 0, len(pyyamlOnlyWindowRows))
	for _, name := range pyyamlOnlyWindowRows {
		r, ok := byName[name]
		if !ok {
			t.Fatalf("parse_tree_corpus.json has no row %q", name)
		}
		// The window exists only while today's generator refuses the file and
		// parse-tree reads it; a row that stops being that is not this window.
		if r.Category != "pyyaml_only" || r.Generator != "rejected" || r.Encoding != "" {
			t.Fatalf("row %q: category %q, generator %q, encoding %q; want pyyaml_only, rejected, UTF-8",
				name, r.Category, r.Generator, r.Encoding)
		}
		out = append(out, r)
	}
	return out
}

// TestPyYAMLOnlyReject_WindowBeforeStep4: each tree holds only thresholds and
// no routing key — conf.d/_defaults.yaml, a threshold tenant, and the corpus
// file — and da-guard's default run is exit 0 with nothing undetermined.
func TestPyYAMLOnlyReject_WindowBeforeStep4(t *testing.T) {
	t.Parallel()
	for _, r := range loadWindowRows(t) {
		t.Run(r.Name, func(t *testing.T) {
			t.Parallel()
			tmp := t.TempDir()
			testutil.WriteTree(t, tmp, map[string]string{
				"conf.d/_defaults.yaml": rsDefaults,
				"conf.d/tenant-x.yaml":  rsTenant,
				"conf.d/" + r.File:      r.Doc,
			})
			code, stdout, stderr := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"), "--format", "json")
			var doc struct {
				ParseFailed  []string           `json:"parse_failed"`
				Undetermined []undeterminedItem `json:"undetermined"`
			}
			if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
				t.Fatalf("exit %d, report is not JSON: %v\nstdout=%s\nstderr=%s", code, err, stdout, stderr)
			}
			if code != exitOK || len(doc.Undetermined) != 0 || len(doc.ParseFailed) != 0 {
				t.Errorf("exit %d, undetermined %v, parse_failed %v; want exit 0 and both empty "+
					"(the ADR-036 §9 window before step 4)\nstdout=%s\nstderr=%s",
					code, doc.Undetermined, doc.ParseFailed, stdout, stderr)
			}
		})
	}
}
