package main

import (
	"os"
	"path/filepath"
	"testing"
)

// TestParseTree_FileModeVerdictIsTheWrittenTrees: file mode's verdict is the
// one tree mode gives --path once its text is written (setFileVerdict) — a
// text the exporter's decode refuses with no parse-tree problem included
// (`tenants:` a list: parse_failed, yet yaml.v3 reads it), which no reader
// could tell from role and problem.
func TestParseTree_FileModeVerdictIsTheWrittenTrees(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	for rel, text := range map[string]string{
		"a.yaml":        "tenants:\n  a: {}\n",
		"b.yaml":        "tenants: {t1: [\n",
		"x.yaml/t.yaml": "tenants:\n  t: {}\n",
	} {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	for _, tc := range []struct {
		rel, text string
		want      any
	}{
		{"a.yaml", "tenants: {t1: [\n", verdictUnservable},   // a parse-tree problem
		{"new.yaml", "tenants:\n  - x\n", verdictUnservable}, // the decode alone refuses it
		{"b.yaml", "tenants:\n  b: {}\n", nil},               // fixed by the write
		{"a.yaml", "tenants:\n  a: {}\n", nil},
		{".hidden.yaml", "tenants: {t1: [\n", nil},      // no walk records it
		{"x.yaml", "tenants: {}\n", verdictTreeRefused}, // a config-named directory: tree mode's entry
	} {
		code, doc, msg := runPT(t, tc.text, "--config-dir", root, "--path", tc.rel, "--input", "-")
		if code != exitOK {
			t.Fatalf("%s: exit %d: %s", tc.rel, code, msg)
		}
		e := doc["entries"].([]any)[0].(map[string]any)
		if e["verdict"] != tc.want {
			t.Errorf("%s with %q: verdict %v, want %v", tc.rel, tc.text, e["verdict"], tc.want)
		}
	}
	// Tree mode on the same disk: b.yaml is dropped, x.yaml refused.
	_, doc, _ := runPT(t, "", "--config-dir", root)
	got := map[string]any{}
	for p, e := range entriesByPath(t, doc) {
		if e["verdict"] != nil {
			got[p] = e["verdict"]
		}
	}
	if len(got) != 2 || got["b.yaml"] != verdictUnservable || got["x.yaml"] != verdictTreeRefused {
		t.Errorf("tree mode verdicts %v, want b.yaml unservable and x.yaml tree_refused", got)
	}
}

// TestParseTree_NamesModeHasNoVerdict: names mode judges no tree.
func TestParseTree_NamesModeHasNoVerdict(t *testing.T) {
	t.Parallel()
	_, doc, _ := runPTList(t, `["a.yaml","_defaults.yaml"]`, "--names", "-")
	for _, e := range doc.Entries {
		if e.Verdict != nil {
			t.Errorf("%s: verdict %q in names mode", e.Path, *e.Verdict)
		}
	}
}
