package main

// golden_served_test.go — which golden fixture trees /metrics serves (#2387).
//
// The golden parity suites (app/config_golden_parity_test.go,
// tests/golden/test_merge_parity.py) prove that the merge readers agree with
// each other on tests/golden/fixtures/*/conf.d. On a tree whose ROOT
// _defaults.yaml the exporter drops whole, that agreement says nothing about
// what /metrics serves. Five such trees are kept on purpose, as merge-core
// corpus, and listed in tests/golden/not_served.json. This test holds that
// list to the trees themselves, in both directions:
//
//   - a tree NOT listed must exit 0 from `da-guard served-values`, so a new
//     fixture (or an edit to a served one) cannot slip into the dropped shape
//     and leave parity green over a tree nothing serves;
//   - a listed tree must exit 3 with its ROOT _defaults.yaml in parse_failed
//     (the reason every entry gives), so the list cannot outlive the shape it
//     excuses, and must name a tree that exists.
//
// ⚠️ rc 0 means nothing in the tree is dropped. It does not mean every golden
// value equals the served one; see the served-* scenarios' comment in
// tests/golden/build_and_capture.py, and the header of
// app/config_golden_parity_test.go for the existing rows where they differ.

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"sort"
	"testing"
)

func goldenDir(t *testing.T) string {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..", "tests", "golden")
}

func loadNotServed(t *testing.T, golden string) map[string]string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(golden, "not_served.json"))
	if err != nil {
		t.Fatalf("read not_served.json: %v", err)
	}
	var doc struct {
		Comment []string          `json:"_comment"`
		Trees   map[string]string `json:"trees"`
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&doc); err != nil {
		t.Fatalf("parse not_served.json: %v", err)
	}
	for name, why := range doc.Trees {
		if why == "" {
			t.Errorf("not_served.json: %q has no reason", name)
		}
	}
	return doc.Trees
}

func TestGoldenFixtureTrees_ServedUnlessListed(t *testing.T) {
	t.Parallel()
	golden := goldenDir(t)
	notServed := loadNotServed(t, golden)

	confDs, err := filepath.Glob(filepath.Join(golden, "fixtures", "*", "conf.d"))
	if err != nil {
		t.Fatalf("glob: %v", err)
	}
	trees := make(map[string]string, len(confDs))
	for _, d := range confDs {
		if fi, err := os.Stat(d); err == nil && fi.IsDir() {
			trees[filepath.Base(filepath.Dir(d))] = d
		}
	}
	// Non-vacuity: a wrong path would glob nothing and pass every subtest.
	if len(trees) == 0 {
		t.Fatalf("found no fixture tree under %s", golden)
	}

	var stale []string
	for name := range notServed {
		if _, ok := trees[name]; !ok {
			stale = append(stale, name)
		}
	}
	sort.Strings(stale)
	if len(stale) > 0 {
		t.Errorf("not_served.json lists trees that do not exist: %v", stale)
	}

	for name, dir := range trees {
		name, dir := name, dir
		_, listed := notServed[name]
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			code, stdout, stderr := runOnce(t, servedValuesCmd,
				"--config-dir", dir, "--at", "2026-09-28T00:00:00Z")
			var doc servedOut
			if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
				t.Fatalf("stdout is not JSON (exit %d): %v\nstderr=%s", code, err, stderr)
			}
			if listed {
				if code != exitParseFailed || len(doc.ParseFailed) == 0 {
					t.Errorf("%s is listed in tests/golden/not_served.json but the exporter "+
						"serves it (exit %d, parse_failed %v): remove it from the list",
						name, code, doc.ParseFailed)
					return
				}
				// The list's reason is the ROOT defaults file being dropped.
				// Some other file failing to decode (a broken tenant file under
				// a root that now decodes) is a different tree, not this entry.
				if !slices.Contains(doc.ParseFailed, "_defaults.yaml") {
					t.Errorf("%s is listed in tests/golden/not_served.json for its root "+
						"_defaults.yaml, but parse_failed is %v: the root file is served, "+
						"so the entry no longer describes this tree",
						name, doc.ParseFailed)
				}
				return
			}
			if code != exitOK {
				t.Errorf("%s: `da-guard served-values` exit %d, parse_failed %v — the exporter "+
					"drops part of this golden tree, so parity on it is not about served "+
					"values. Fix the fixture's shape (root defaults: numbers only); list it "+
					"in tests/golden/not_served.json only if what it pins cannot be written "+
					"in a served shape.\nstderr=%s", name, code, doc.ParseFailed, stderr)
			}
		})
	}
}
