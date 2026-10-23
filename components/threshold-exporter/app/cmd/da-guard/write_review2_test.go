package main

import (
	"bytes"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestEdit_UnchangedNeedsTheMeaningUnchangedToo: an unset of a key only a
// `<<` merge supplies leaves the nodes as they were, but the change means
// the key is gone — refused by the read-back, never reported unchanged. A
// set to the value an explicit key already holds is unchanged; a set of a
// key only the merge supplies writes it explicitly.
func TestEdit_UnchangedNeedsTheMeaningUnchangedToo(t *testing.T) {
	const merged = "tenants:\n  base: &b\n    a: \"1\"\n    c: \"3\"\n  t2:\n    <<: *b\n"
	cases := []struct {
		name, base, ops, status, reason, file string
	}{
		{"unset a merged key", merged, `[{"op":"unset","path":["tenants","t2","a"]}]`,
			writeStatusRefused, writeReasonReadBackMismatch, merged},
		{"set an explicit key to its value", merged, `[{"op":"set","path":["tenants","base","a"],"value":{"str":"1"}}]`,
			writeStatusUnchanged, "", merged},
		{"set a merged key to its value", merged, `[{"op":"set","path":["tenants","t2","a"],"value":{"str":"1"}}]`,
			writeStatusWritten, "", merged + "    a: \"1\"\n"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := writeTree(t, map[string]string{"t2.yaml": tc.base})
			_, res := runW(t, editCmd, `{"ops":`+tc.ops+`}`, "--config-dir", root, "--path", "t2.yaml")
			if res.Status != tc.status || reasonOf(res) != tc.reason {
				t.Fatalf("status %s reason %q, want %s %q (%v)", res.Status, reasonOf(res), tc.status, tc.reason, res.Message)
			}
			got := snapshot(t, root)
			if len(got) != 1 || got["t2.yaml"] != tc.file {
				t.Fatalf("tree %q, want t2.yaml %q", got, tc.file)
			}
		})
	}
}

// TestEmit_NeverRemovesADirectoryItDidNotMake: a level another process
// makes between the scan and the Mkdir (EEXIST) is not this run's, and a
// write that does not complete leaves it; the levels this run made go.
func TestEmit_NeverRemovesADirectoryItDidNotMake(t *testing.T) {
	root := writeTree(t, map[string]string{"x.yaml": "a: \"1\"\n"})
	theirs := filepath.Join(root, "a")
	var out, errOut bytes.Buffer
	w := &writer{cmd: emitCmd, jsonOut: true, stdout: &out, errOut: &errOut,
		res:  writeResult{Schema: writeSchema, Command: emitCmd, Path: "a/b/t9.yaml", Warnings: []string{}},
		link: func(string, string) error { return fs.ErrExist },
		mkdir: func(p string, perm os.FileMode) error {
			if p == theirs {
				if err := os.Mkdir(p, perm); err != nil { // the other process
					return err
				}
			}
			return os.Mkdir(p, perm)
		}}
	if code := w.commit(filepath.Join(root, "a", "b", "t9.yaml"), []byte("a: \"1\"\n"), nil, 0o644, nil); code != exitFindings {
		t.Fatalf("exit %d %+v", code, w.res)
	}
	if fi, err := os.Stat(theirs); err != nil || !fi.IsDir() {
		t.Fatalf("the other process's directory was removed: %v", err)
	}
	if _, err := os.Lstat(filepath.Join(theirs, "b")); !os.IsNotExist(err) {
		t.Fatalf("this run's directory left behind: %v", err)
	}
	if !strings.Contains(errOut.String(), "refused") {
		t.Fatalf("stderr %q", errOut.String())
	}
}
