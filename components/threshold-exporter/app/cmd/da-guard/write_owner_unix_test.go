//go:build unix

package main

import (
	"bytes"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

// TestCommit_ReportsTheOwnerItCannotSet: commit hands the original's owner
// and group to writer.chown and reports what the kernel refuses, on stderr
// and in --json — with a fake chown, so it runs as any user.
func TestCommit_ReportsTheOwnerItCannotSet(t *testing.T) {
	for _, tc := range []struct {
		name   string
		refuse func(uid int) bool
		want   string
	}{
		{"owner refused", func(uid int) bool { return uid != -1 }, "group (gid "},
		{"both refused", func(int) bool { return true }, "owner and group"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := writeTree(t, map[string]string{"t1.yaml": "tenants: {}\n"})
			p := filepath.Join(root, "t1.yaml")
			fi, err := os.Stat(p)
			if err != nil {
				t.Fatal(err)
			}
			// An original another user owns (this process owns the
			// temporary file), so the owner differs and is tried.
			orig := otherOwner{fi, &syscall.Stat_t{Uid: 4321, Gid: 4322}}
			var out, errOut bytes.Buffer
			w := &writer{cmd: editCmd, jsonOut: true, stdout: &out, errOut: &errOut,
				chown: func(uid, _ int) error {
					if tc.refuse(uid) {
						return fmt.Errorf("chown: %w", fs.ErrPermission)
					}
					return nil
				}}
			code, res := commitOne(w, "t1.yaml", p, []byte("tenants:\n  t1: {}\n"), []byte("tenants: {}\n"), 0o644, orig)
			if code != exitOK || res.Status != writeStatusWritten {
				t.Fatalf("exit %d %+v", code, w.res)
			}
			if len(res.Warnings) != 1 || !strings.Contains(res.Warnings[0], tc.want) ||
				!strings.Contains(errOut.String(), "t1.yaml: warning: "+res.Warnings[0]) {
				t.Fatalf("warnings %q, stderr %q: want one naming %q", res.Warnings, errOut.String(), tc.want)
			}
			if !strings.Contains(out.String(), `"warnings":["`) {
				t.Fatalf("--json document %s", out.String())
			}
		})
	}
}

// otherOwner is a FileInfo whose owner is st's.
type otherOwner struct {
	os.FileInfo
	st *syscall.Stat_t
}

func (o otherOwner) Sys() any { return o.st }
