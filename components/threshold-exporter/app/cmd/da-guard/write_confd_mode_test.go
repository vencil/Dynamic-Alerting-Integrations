package main

import (
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// #3535: every file da-guard writes into conf.d is config.ConfDFileMode,
// whatever mode the file it rewrites had (a conf.d file is a repo artifact:
// committed, read by another uid). The create, copy and replace kinds are
// pinned in their own tests; these are the edit, and the move — which
// renames the same file and writes nothing, so its mode is the source's.

func TestWrite_AnEditRewritesTheFileInConfDFileMode(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX modes")
	}
	root := writeTree(t, map[string]string{"t.yaml": "tenants:\n  t3:\n    cpu: 1\n"})
	abs := filepath.Join(root, "t.yaml")
	if err := os.Chmod(abs, 0o600); err != nil {
		t.Fatal(err)
	}
	edit := `{"files":[{"path":"t.yaml","ops":[{"op":"set","path":["tenants","t3","cpu"],"value":{"int":"5"}}]}]}`
	if code, res := runBatch(t, writeCmd, edit, "--config-dir", root); code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("exit %d %v (%v)", code, statuses(res), res.Message)
	}
	if fi, err := os.Stat(abs); err != nil || fi.Mode().Perm() != config.ConfDFileMode {
		t.Fatalf("mode %v (%v), want config.ConfDFileMode, not the original's 0600", fi.Mode().Perm(), err)
	}
}

func TestWrite_AMoveKeepsTheSourcesMode(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX modes")
	}
	root := writeTree(t, map[string]string{"a.yaml": "tenants:\n  t3:\n    cpu: 1\n"})
	if err := os.Chmod(filepath.Join(root, "a.yaml"), 0o600); err != nil {
		t.Fatal(err)
	}
	move := `{"files":[{"path":"b.yaml","from":"a.yaml"}]}`
	if code, res := runBatch(t, writeCmd, move, "--config-dir", root); code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("exit %d %v (%v)", code, statuses(res), res.Message)
	}
	if fi, err := os.Stat(filepath.Join(root, "b.yaml")); err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v (%v), want the source's 0600: a move writes nothing", fi.Mode().Perm(), err)
	}
}

// A directory made for a file is config.ConfDDirMode whatever the umask
// narrowed: the mkdir seam stands in for a umask of 077 (a 0700 directory
// would hide the 0644 file inside it from another uid).
func TestEmit_ADirectoryItMakesIsConfDDirMode(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX modes")
	}
	root := writeTree(t, map[string]string{"x.yaml": "a: \"1\"\n"})
	var out, errOut bytes.Buffer
	w := &writer{cmd: writeCmd, jsonOut: true, stdout: &out, errOut: &errOut,
		mkdir: func(p string, _ os.FileMode) error { return os.Mkdir(p, 0o700) }}
	target := filepath.Join(root, "a", "b", "t9.yaml")
	if code, res := commitOne(w, "a/b/t9.yaml", target, []byte("a: \"1\"\n"), nil, config.ConfDFileMode, nil); code != exitOK {
		t.Fatalf("exit %d %+v (%s)", code, res, errOut.String())
	}
	for _, d := range []string{filepath.Join(root, "a"), filepath.Join(root, "a", "b")} {
		if fi, err := os.Stat(d); err != nil || fi.Mode().Perm() != config.ConfDDirMode {
			t.Fatalf("%s: mode %v (%v), want config.ConfDDirMode", d, fi.Mode().Perm(), err)
		}
	}
}
