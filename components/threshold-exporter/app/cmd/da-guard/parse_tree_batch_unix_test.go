//go:build unix

package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// batchProbePaths is every path a batch is asked about on one tree: each
// entry tree mode lists (files, directories, links, special files), and
// paths the walk does not hold — new files at the root and below it, a new
// carrier spelling beside the existing ones, a hidden name, a name that is
// not a config name, one below a file.
func batchProbePaths(t *testing.T, conf string) []string {
	t.Helper()
	code, doc, msg := runPT(t, "", "--config-dir", conf)
	set := map[string]bool{
		"zz-new-tenant.yaml": true, "new-team/zz-new.yaml": true, "_defaults.YML": true,
		".hidden.yaml": true, "notes.txt": true, "team-a/.x/y.yaml": true,
	}
	if code == exitOK {
		for p, e := range entriesByPath(t, doc) {
			if p == "." {
				continue
			}
			set[p] = true
			if e["role"] == "dir" {
				set[p+"/_defaults.yaml"] = true
				set[p+"/zz-added.yaml"] = true
			}
			if e["role"] == "config" {
				set[p+"/below-a-file.yaml"] = true
			}
		}
	} else if !strings.Contains(msg, "UTF-8") {
		t.Fatalf("tree mode: exit %d: %s", code, msg)
	}
	out := make([]string, 0, len(set))
	for p := range set {
		out = append(out, p)
	}
	sort.Strings(out)
	return out
}

// batchTexts is the texts each path is read as: its bytes on disk when it
// is a regular file (never opened otherwise: a FIFO would block), and texts
// that are valid, broken, and empty.
func batchTexts(conf, rel string) [][]byte {
	texts := [][]byte{
		[]byte("tenants:\n  probe-tenant:\n    mysql_connections: \"70\"\n"),
		[]byte("defaults:\n  mysql_connections: 80\nprofiles: {p: {a: 1}}\n"),
		[]byte("a: [unclosed\n"),
		[]byte("k: 1\nk: 2\n"),
		{},
		{0xff, 0xfe, 'x'},
	}
	p := filepath.Join(conf, filepath.FromSlash(rel))
	if fi, err := os.Lstat(p); err == nil && fi.Mode().IsRegular() {
		if data, err := os.ReadFile(p); err == nil { // #nosec G304 -- a test tree
			texts = append([][]byte{data}, texts...)
		}
	}
	return texts
}

// TestParseTreeBatch_IsFileModeLineForLine: on every fixture-catalogue tree
// (and the base tree alone), batch file mode's stdout is byte for byte the
// concatenation of file mode's stdout for each item, and their exit codes
// agree — the identity the generator container's one-call cache fill rests
// on (ADR-036 §3): an entry it stores is what the per-file fill stored.
func TestParseTreeBatch_IsFileModeLineForLine(t *testing.T) {
	c := loadFixtureCatalog(t)
	rows := append([]catalogRow{{ID: "base-only", Base: true}}, c.Rows...)
	for _, row := range rows {
		t.Run(row.ID, func(t *testing.T) {
			conf := buildCatalogRow(t, c, row, t.TempDir())
			var items []parseTreeBatchItem
			for _, rel := range batchProbePaths(t, conf) {
				for _, text := range batchTexts(conf, rel) {
					items = append(items, parseTreeBatchItem{Path: rel, Input: text})
				}
			}
			var want bytes.Buffer
			wantCode := exitOK
			inputFile := filepath.Join(t.TempDir(), "input")
			for _, it := range items {
				if err := os.WriteFile(inputFile, it.Input, 0o600); err != nil {
					t.Fatal(err)
				}
				var out, errOut bytes.Buffer
				code := runParseTree([]string{"--config-dir", conf, "--path", it.Path,
					"--input", inputFile}, strings.NewReader(""), &out, &errOut)
				if code != exitOK {
					wantCode = code
					break
				}
				want.Write(out.Bytes())
			}
			raw, err := json.Marshal(items)
			if err != nil {
				t.Fatal(err)
			}
			var got, errOut bytes.Buffer
			code := runParseTree([]string{"--config-dir", conf, "--inputs", "-"},
				bytes.NewReader(raw), &got, &errOut)
			if code != wantCode {
				t.Fatalf("batch exit %d, file mode %d: %s", code, wantCode, errOut.String())
			}
			if code != exitOK {
				return
			}
			if n := bytes.Count(got.Bytes(), []byte("\n")); n != len(items) {
				t.Fatalf("batch printed %d lines for %d items", n, len(items))
			}
			if !bytes.Equal(got.Bytes(), want.Bytes()) {
				g := strings.Split(got.String(), "\n")
				w := strings.Split(want.String(), "\n")
				for i := range w {
					if i >= len(g) || g[i] != w[i] {
						t.Fatalf("item %d (%s) differs:\nbatch: %.400s\nfile:  %.400s",
							i, items[i].Path, g[i], w[i])
					}
				}
				t.Fatal("batch output differs from file mode's")
			}
		})
	}
}

// TestParseTreeBatch_CallerErrors: what batch mode refuses, exit 2, nothing
// on stdout.
func TestParseTreeBatch_CallerErrors(t *testing.T) {
	conf := writeTree(t, map[string]string{"a.yaml": "tenants: {a: {}}\n"})
	for name, tc := range map[string]struct {
		args  []string
		stdin string
	}{
		"not an array":    {[]string{"--config-dir", conf, "--inputs", "-"}, `{"path": "a.yaml"}`},
		"null":            {[]string{"--config-dir", conf, "--inputs", "-"}, `null`},
		"two documents":   {[]string{"--config-dir", conf, "--inputs", "-"}, `[] []`},
		"trailing brace":  {[]string{"--config-dir", conf, "--inputs", "-"}, `[{"path": "a.yaml", "input": ""}]}`},
		"trailing text":   {[]string{"--config-dir", conf, "--inputs", "-"}, `[] x`},
		"unknown field":   {[]string{"--config-dir", conf, "--inputs", "-"}, `[{"path": "a.yaml", "text": ""}]`},
		"input not b64":   {[]string{"--config-dir", conf, "--inputs", "-"}, `[{"path": "a.yaml", "input": "!!"}]`},
		"absolute path":   {[]string{"--config-dir", conf, "--inputs", "-"}, `[{"path": "/a.yaml", "input": ""}]`},
		"leaves the root": {[]string{"--config-dir", conf, "--inputs", "-"}, `[{"path": "../a.yaml", "input": ""}]`},
		"with --path":     {[]string{"--config-dir", conf, "--inputs", "-", "--path", "a.yaml", "--input", "-"}, `[]`},
		"with list-only":  {[]string{"--config-dir", conf, "--inputs", "-", "--list-only"}, `[]`},
		"no config-dir":   {[]string{"--inputs", "-"}, `[]`},
		"missing file":    {[]string{"--config-dir", conf, "--inputs", filepath.Join(conf, "nope")}, ``},
	} {
		t.Run(name, func(t *testing.T) {
			var out, errOut bytes.Buffer
			code := runParseTree(tc.args, strings.NewReader(tc.stdin), &out, &errOut)
			if code != exitCallerErr || out.Len() != 0 {
				t.Fatalf("exit %d, stdout %q, want exit %d and nothing: %s",
					code, out.String(), exitCallerErr, errOut.String())
			}
		})
	}
	var out, errOut bytes.Buffer
	if code := runParseTree([]string{"--config-dir", conf, "--inputs", "-"},
		strings.NewReader(`[]`), &out, &errOut); code != exitOK || out.Len() != 0 {
		t.Fatalf("empty batch: exit %d, stdout %q: %s", code, out.String(), errOut.String())
	}
}
