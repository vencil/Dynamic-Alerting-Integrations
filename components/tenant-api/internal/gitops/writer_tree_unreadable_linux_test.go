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
	"reflect"
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
// once, as a STORE FAULT (D3c: none of these is content git holds), naming
// exactly the entry, and nothing is written. The PR modes judge
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
					assertTreeUnreadable(t, mode, err, []cfg.UnreadableFile{want}, true)
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
			assertTreeUnreadable(t, mode, err, []cfg.UnreadableFile{{RelKey: "eio.yaml", Reason: cfg.UnreadableReadError}}, true)
		})
	}
}

// D3c-1: the write's OWN tenant file being unreadable in a way that is not a
// dangling symlink — a read_error, or a FIFO — refuses the write for EVERY
// caller (TargetUnreadableError, TENANT_CONFIG_NOT_LOADABLE): the file has
// content nobody can read, and replacing it would erase it. The platform-wide
// writer (WithReplaceUnparseable) is refused too. Every direct path and
// preview agrees, and the PR modes for the read_error shape (a committed
// symlink to /proc/self/mem; a FIFO cannot be committed).
func TestTreeUnreadable_UnreadableTargetIsRefusedForEveryCaller(t *testing.T) {
	mem, ok := testutil.ProcSelfMem()
	if !ok {
		t.Skip("no /proc/self/mem that fails to read")
	}
	for _, shape := range []struct {
		name, reason string
		build        func(t *testing.T, target string)
		pr           bool
	}{
		{"read_error", cfg.UnreadableReadError, func(t *testing.T, target string) {
			if err := os.Symlink(mem, target); err != nil {
				t.Fatal(err)
			}
		}, true},
		{"fifo", cfg.UnreadableSpecialFile, func(t *testing.T, target string) { mkfifoReleased(t, target) }, false},
	} {
		modes := append(append([]string{}, directTreeModes...), "Write+platform")
		if shape.pr {
			modes = append(modes, "WritePR", "WritePRBatch")
		}
		for _, mode := range modes {
			t.Run(shape.name+"/"+mode, func(t *testing.T) {
				dir := seedTreeRepo(t, map[string]string{"other-t.yaml": tenantBody("other-t")})
				shape.build(t, filepath.Join(dir, "un-t.yaml"))
				if mode == "WritePR" || mode == "WritePRBatch" {
					gitRun(t, dir, "add", "-A")
					gitRun(t, dir, "commit", "-m", "unreadable tenant file")
				}
				w := NewWriter(dir, dir)
				done := make(chan error, 1)
				go func() {
					if mode == "Write+platform" {
						_, err := w.Write(WithReplaceUnparseable(context.Background()), "un-t", "op@example.com", tenantBody("un-t"))
						done <- err
						return
					}
					done <- treeVerdictVia(t, mode, w, "un-t")
				}()
				var err error
				select {
				case err = <-done:
				case <-time.After(30 * time.Second):
					t.Fatal("blocked")
				}
				var te *TargetUnreadableError
				if !errors.As(err, &te) || te.Reason != shape.reason || te.TenantID != "un-t" {
					t.Fatalf("%s: err = %v, want *TargetUnreadableError (%s)", mode, err, shape.reason)
				}
				if errors.Is(err, ErrTenantTreeUnreadable) || errors.Is(err, ErrTenantStoreFault) {
					t.Errorf("%s: err = %v: the target was also counted against the tree", mode, err)
				}
			})
		}
	}
}

// D3c-2: a store fault and a dangling link elsewhere at once — the store
// fault wins (the server must be repaired first), naming both entries; in a
// batch it fails the batch whole rather than per op.
func TestTreeUnreadable_StoreFaultWinsOverDanglingLink(t *testing.T) {
	for _, mode := range append(append([]string{}, directTreeModes...), "WritePR") {
		t.Run(mode, func(t *testing.T) {
			dir := seedTreeRepo(t, map[string]string{"mx-t.yaml": tenantBody("mx-t")})
			if err := os.Symlink("missing-target.yaml", filepath.Join(dir, "gone.yaml")); err != nil {
				t.Fatal(err)
			}
			mkfifoReleased(t, filepath.Join(dir, "stuck.yaml"))
			w := NewWriter(dir, dir)
			err := treeVerdictVia(t, mode, w, "mx-t")
			assertTreeUnreadable(t, mode, err, []cfg.UnreadableFile{
				{RelKey: "gone.yaml", Reason: cfg.UnreadableStatError},
				{RelKey: "stuck.yaml", Reason: cfg.UnreadableSpecialFile},
			}, true)
			if mode == "WriteBatch" { // treeVerdictVia unwraps a per-op refusal; ask the batch itself
				_, berr := w.WriteBatch(context.Background(), []BatchOp{{TenantID: "mx-t",
					Merge: func([]byte) (string, error) { return tenantBody("mx-t"), nil }}}, "op@example.com")
				var rej *BatchRejectedError
				if errors.As(berr, &rej) || !errors.Is(berr, ErrTenantStoreFault) {
					t.Errorf("WriteBatch: %v, want the batch failed whole with the store fault", berr)
				}
			}
		})
	}
}

// The tenant_api_store_unreadable_entries source: after a serving walk the
// Writer reports that walk's store faults by reason — a dangling link is not
// one — and nothing before its first walk.
func TestWriter_StoreUnreadableCountsTheLastWalk(t *testing.T) {
	mem, ok := testutil.ProcSelfMem()
	if !ok {
		t.Skip("no /proc/self/mem that fails to read")
	}
	dir := seedTreeRepo(t, map[string]string{"cnt-t.yaml": tenantBody("cnt-t")})
	w := NewWriter(dir, dir)
	if _, ok := w.StoreUnreadable(); ok {
		t.Fatal("StoreUnreadable reported counts before any walk")
	}
	mkfifoReleased(t, filepath.Join(dir, "stuck.yaml"))
	if err := os.Symlink(mem, filepath.Join(dir, "eio.yaml")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("missing-target.yaml", filepath.Join(dir, "gone.yaml")); err != nil {
		t.Fatal(err)
	}
	testutil.UnlistableDeepDir(t, dir, 100)
	if _, _, err := w.DryRunValidate(context.Background(), "cnt-t", tenantBody("cnt-t")); !errors.Is(err, ErrTenantStoreFault) {
		t.Fatalf("DryRunValidate: %v, want the store fault", err)
	}
	got, ok := w.StoreUnreadable()
	want := map[string]int{cfg.UnreadableSpecialFile: 1, cfg.UnreadableReadError: 1, cfg.UnreadableWalkError: 1}
	if !ok || !reflect.DeepEqual(got, want) {
		t.Errorf("StoreUnreadable = %v, %v; want %v (the dangling link not counted)", got, ok, want)
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
