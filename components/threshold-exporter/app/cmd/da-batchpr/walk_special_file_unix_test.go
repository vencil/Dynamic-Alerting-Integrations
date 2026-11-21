//go:build unix

package main

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// #2950: walkFilesDir loads --emit-dir / --patches-dir with os.ReadFile, so a
// FIFO in either tree blocked apply and refresh-source forever (open(2) waits
// for a writer). Now the walk refuses it — and a symlink to one — with an
// error naming the path, before anything opens it.
//
// The call runs in a goroutine with a deadline: if the IsSpecialFile check is
// removed, the test fails on the timeout instead of hanging the test binary
// (the stuck goroutine stays blocked in open(2) until the binary exits).
func TestWalkFilesDir_SpecialFileRefusedWithoutBlocking(t *testing.T) {
	t.Parallel()
	shapes := map[string]func(t *testing.T, root string) string{
		"fifo": func(t *testing.T, root string) string {
			p := filepath.Join(root, "sub", "stuck.yaml")
			mustWriteFile(t, filepath.Join(root, "sub", "ok.yaml"), []byte("ok"))
			mkfifoOrSkip(t, p)
			return p
		},
		"symlink to fifo outside root": func(t *testing.T, root string) string {
			outside := filepath.Join(t.TempDir(), "pipe.yaml")
			mkfifoOrSkip(t, outside)
			p := filepath.Join(root, "link.yaml")
			if err := os.Symlink(outside, p); err != nil {
				t.Skipf("symlink: %v", err)
			}
			return p
		},
	}
	for name, build := range shapes {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			root := t.TempDir()
			mustWriteFile(t, filepath.Join(root, "top.yaml"), []byte("top"))
			special := build(t, root)

			type result struct {
				files map[string][]byte
				err   error
			}
			done := make(chan result, 1)
			go func() {
				files, err := walkFilesDir(root, "")
				done <- result{files, err}
			}()
			var res result
			select {
			case res = <-done:
			case <-time.After(3 * time.Second):
				t.Fatal("walkFilesDir blocked reading a special file")
			}
			if res.err == nil {
				t.Fatalf("walkFilesDir: want an error for %s, got nil (files: %v)", special, keysOfBytesMap(res.files))
			}
			if !errors.Is(res.err, config.ErrSpecialFile) {
				t.Errorf("error does not wrap config.ErrSpecialFile: %v", res.err)
			}
			if !strings.Contains(res.err.Error(), special) {
				t.Errorf("error does not name %q: %v", special, res.err)
			}
		})
	}
}

func mkfifoOrSkip(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Mkfifo(path, 0o644); err != nil {
		t.Skipf("mkfifo unavailable: %v", err)
	}
}
