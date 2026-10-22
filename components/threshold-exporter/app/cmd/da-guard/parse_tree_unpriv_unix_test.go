//go:build unix

package main

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// TestParseTree_OneEntryPerPathUnderRealModes: every mode's entries hold
// each path once, on a tree where the mode bits really deny (as uid 65534
// when run as uid 0) — a config-named directory that cannot be listed (the
// walk once recorded it twice: a NotFile on entering it and a walk error
// on listing it), a directory that cannot be listed, one that may be listed
// but not searched, a mode-000 file, a link into a mode-000 directory. Each
// path's role is pinned beside (#3206).
func TestParseTree_OneEntryPerPathUnderRealModes(t *testing.T) {
	if !testutil.RunUnprivileged(t, "TestParseTree_OneEntryPerPathUnderRealModes") {
		return
	}
	root := t.TempDir()
	conf := filepath.Join(root, "conf.d")
	locked := filepath.Join(root, "locked")
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatalf("cannot build the fixture: %v", err)
		}
	}
	for _, d := range []string{"unlist.yaml", "sealed", "noexec", "dir.yaml"} {
		must(os.MkdirAll(filepath.Join(conf, d), 0o755))
	}
	must(os.MkdirAll(locked, 0o755))
	for p, body := range map[string]string{
		"ok.yaml": "tenants: {ok: {}}\n", "zero.yaml": "tenants: {z: {}}\n",
		"unlist.yaml/u.yaml": "tenants: {u: {}}\n", "sealed/s.yaml": "tenants: {s: {}}\n",
		"noexec/t.yaml": "tenants: {t: {}}\n", "dir.yaml/in.yaml": "tenants: {in: {}}\n",
	} {
		must(os.WriteFile(filepath.Join(conf, p), []byte(body), 0o644))
	}
	must(os.WriteFile(filepath.Join(locked, "real.yaml"), []byte("tenants: {b: {}}\n"), 0o644))
	must(os.Symlink(filepath.Join(locked, "real.yaml"), filepath.Join(conf, "b.yaml")))
	modes := map[string]os.FileMode{
		filepath.Join(conf, "unlist.yaml"): 0, filepath.Join(conf, "sealed"): 0,
		filepath.Join(conf, "noexec"): 0o444, filepath.Join(conf, "zero.yaml"): 0, locked: 0,
	}
	t.Cleanup(func() {
		for p := range modes {
			_ = os.Chmod(p, 0o755)
		}
	})
	for p, m := range modes {
		must(os.Chmod(p, m))
	}
	want := map[string]string{
		"unlist.yaml": "unlistable_dir", "sealed": "unlistable_dir", "dir.yaml": "dir",
		"noexec": "dir", "noexec/t.yaml": "config", "zero.yaml": "config", "b.yaml": "config", "ok.yaml": "config",
		"dir.yaml/in.yaml": "config",
	}
	for _, mode := range [][]string{nil, {"--list-only"}} {
		code, doc, msg := runPT(t, "", append(mode, "--config-dir", conf)...)
		if code != exitOK {
			t.Fatalf("%v: exit %d: %s", mode, code, msg)
		}
		got := map[string]string{}
		for p, e := range entriesByPath(t, doc) { // fails on a path listed twice
			got[p] = e["role"].(string)
		}
		for p, r := range want {
			if got[p] != r {
				t.Errorf("%v %s: role %q, want %q (all %v)", mode, p, got[p], r, got)
			}
		}
		if len(got) != len(want) {
			t.Errorf("%v: entries %v, want %v", mode, got, want)
		}
	}
	// File mode on each directory gives tree mode's very entry — the
	// unlistable ones too (#3206 F1/F2).
	_, tree, _ := runPT(t, "", "--config-dir", conf)
	treeEntries := entriesByPath(t, tree)
	for _, p := range []string{"unlist.yaml", "sealed", "noexec", "dir.yaml"} {
		_, doc, _ := runPT(t, "a: 1\n", "--config-dir", conf, "--path", p, "--input", "-")
		got := doc["entries"].([]any)
		if len(got) != 1 || !reflect.DeepEqual(got[0], any(treeEntries[p])) {
			t.Errorf("file mode %s: %v; tree mode %v", p, got, treeEntries[p])
		}
		if len(doc["files"].(map[string]any)) != 0 {
			t.Errorf("file mode %s: the text was read", p)
		}
	}
}
