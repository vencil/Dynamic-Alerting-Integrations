//go:build linux

package gitops

// #2939: the special-file, read_error and walk_error shapes of the
// whole-tree verdict (writer_tree_unreadable_test.go has the portable ones),
// and readMerge's own read of a tenant file that is a FIFO. Linux only: the
// read_error shape is a symlink to /proc/self/mem, and the walk_error one a
// directory past PATH_MAX (testutil.UnlistableDeepDir) — the shapes that
// fail for root, which chmod does not. Seams: none.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/testutil"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// mkfifoReleased puts a FIFO at path and, at cleanup, releases any reader a
// failing run left waiting on it (open for write, then close → EOF).
func mkfifoReleased(t *testing.T, path string) {
	t.Helper()
	if err := syscall.Mkfifo(path, 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	t.Cleanup(func() {
		if f, err := os.OpenFile(path, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
			_ = f.Close()
		}
	})
}

// unreadableShapes build one unreadable entry in conf.d that is not the
// written tenant's file and return what the walker records for it.
var unreadableShapes = []struct {
	name  string
	build func(t *testing.T, dir string) cfg.UnreadableFile
}{
	{"fifo", func(t *testing.T, dir string) cfg.UnreadableFile { // (b)
		mkfifoReleased(t, filepath.Join(dir, "stuck.yaml"))
		return cfg.UnreadableFile{RelKey: "stuck.yaml", Reason: cfg.UnreadableSpecialFile}
	}},
	{"read_error", func(t *testing.T, dir string) cfg.UnreadableFile {
		mem, ok := testutil.ProcSelfMem()
		if !ok {
			t.Skip("no /proc/self/mem that fails to read")
		}
		if err := os.Symlink(mem, filepath.Join(dir, "eio.yaml")); err != nil {
			t.Fatal(err)
		}
		return cfg.UnreadableFile{RelKey: "eio.yaml", Reason: cfg.UnreadableReadError}
	}},
	{"walk_error", func(t *testing.T, dir string) cfg.UnreadableFile { // (d)
		// slack 100: a batch's staging copy (a root ~45 bytes longer) can
		// still hold every directory the serving walk lists.
		return cfg.UnreadableFile{RelKey: testutil.UnlistableDeepDir(t, dir, 100), Reason: cfg.UnreadableWalkError}
	}},
}

// (b) (d) and read_error: every direct write path and preview refuses, at
// once, naming exactly the entry, and nothing is written. The PR modes judge
// the base git holds, where none of these untracked shapes exists; their
// verdict is pinned with a committed shape (writer_tree_unreadable_test.go).
func TestTreeUnreadable_ShapesRefuseEveryDirectPath(t *testing.T) {
	for _, shape := range unreadableShapes {
		for _, mode := range directTreeModes {
			t.Run(shape.name+"/"+mode, func(t *testing.T) {
				dir := seedTreeRepo(t, map[string]string{"sh-t.yaml": tenantBody("sh-t")})
				want := shape.build(t, dir)
				head := gitRevParse(t, dir, "HEAD")
				w := NewWriter(dir, dir)
				done := make(chan error, 1)
				go func() { done <- treeVerdictVia(t, mode, w, "sh-t") }()
				select {
				case err := <-done:
					assertTreeUnreadable(t, mode, err, []cfg.UnreadableFile{want})
				case <-time.After(30 * time.Second):
					t.Fatal("blocked")
				}
				if got := gitRevParse(t, dir, "HEAD"); got != head {
					t.Errorf("HEAD moved %s → %s", head, got)
				}
				if got, _ := os.ReadFile(filepath.Join(dir, "sh-t.yaml")); string(got) != tenantBody("sh-t") {
					t.Errorf("sh-t.yaml changed: %q", got)
				}
			})
		}
	}
}

// (h) read_error in PR mode: a committed symlink to /proc/self/mem is on the
// fresh base, so WritePR and WritePRBatch refuse like the direct paths.
func TestTreeUnreadable_ReadErrorOnTheBaseRefusesPRModes(t *testing.T) {
	mem, ok := testutil.ProcSelfMem()
	if !ok {
		t.Skip("no /proc/self/mem that fails to read")
	}
	for _, mode := range []string{"WritePR", "WritePRBatch"} {
		t.Run(mode, func(t *testing.T) {
			dir := seedTreeRepo(t, map[string]string{"prr-t.yaml": tenantBody("prr-t")})
			if err := os.Symlink(mem, filepath.Join(dir, "eio.yaml")); err != nil {
				t.Fatal(err)
			}
			gitRun(t, dir, "add", "-A")
			gitRun(t, dir, "commit", "-m", "read_error link")
			err := treeVerdictVia(t, mode, NewWriter(dir, dir), "prr-t")
			assertTreeUnreadable(t, mode, err, []cfg.UnreadableFile{{RelKey: "eio.yaml", Reason: cfg.UnreadableReadError}})
		})
	}
}

// read_error on the write's OWN file is not counted by the whole-tree
// verdict: it is a target (refuseUnreadableTree excludes it, as a dangling
// one). ⚠️ The write is still refused today — by validate's eol-expansion
// guard, which fails closed on a current file it cannot read (#2405,
// eolGuardErrs) — so this pins only that the refusal is NOT the tree's.
// Whether the guard should let such a file be replaced is an open decision.
func TestTreeUnreadable_ReadErrorTargetIsNotCountedByTheTree(t *testing.T) {
	mem, ok := testutil.ProcSelfMem()
	if !ok {
		t.Skip("no /proc/self/mem that fails to read")
	}
	dir := seedTreeRepo(t, map[string]string{"other-t.yaml": tenantBody("other-t")})
	if err := os.Symlink(mem, filepath.Join(dir, "eio-t.yaml")); err != nil {
		t.Fatal(err)
	}
	_, err := NewWriter(dir, dir).Write(context.Background(), "eio-t", "op@example.com", tenantBody("eio-t"))
	if errors.Is(err, ErrTenantTreeUnreadable) {
		t.Fatalf("err = %v: the write's own file was counted against the tree", err)
	}
	if !errors.Is(err, ErrValidation) {
		t.Fatalf("err = %v, want the eol guard's fail-closed ErrValidation", err)
	}
}

// (g) WriteMerged's read of the tenant's OWN file, when that file is a FIFO:
// the Writer refuses it at once (confd.ErrNotRegularFile) instead of
// blocking in the read under the writer lock — it does not rely on the
// handler's earlier refusal. The FIFO is the write's target, so the
// whole-tree verdict does not count it; the read is what must not block.
func TestWriteMerged_TargetFIFODoesNotBlock(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"other-t.yaml": tenantBody("other-t")})
	mkfifoReleased(t, filepath.Join(dir, "fifo-t.yaml"))
	head := gitRevParse(t, dir, "HEAD")
	w := NewWriter(dir, dir)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_, err := w.WriteMerged(ctx, "fifo-t", "op@example.com",
			func([]byte) (string, error) { return tenantBody("fifo-t"), nil })
		done <- err
	}()
	select {
	case err := <-done:
		if !errors.Is(err, confd.ErrNotRegularFile) {
			t.Fatalf("err = %v, want confd.ErrNotRegularFile", err)
		}
	case <-ctx.Done():
		t.Fatal("WriteMerged blocked on the FIFO tenant file")
	}
	if got := gitRevParse(t, dir, "HEAD"); got != head {
		t.Errorf("HEAD moved %s → %s", head, got)
	}
}
