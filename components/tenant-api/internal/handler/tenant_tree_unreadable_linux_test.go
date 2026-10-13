//go:build linux

package handler

// #2939 (b) (d) and read_error through the handlers: a FIFO named like a
// config file, a symlink to a file whose read fails (/proc/self/mem: EIO
// even for root) and a directory past PATH_MAX (testutil.UnlistableDeepDir:
// unlistable even for root) each refuse PUT with 409 and a batch with 422,
// the op carrying TENANT_TREE_UNREADABLE — at once, nothing written. The
// batch used to fail its staging copy on the FIFO with a 500; the shapes
// that fail for root are why this is Linux only. Seams: none.

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/testutil"
)

func TestTreeUnreadable_LinuxShapes_PutAndBatch(t *testing.T) {
	t.Parallel()
	for _, shape := range []struct {
		name  string
		build func(t *testing.T, dir string) string // returns the entry's conf.d-relative name
	}{
		{"fifo", func(t *testing.T, dir string) string {
			p := filepath.Join(dir, "stuck.yaml")
			if err := syscall.Mkfifo(p, 0o644); err != nil {
				t.Skipf("mkfifo: %v", err)
			}
			t.Cleanup(func() { // release a reader a failing run left on it
				if f, err := os.OpenFile(p, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
					_ = f.Close()
				}
			})
			return "stuck.yaml"
		}},
		{"read_error", func(t *testing.T, dir string) string {
			mem, ok := testutil.ProcSelfMem()
			if !ok {
				t.Skip("no /proc/self/mem that fails to read")
			}
			if err := os.Symlink(mem, filepath.Join(dir, "eio.yaml")); err != nil {
				t.Fatal(err)
			}
			return "eio.yaml"
		}},
		{"walk_error", func(t *testing.T, dir string) string {
			return testutil.UnlistableDeepDir(t, dir, 100)
		}},
	} {
		for _, group := range []string{"platform", "team,readers"} {
			t.Run(shape.name+"/"+group, func(t *testing.T) {
				t.Parallel()
				f := newTreeFixture(t)
				entry := shape.build(t, f.dir)
				visible := group == "platform"
				head := f.head(t)
				start := time.Now()
				assertTreeRefusal(t, f.put(t, group, "tu-a"), entry, visible)
				assertBatchTreeRefusal(t, f.batch(t, group, "tu-a"), entry, visible)
				if took := time.Since(start); took > 10*time.Second {
					t.Errorf("answered after %v: a walk or read blocked", took)
				}
				if got := f.head(t); got != head {
					t.Errorf("HEAD moved %s → %s", head, got)
				}
			})
		}
	}
}
