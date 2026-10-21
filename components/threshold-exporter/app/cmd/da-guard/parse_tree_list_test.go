package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// runPTList runs `parse-tree --list-only …` and decodes its one document.
func runPTList(t *testing.T, stdin string, args ...string) (int, *parseTreeListDoc, string) {
	t.Helper()
	var out, errOut bytes.Buffer
	code := runParseTree(append([]string{"--list-only"}, args...), strings.NewReader(stdin), &out, &errOut)
	if code != exitOK {
		if out.Len() != 0 {
			t.Fatalf("exit %d with stdout %q", code, out.String())
		}
		return code, nil, errOut.String()
	}
	var doc parseTreeListDoc
	dec := json.NewDecoder(&out)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&doc); err != nil {
		t.Fatalf("stdout is not one list document: %v", err)
	}
	if dec.More() {
		t.Fatal("stdout holds more than one JSON document")
	}
	if doc.Schema != parseTreeListSchema {
		t.Fatalf("schema %q", doc.Schema)
	}
	return code, &doc, errOut.String()
}

// TestParseTreeList_AgreesWithTreeMode pins that --list-only answers "which
// files, read by whom" exactly as tree mode does — selected readers, not_read,
// defaults_carriers, and every unreadable entry decided without the bytes —
// and differs only where tree mode had to read: a content verdict
// (syntax_error here) is absent, the file listed with its readers.
func TestParseTreeList_AgreesWithTreeMode(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{
		"t1.yaml":                  "tenants: {t1: {}}\n",
		"Upper.YAML":               "tenants: {T2: {}}\n",
		"_defaults.yaml":           "defaults: {}\n",
		"_defaults.yml":            "defaults: {}\n",
		"team/_domain_policy.yaml": "x: 1\n",
		"team/_defaults.YML":       "defaults: {}\n",
		"team/sub/t3.yml":          "tenants: {t3: {}}\n",
		"broken.yaml":              ": bad: [\n",
		".git/a.yaml":              "x\n",
		"readme.md":                "x\n",
		"notes.yaml/inner.yaml":    "tenants: {t4: {}}\n",
	})
	if err := os.Symlink("nowhere.yaml", filepath.Join(root, "dangling.yaml")); err != nil {
		t.Fatal(err)
	}

	_, tree, _ := runPT(t, "", "--config-dir", root)
	_, list, _ := runPTList(t, "", "--config-dir", root)

	selected := map[string][]string{}
	notRead := []string{}
	unreadable := map[string]string{}
	for _, e := range list.Entries {
		switch {
		case e.NotRead != "":
			notRead = append(notRead, e.Path)
			if len(e.Readers) != 0 {
				t.Errorf("%s: not_read %q with readers %v", e.Path, e.NotRead, e.Readers)
			}
		default:
			selected[e.Path] = e.Readers
		}
		if e.Unreadable != nil {
			unreadable[e.Path] = string(e.Unreadable.Kind)
		}
	}
	if want := readersOf(tree); !reflect.DeepEqual(selected, want) {
		t.Errorf("selected readers\n list %v\n tree %v", selected, want)
	}
	wantNotRead := []string{}
	for _, n := range tree["not_read"].([]any) {
		wantNotRead = append(wantNotRead, n.(map[string]any)["file"].(string))
	}
	if !reflect.DeepEqual(notRead, wantNotRead) {
		t.Errorf("not_read list %v, tree %v", notRead, wantNotRead)
	}
	wantCarriers := map[string]string{}
	for d, c := range tree["defaults_carriers"].(map[string]any) {
		wantCarriers[d] = c.(string)
	}
	if !reflect.DeepEqual(list.DefaultsCarriers, wantCarriers) {
		t.Errorf("defaults_carriers list %v, tree %v", list.DefaultsCarriers, wantCarriers)
	}
	wantUnreadable := unreadableKinds(tree)
	if wantUnreadable["broken.yaml"] != "syntax_error" {
		t.Fatalf("fixture: tree mode gives broken.yaml %q", wantUnreadable["broken.yaml"])
	}
	delete(wantUnreadable, "broken.yaml") // a content verdict: list mode reads nothing
	if !reflect.DeepEqual(unreadable, wantUnreadable) {
		t.Errorf("unreadable list %v, tree %v", unreadable, wantUnreadable)
	}

	// Sorted by path, and what the name makes each entry.
	byPath := map[string]parseTreeListEntry{}
	for i, e := range list.Entries {
		if i > 0 && list.Entries[i-1].Path >= e.Path {
			t.Errorf("entries not sorted: %q before %q", list.Entries[i-1].Path, e.Path)
		}
		byPath[e.Path] = e
	}
	for _, c := range []struct {
		path, class, tenant string
		depth               int
	}{
		{"t1.yaml", classTenant, "t1", 0},
		{"Upper.YAML", classTenant, "Upper", 0}, // the name's case, not the `tenants:` key
		{"_defaults.yml", classDefaults, "", 0},
		{"team/_domain_policy.yaml", classPlatform, "", 1},
		{"team/sub/t3.yml", classTenant, "t3", 2},
		{"notes.yaml", classDirectory, "", 0},
		{"notes.yaml/inner.yaml", classTenant, "inner", 1},
	} {
		e, ok := byPath[c.path]
		if !ok {
			t.Errorf("%s: not listed", c.path)
			continue
		}
		if e.Class != c.class || e.TenantNamed != c.tenant || e.Depth != c.depth {
			t.Errorf("%s: class %q tenant_named %q depth %d, want %q %q %d",
				c.path, e.Class, e.TenantNamed, e.Depth, c.class, c.tenant, c.depth)
		}
	}
	for _, hidden := range []string{".git/a.yaml", "readme.md"} {
		if _, ok := byPath[hidden]; ok {
			t.Errorf("%s: listed, but the walk never reads it", hidden)
		}
	}
}

// TestParseTreeList_Names classifies given names by pkg/confdname alone.
func TestParseTreeList_Names(t *testing.T) {
	t.Parallel()
	_, doc, _ := runPTList(t, `["db-a.yaml","MiXeD.YmL",".h.yaml","_defaults.YML","_x.yml","n.txt","x.yaml.bak"]`, "--names", "-")
	if doc.Mode != "names" || len(doc.DefaultsCarriers) != 0 {
		t.Fatalf("mode %q carriers %v", doc.Mode, doc.DefaultsCarriers)
	}
	want := []parseTreeListEntry{
		{Path: "db-a.yaml", Class: classTenant, TenantNamed: "db-a"},
		{Path: "MiXeD.YmL", Class: classTenant, TenantNamed: "MiXeD"},
		{Path: ".h.yaml", Class: classHidden},
		{Path: "_defaults.YML", Class: classDefaults},
		{Path: "_x.yml", Class: classPlatform},
		{Path: "n.txt", Class: classNotConfigName},
		{Path: "x.yaml.bak", Class: classNotConfigName},
	}
	if !reflect.DeepEqual(doc.Entries, want) {
		t.Errorf("entries\n got  %+v\n want %+v", doc.Entries, want)
	}
}

func TestParseTreeList_CallerErrors(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{"t1.yaml": "tenants: {t1: {}}\n"})
	for _, tc := range []struct {
		stdin string
		args  []string
	}{
		{"", nil}, // neither --config-dir nor --names
		{"", []string{"--config-dir", root, "--path", "t1.yaml", "--input", "-"}},
		{`["a.yaml"]`, []string{"--config-dir", root, "--names", "-"}},
		{`"a.yaml"`, []string{"--names", "-"}},     // not an array
		{`["a/b.yaml"]`, []string{"--names", "-"}}, // not a base name
		{`[""]`, []string{"--names", "-"}},
		{"", []string{"--config-dir", filepath.Join(root, "missing")}},
	} {
		if code, _, _ := runPTList(t, tc.stdin, tc.args...); code != exitCallerErr {
			t.Errorf("--list-only %v: exit %d, want %d", tc.args, code, exitCallerErr)
		}
	}
	var out, errOut bytes.Buffer
	if code := runParseTree([]string{"--names", "-"}, strings.NewReader(`["a.yaml"]`), &out, &errOut); code != exitCallerErr {
		t.Errorf("--names without --list-only: exit %d, want %d", code, exitCallerErr)
	}
}
