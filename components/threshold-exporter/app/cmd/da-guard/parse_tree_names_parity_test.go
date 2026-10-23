package main

// parse_tree_names_parity_test.go — names mode answers what tree mode answers
// (#3206). A Python caller that holds names but no tree (a ConfigMap's keys,
// a file it is about to write, a hidden name the walk never lists) asks
// `parse-tree --list-only --names`; it must get, for every row of the shared
// name table, the answer a walk of a directory holding those names gives:
// class, tenant id, defaults-document, config extension, and which defaults
// carrier the directory selects (the losers' not_read).
//
// Seams: none — runParseTree in process over a temp directory.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

type nameTableRow struct {
	Name          string `json:"name"`
	YAMLExtension bool   `json:"yaml_extension"`
	Hidden        bool   `json:"hidden"`
}

func loadNameTable(t *testing.T) []nameTableRow {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	p := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "confd_name_classification_matrix.json")
	raw, err := os.ReadFile(p) // #nosec G304 -- repo-relative test fixture
	if err != nil {
		t.Fatalf("read the name table: %v", err)
	}
	var doc struct {
		Rows []nameTableRow `json:"rows"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Rows) < 18 {
		t.Fatalf("the name table shrank to %d rows", len(doc.Rows))
	}
	return doc.Rows
}

func TestParseTree_NamesModeAgreesWithTreeModeOnEveryNameTableRow(t *testing.T) {
	t.Parallel()
	rows := loadNameTable(t)
	names := make([]string, 0, len(rows))
	files := make(map[string]string, len(rows))
	for _, r := range rows {
		names = append(names, r.Name)
		files[r.Name] = "{}\n"
	}
	stdin, err := json.Marshal(names)
	if err != nil {
		t.Fatal(err)
	}
	_, byName, _ := runPTList(t, string(stdin), "--names", "-")
	named := make(map[string]wireEntry, len(byName.Entries))
	for _, e := range byName.Entries {
		named[e.Path] = e
	}
	// The extension is the table's column, hidden or not.
	for _, r := range rows {
		if got := named[r.Name].ConfigExtension; got != r.YAMLExtension {
			t.Errorf("%q: names-mode config_extension %t, table yaml_extension %t", r.Name, got, r.YAMLExtension)
		}
	}

	root := writeTree(t, files)
	ents, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(ents) != len(rows) {
		t.Skipf("the filesystem folds names (%d of %d rows on disk): tree mode CANNOT BE MEASURED here", len(ents), len(rows))
	}
	_, tree, _ := runPTList(t, "", "--config-dir", root)
	walked := 0
	for _, e := range tree.Entries {
		n, ok := named[e.Path]
		if !ok {
			t.Errorf("%q: walked, but not a given name", e.Path)
			continue
		}
		walked++
		if e.Class != n.Class || !eqStr(e.TenantNamed, n.TenantNamed) ||
			e.DefaultsDocument != n.DefaultsDocument || e.ConfigExtension != n.ConfigExtension ||
			!eqStr(e.NotRead, n.NotRead) {
			t.Errorf("%q: tree mode {class %s tenant %v doc %t ext %t not_read %v}, names mode "+
				"{class %s tenant %v doc %t ext %t not_read %v}", e.Path,
				e.Class, deref(e.TenantNamed), e.DefaultsDocument, e.ConfigExtension, deref(e.NotRead),
				n.Class, deref(n.TenantNamed), n.DefaultsDocument, n.ConfigExtension, deref(n.NotRead))
		}
	}
	// Every name the walk does not list is one it skips by name: hidden, or
	// no config extension.
	for _, r := range rows {
		if r.Hidden || !r.YAMLExtension {
			continue
		}
		found := false
		for _, e := range tree.Entries {
			found = found || e.Path == r.Name
		}
		if !found {
			t.Errorf("%q: a config name the walk did not list", r.Name)
		}
	}
	if walked == 0 {
		t.Fatal("the walk listed none of the table's names: the comparison measured nothing")
	}
	if tree.DefaultsCarriers["."] != byName.DefaultsCarriers["."] || tree.DefaultsCarriers["."] == "" {
		t.Errorf("defaults carrier: tree mode %q, names mode %q", tree.DefaultsCarriers["."], byName.DefaultsCarriers["."])
	}
}

// The selection among several carrier spellings is the walk's, in names mode
// too: the cells of pkg/config's selection table, by name alone.
func TestParseTree_NamesModeSelectsTheCarrierTheWalkSelects(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		names []string
		want  string
	}{
		{[]string{"_defaults.yaml", "_defaults.yml"}, "_defaults.yaml"},
		{[]string{"_DEFAULTS.YML", "_defaults.yaml"}, "_defaults.yaml"},
		{[]string{"_Defaults.yaml", "_defaults.YAML"}, "_defaults.YAML"},
		{[]string{"_DEFAULTS.YML", "_defaults.yml"}, "_DEFAULTS.YML"},
		{[]string{"_defaults.yml", "a.yaml"}, "_defaults.yml"},
	} {
		raw, _ := json.Marshal(tc.names)
		_, doc, _ := runPTList(t, string(raw), "--names", "-")
		if got := doc.DefaultsCarriers["."]; got != tc.want {
			t.Errorf("%v: carrier %q, want %q", tc.names, got, tc.want)
		}
		for _, e := range doc.Entries {
			loser := e.Class == classDefaults && e.Path != tc.want
			if loser != (e.NotRead != nil && *e.NotRead == notReadUnselectedCarrier) {
				t.Errorf("%v: %q not_read %v", tc.names, e.Path, deref(e.NotRead))
			}
		}
	}
	_, none, _ := runPTList(t, `["a.yaml"]`, "--names", "-")
	if len(none.DefaultsCarriers) != 0 {
		t.Errorf("no carrier among the names, defaults_carriers %v", none.DefaultsCarriers)
	}
}

func eqStr(a, b *string) bool { return (a == nil) == (b == nil) && (a == nil || *a == *b) }
