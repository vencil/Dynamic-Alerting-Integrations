//go:build unix

package main

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

// TestWrite_NeverReadsANonRegularFile: a FIFO with no writer (which a plain
// read blocks on for ever) and a character device (which reads without end)
// are judged and refused not_read, at once, and never read.
func TestWrite_NeverReadsANonRegularFile(t *testing.T) {
	type special struct {
		name string
		make func(path string) error
	}
	specials := []special{
		{"fifo", func(p string) error { return syscall.Mkfifo(p, 0o600) }},
		// /dev/zero's numbers (1, 5): a device that never ends.
		{"char device", func(p string) error { return syscall.Mknod(p, syscall.S_IFCHR|0o600, 1<<8|5) }},
	}
	for _, sp := range specials {
		for _, cmd := range []string{editCmd, emitCmd} {
			t.Run(sp.name+"/"+cmd, func(t *testing.T) {
				root := writeTree(t, map[string]string{"t2.yaml": "tenants:\n  t2:\n    a: \"1\"\n"})
				if err := sp.make(filepath.Join(root, "t1.yaml")); err != nil {
					t.Skipf("cannot make a %s here: %v", sp.name, err)
				}
				in := `{"ops":[{"op":"set","path":["a"],"value":{"str":"1"}}]}`
				if cmd == emitCmd {
					in = `{"content":{"map":[["a",{"str":"1"}]]}}`
				}
				type result struct {
					code int
					res  writeResult
				}
				done := make(chan result, 1)
				go func() {
					code, res := runW(t, cmd, in, "--config-dir", root, "--path", "t1.yaml")
					done <- result{code, res}
				}()
				select {
				case r := <-done:
					if r.code != exitFindings || reasonOf(r.res) != writeReasonNotRead || r.res.Entry == nil {
						t.Fatalf("exit %d %+v %v", r.code, r.res, r.res.Message)
					}
				case <-time.After(10 * time.Second):
					t.Fatal("the write blocked on the special file")
				}
				fi, err := os.Lstat(filepath.Join(root, "t1.yaml"))
				if err != nil || fi.Mode().IsRegular() {
					t.Fatalf("the special file was replaced: %v %v", fi, err)
				}
			})
		}
	}
}

// TestEdit_KeepsTheOwner: the written file has the original's owner and
// group (needs the privilege to give a file away).
func TestEdit_KeepsTheOwner(t *testing.T) {
	root := writeTree(t, map[string]string{"t1.yaml": "tenants:\n  t1:\n    a: \"1\"\n"})
	p := filepath.Join(root, "t1.yaml")
	if err := os.Chown(p, 4321, 4322); err != nil {
		t.Skipf("cannot give a file away here: %v", err)
	}
	code, res := runW(t, editCmd, `{"ops":[{"op":"set","path":["tenants","t1","a"],"value":{"str":"2"}}]}`,
		"--config-dir", root, "--path", "t1.yaml")
	if code != exitOK || res.Status != writeStatusWritten || len(res.Warnings) != 0 {
		t.Fatalf("exit %d %+v", code, res)
	}
	fi, err := os.Stat(p)
	if err != nil {
		t.Fatal(err)
	}
	st := fi.Sys().(*syscall.Stat_t)
	if st.Uid != 4321 || st.Gid != 4322 {
		t.Fatalf("owner %d:%d, want 4321:4322", st.Uid, st.Gid)
	}
}
