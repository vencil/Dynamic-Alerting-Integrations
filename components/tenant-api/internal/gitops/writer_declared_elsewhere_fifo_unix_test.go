//go:build unix

package gitops

// #2939: a FIFO named *.yaml used to block the declared-elsewhere walk until
// the bound failed the write closed (and every later write, while the walk
// stayed stuck). The walker no longer opens a special file: the walk returns
// at once, and the FIFO is one of its unreadable entries — which refuses the
// write (ADR-036 §4: the tree after it is one the route generator refuses),
// at once, as a store fault (ErrTenantStoreFault: a FIFO is the server's
// storage, not content git holds) rather than the walk's ErrTenantTreeScan.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestTenantWrite_FIFOInTreeRefusedWithoutBlockingTheWalk(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"fifo-t.yaml": tenantBody("fifo-t")})
	fifo := filepath.Join(dir, "stuck.yaml")
	if err := syscall.Mkfifo(fifo, 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	t.Cleanup(func() { // a reader a failing run left on it sees EOF
		if f, err := os.OpenFile(fifo, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
			_ = f.Close()
		}
	})
	w := NewWriter(dir, dir)
	w.treeScanTimeout = 5 * time.Second
	done := make(chan error, 1)
	go func() {
		_, err := w.WriteMerged(context.Background(), "fifo-t", "op@example.com",
			func([]byte) (string, error) { return "tenants:\n  fifo-t:\n    _silent_mode: \"critical\"\n", nil })
		done <- err
	}()
	select {
	case err := <-done:
		if errors.Is(err, ErrTenantTreeScan) {
			t.Fatalf("write err = %v: the walk did not return", err)
		}
		if !errors.Is(err, ErrTenantStoreFault) {
			t.Fatalf("write err = %v, want ErrTenantStoreFault", err)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("write blocked")
	}
	if n := w.stuckTreeScans.Load(); n != 0 {
		t.Errorf("stuckTreeScans = %d, want 0", n)
	}
}
