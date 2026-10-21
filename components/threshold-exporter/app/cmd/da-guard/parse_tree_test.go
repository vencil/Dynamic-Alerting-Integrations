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

func writeTree(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for rel, body := range files {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

// runPT runs the subcommand and decodes its one JSON document.
func runPT(t *testing.T, stdin string, args ...string) (int, map[string]any, string) {
	t.Helper()
	var out, errOut bytes.Buffer
	code := runParseTree(args, strings.NewReader(stdin), &out, &errOut)
	if code != exitOK {
		if out.Len() != 0 {
			t.Fatalf("exit %d with stdout %q", code, out.String())
		}
		return code, nil, errOut.String()
	}
	dec := json.NewDecoder(&out)
	var doc map[string]any
	if err := dec.Decode(&doc); err != nil {
		t.Fatalf("stdout is not one JSON document: %v", err)
	}
	if dec.More() {
		t.Fatal("stdout holds more than one JSON document")
	}
	if doc["schema"] != parseTreeSchema {
		t.Fatalf("schema %v", doc["schema"])
	}
	return code, doc, errOut.String()
}

// entriesByPath is the document's "entries", keyed by path; a path listed
// twice fails the test.
func entriesByPath(t *testing.T, doc map[string]any) map[string]map[string]any {
	t.Helper()
	out := map[string]map[string]any{}
	for _, raw := range doc["entries"].([]any) {
		e := raw.(map[string]any)
		p := e["path"].(string)
		if _, dup := out[p]; dup {
			t.Fatalf("%s: listed twice", p)
		}
		out[p] = e
	}
	return out
}

func stringsOf(v any) []string {
	out := []string{}
	for _, r := range v.([]any) {
		out = append(out, r.(string))
	}
	return out
}

// readersOf is who reads each path a reader tries — every path with content
// in "files" or a "problem" (what v1 called "selected").
func readersOf(doc map[string]any) map[string][]string {
	out := map[string][]string{}
	files := doc["files"].(map[string]any)
	for _, raw := range doc["entries"].([]any) {
		e := raw.(map[string]any)
		p := e["path"].(string)
		if _, ok := files[p]; ok || e["problem"] != nil {
			out[p] = stringsOf(e["readers"])
		}
	}
	return out
}

// unreadableKinds is path → problem kind, for every entry with a problem.
func unreadableKinds(doc map[string]any) map[string]string {
	out := map[string]string{}
	for _, raw := range doc["entries"].([]any) {
		e := raw.(map[string]any)
		if pr, ok := e["problem"].(map[string]any); ok {
			out[e["path"].(string)] = pr["kind"].(string)
		}
	}
	return out
}

// notReadOf is every entry no reader reads, as {file, reason}, in path order.
func notReadOf(doc map[string]any) []any {
	out := []any{}
	for _, raw := range doc["entries"].([]any) {
		e := raw.(map[string]any)
		if r, ok := e["not_read"].(string); ok {
			out = append(out, map[string]any{"file": e["path"], "reason": r})
		}
	}
	return out
}

// checkSelectedInvariant: every entry carries every field; a config entry
// with readers has its content in "files" or a problem, never both; every
// path in "files" is an entry's; an entry with no reader says why.
func checkSelectedInvariant(t *testing.T, doc map[string]any) {
	t.Helper()
	files := doc["files"].(map[string]any)
	byPath := entriesByPath(t, doc)
	for p := range files {
		if _, ok := byPath[p]; !ok {
			t.Errorf("files[%s]: no entry", p)
		}
	}
	for p, e := range byPath {
		for _, k := range []string{"path", "depth", "class", "tenant_named", "defaults_document",
			"readers", "not_read", "role", "problem"} {
			if _, ok := e[k]; !ok {
				t.Errorf("%s: no %q", p, k)
			}
		}
		if len(e) != 9 {
			t.Errorf("%s: %d fields, want 9: %v", p, len(e), e)
		}
		_, hasContent := files[p]
		readers := stringsOf(e["readers"])
		if (len(readers) == 0) != (e["not_read"] != nil) {
			t.Errorf("%s: readers %v, not_read %v", p, readers, e["not_read"])
		}
		if hasContent && e["problem"] != nil {
			t.Errorf("%s: content and a problem", p)
		}
		if doc["mode"] == "tree" && e["role"] == "config" && len(readers) > 0 && !hasContent && e["problem"] == nil {
			t.Errorf("%s: a config file a reader reads, with neither content nor a problem", p)
		}
	}
}

// TestParseTree_Readers pins which reader reads which file: the exporter's
// selection (tenant files, root `_` files, one carrier per directory) and the
// generator's (those, plus every `_` file below the root, plus an unselected
// carrier below the root).
func TestParseTree_Readers(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{
		"t1.yaml":                      "tenants:\n  t1: {cpu: 1}\n",
		"UPPER.YAML":                   "tenants:\n  t2: {cpu: 1}\n",
		"_defaults.yaml":               "defaults: {cpu: 80}\n",
		"_defaults.yml":                "defaults: {cpu: 70}\n",
		"_domain_policy.yaml":          "domain_policies: {}\n",
		"team/t3.yaml":                 "tenants:\n  t3: {cpu: 1}\n",
		"team/_defaults.yml":           "defaults: {cpu: 1}\n",
		"team/_DEFAULTS.yml":           "defaults: {cpu: 2}\n",
		"team/_domain_policy.yml":      "domain_policies: {}\n",
		"team/_routing_profiles.yaml":  "routing_profiles: {}\n",
		"team/_profiles.yaml":          "profiles: {}\n",
		"_sub/deep/_defaults.yaml":     "defaults: {cpu: 3}\n",
		"multi/_defaults.yaml":         "defaults: {cpu: 4}\n",
		"multi/_defaults.yml":          "defaults: {cpu: 5}\n",
		"multi/_defaults.YAML":         "a: [\n",
		".hidden/x.yaml":               "tenants: {h: {}}\n",
		"team/.h.yaml":                 "tenants: {h: {}}\n",
		"notes.md":                     "x",
		"team/_routing_profiles.yml.b": "x",
	})
	code, doc, _ := runPT(t, "", "--config-dir", root)
	if code != exitOK {
		t.Fatalf("exit %d", code)
	}
	both, gen := []string{"exporter", "generator"}, []string{"generator"}
	want := map[string][]string{
		"t1.yaml": both, "UPPER.YAML": both, "_defaults.yaml": both, "_domain_policy.yaml": both,
		"team/t3.yaml": both, "team/_DEFAULTS.yml": both, "multi/_defaults.yaml": both,
		"team/_domain_policy.yml": gen, "team/_routing_profiles.yaml": gen, "team/_profiles.yaml": gen,
		"_sub/deep/_defaults.yaml": both,
	}
	if got := readersOf(doc); !reflect.DeepEqual(got, want) {
		t.Fatalf("selected\n got %v\nwant %v", got, want)
	}
	if got := doc["defaults_carriers"]; !reflect.DeepEqual(got, map[string]any{
		".": "_defaults.yaml", "team": "team/_DEFAULTS.yml", "_sub/deep": "_sub/deep/_defaults.yaml",
		"multi": "multi/_defaults.yaml"}) {
		t.Fatalf("defaults_carriers %v", got)
	}
	// #1674: an unselected carrier is ignored whole, below the root as at it
	// (the generator names it and reads only the selected one) — so a broken
	// one is not reported unreadable.
	unsel := func(f string) any { return map[string]any{"file": f, "reason": notReadUnselectedCarrier} }
	if got := notReadOf(doc); !reflect.DeepEqual(got, []any{
		unsel("_defaults.yml"), unsel("multi/_defaults.YAML"), unsel("multi/_defaults.yml"), unsel("team/_defaults.yml")}) {
		t.Fatalf("not_read %v", got)
	}
	checkSelectedInvariant(t, doc)
	if doc["mode"] != "tree" || doc["da_guard_version"] != Version {
		t.Fatalf("mode %v, version %v", doc["mode"], doc["da_guard_version"])
	}
}

// TestParseTree_PathKinds: the two kinds about the path, not the text — a
// dangling link (read_failure), a config-named directory, a symlink to a
// device, a directory symlink the walk does not follow (not_regular_file) —
// next to a text kind.
func TestParseTree_PathKinds(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{"t1.yaml": "a: 1\na: 2\n", "dir.yaml/x.yaml": "a: 1\n"})
	outside := writeTree(t, map[string]string{"o.yaml": "a: 1\n"})
	for link, target := range map[string]string{
		"dangling.yaml": filepath.Join(root, "missing"),
		"null.yaml":     os.DevNull,
		"elsewhere":     outside,
		"inside.yaml":   filepath.Join(root, "dir.yaml"),
	} {
		if err := os.Symlink(target, filepath.Join(root, link)); err != nil {
			t.Skipf("symlink: %v", err)
		}
	}
	_, doc, _ := runPT(t, "", "--config-dir", root)
	want := map[string]string{
		"t1.yaml": "duplicate_key", "dangling.yaml": "read_failure", "null.yaml": "not_regular_file",
		"dir.yaml": "not_regular_file", "elsewhere": "not_regular_file", "inside.yaml": "not_regular_file",
	}
	if got := unreadableKinds(doc); !reflect.DeepEqual(got, want) {
		t.Fatalf("unreadable\n got %v\nwant %v", got, want)
	}
	if r := readersOf(doc)["dir.yaml"]; !reflect.DeepEqual(r, []string{"exporter", "generator"}) {
		t.Fatalf("dir.yaml readers %v", r)
	}
	if _, ok := doc["files"].(map[string]any)["dir.yaml/x.yaml"]; !ok {
		t.Fatal("the walk descends a config-named directory; its files are read")
	}
	checkSelectedInvariant(t, doc)
}

// TestParseTree_DecodePanicIsData: a file on which yaml.v3's decode panics
// (a collection key in a mapping that also merges) is an unreadable file in
// the document, not a crash of the run — parse-tree's walk decodes nothing.
func TestParseTree_DecodePanicIsData(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{
		"a.yaml":  "? [a]\n: 1\n<<: {a: 1}\n",
		"b.yaml":  "<<: &y [a, b]\n",
		"ok.yaml": "tenants: {t1: {cpu: 1}}\n",
	})
	code, doc, _ := runPT(t, "", "--config-dir", root)
	if code != exitOK {
		t.Fatalf("exit %d", code)
	}
	want := map[string]string{"a.yaml": "non_scalar_key", "b.yaml": "decode_error"}
	if got := unreadableKinds(doc); !reflect.DeepEqual(got, want) {
		t.Fatalf("unreadable %v, want %v", got, want)
	}
}

// TestParseTree_FileMode: one text for one path, its readers judged in the
// tree once the path is written.
func TestParseTree_FileMode(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{"_defaults.yaml": "defaults: {}\n", "team/t.yaml": "a: 1\n",
		"sub/_defaults.yaml": "defaults: {}\n"})
	for _, tc := range []struct {
		path, input string
		readers     []string
		notRead     string
		kind        string
	}{
		{"team/t.yaml", "a: yes\n", []string{"exporter", "generator"}, "", ""},
		{"team/_domain_policy.yaml", "domain_policies: {}\n", []string{"generator"}, "", ""},
		{"_defaults.yml", "defaults: {}\n", []string{}, notReadUnselectedCarrier, ""},
		{"team/_defaults.yml", "defaults: {}\n", []string{"exporter", "generator"}, "", ""},
		{"sub/_defaults.yml", "defaults: {}\n", []string{}, notReadUnselectedCarrier, ""},
		{"team/x.txt", "a: 1\n", []string{}, notReadNotConfigName, ""},
		{".git/x.yaml", "a: 1\n", []string{}, notReadHidden, ""},
		{"team/t.yaml", "a: ! \"<<\"\n", []string{"exporter", "generator"}, "", "non_specific_tag"},
	} {
		code, doc, _ := runPT(t, tc.input, "--config-dir", root, "--path", tc.path, "--input", "-")
		if code != exitOK {
			t.Fatalf("%s: exit %d", tc.path, code)
		}
		if got := readersOf(doc)[tc.path]; !reflect.DeepEqual(got, tc.readers) {
			t.Errorf("%s: readers %v, want %v", tc.path, got, tc.readers)
		}
		nr := notReadOf(doc)
		if (tc.notRead == "") != (len(nr) == 0) || tc.notRead != "" && nr[0].(map[string]any)["reason"] != tc.notRead {
			t.Errorf("%s: not_read %v, want %q", tc.path, nr, tc.notRead)
		}
		if got := unreadableKinds(doc)[tc.path]; got != tc.kind {
			t.Errorf("%s: kind %q, want %q", tc.path, got, tc.kind)
		}
		if tc.kind == "" {
			if _, ok := doc["files"].(map[string]any)[tc.path]; !ok {
				t.Errorf("%s: no content", tc.path)
			}
		}
		if doc["mode"] != "file" {
			t.Errorf("mode %v", doc["mode"])
		}
	}
	// --input as a file, and the directory's carrier once written.
	in := filepath.Join(t.TempDir(), "in.yaml")
	if err := os.WriteFile(in, []byte("defaults: {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	_, doc, _ := runPT(t, "", "--config-dir", root, "--path", "_defaults.yml", "--input", in)
	if got := doc["defaults_carriers"]; !reflect.DeepEqual(got, map[string]any{".": "_defaults.yaml"}) {
		t.Fatalf("defaults_carriers %v", got)
	}
}

// TestParseTree_ExitCodes: 0 for help and for any tree (an unreadable file is
// data), 2 for a caller error, never a partial stdout.
func TestParseTree_ExitCodes(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{"bad.yaml": "a: [\n"})
	var out, errOut bytes.Buffer
	if code := run([]string{parseTreeCmd, "-h"}, &out, &errOut); code != exitOK {
		t.Fatalf("-h: exit %d", code)
	}
	for _, kind := range []string{"non_specific_tag", "decode_error", "da-guard.parse-tree/v2", "unlistable_dir", "not_a_file"} {
		if !strings.Contains(errOut.String(), kind) {
			t.Errorf("-h does not document %q", kind)
		}
	}
	if code, _, _ := runPT(t, "", "--config-dir", root); code != exitOK {
		t.Fatalf("a tree with an unreadable file: exit %d, want 0", code)
	}
	for _, args := range [][]string{
		{},
		{"--nope"},
		{"--config-dir", filepath.Join(root, "missing")},
		{"--config-dir", filepath.Join(root, "bad.yaml")},
		{"--config-dir", root, "extra"},
		{"--config-dir", root, "--path", "x.yaml"},
		{"--config-dir", root, "--input", "-"},
		{"--config-dir", root, "--path", "../x.yaml", "--input", "-"},
		{"--config-dir", root, "--path", "/abs.yaml", "--input", "-"},
		{"--config-dir", root, "--path", ".", "--input", "-"},
		{"--config-dir", root, "--path", "x.yaml", "--input", filepath.Join(root, "missing")},
	} {
		if code, _, _ := runPT(t, "", args...); code != exitCallerErr {
			t.Errorf("%v: exit %d, want %d", args, code, exitCallerErr)
		}
	}
}

// TestParseTree_NonUTF8FileName: JSON cannot carry the name, so the tree is
// refused (2) rather than named with U+FFFD.
func TestParseTree_NonUTF8FileName(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "t\xff.yaml"), []byte("a: 1\n"), 0o600); err != nil {
		t.Skipf("filesystem refuses the name: %v", err)
	}
	if code, _, msg := runPT(t, "", "--config-dir", root); code != exitCallerErr || !strings.Contains(msg, "UTF-8") {
		t.Fatalf("exit %d, %q", code, msg)
	}
}

// TestParseTree_FileModeOnDisk: --path is judged against what already exists
// under --config-dir, not by its name alone. A parent the walk does not
// descend: read by no reader (not_read). An existing --path that is not a
// regular file: unreadable exactly as tree mode reports the same path.
func TestParseTree_FileModeOnDisk(t *testing.T) {
	t.Parallel()
	root := writeTree(t, map[string]string{"t.yaml": "a: 1\n", "sub/d.yaml/x.yaml": "a: 1\n", "real/r.yaml": "a: 1\n"})
	outside := t.TempDir()
	for link, target := range map[string]string{
		"extlink":       outside,
		"inlink":        filepath.Join(root, "real"),
		"dangling.yaml": filepath.Join(root, "missing"),
		"filelink.yaml": filepath.Join(root, "t.yaml"),
		"dirlink.yaml":  outside,
	} {
		if err := os.Symlink(target, filepath.Join(root, link)); err != nil {
			t.Skipf("symlink: %v", err)
		}
	}
	_, tree, _ := runPT(t, "", "--config-dir", root)
	treeEntries := entriesByPath(t, tree)
	both := []string{"exporter", "generator"}
	for _, tc := range []struct{ path, notRead, kind string }{
		{"extlink/a.yaml", notReadParentNotWalked, ""},
		{"inlink/a.yaml", notReadParentNotWalked, ""},
		{"t.yaml/a.yaml", notReadParentNotWalked, ""},
		{"dangling.yaml/a.yaml", notReadParentNotWalked, ""},
		{"sub/d.yaml", "", "not_regular_file"},
		{"dirlink.yaml", "", "not_regular_file"},
		{"dangling.yaml", "", "read_failure"},
		{"t.yaml", "", ""},
		{"filelink.yaml", "", ""},
		{"real/r.yaml", "", ""},
		{"new/dir/n.yaml", "", ""},
	} {
		_, doc, _ := runPT(t, "a: 1\n", "--config-dir", root, "--path", tc.path, "--input", "-")
		readers := readersOf(doc)[tc.path]
		nr := notReadOf(doc)
		checkSelectedInvariant(t, doc)
		switch {
		case tc.notRead != "":
			if len(readers) != 0 || len(nr) != 1 || nr[0].(map[string]any)["reason"] != tc.notRead {
				t.Errorf("%s: readers %v, not_read %v; want %s", tc.path, readers, nr, tc.notRead)
			}
		case tc.kind != "":
			// The same entry as tree mode's, every field.
			got := doc["entries"].([]any)
			want := treeEntries[tc.path]
			if len(got) != 1 || want == nil || !reflect.DeepEqual(got[0], any(want)) ||
				want["problem"].(map[string]any)["kind"] != tc.kind {
				t.Errorf("%s: entries %v; tree mode %v; want kind %s", tc.path, got, want, tc.kind)
			}
			if len(doc["files"].(map[string]any)) != 0 {
				t.Errorf("%s: the text was read", tc.path)
			}
		default:
			if len(nr) != 0 || !reflect.DeepEqual(readers, both) || len(unreadableKinds(doc)) != 0 {
				t.Errorf("%s: readers %v, not_read %v; want read by both", tc.path, readers, nr)
			}
		}
	}
}
