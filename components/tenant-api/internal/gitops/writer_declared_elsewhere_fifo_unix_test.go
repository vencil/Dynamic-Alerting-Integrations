//go:build unix

package gitops

// #2078: the declared-elsewhere guard walks conf.d on every tenant write. A
// file whose read never returns (a FIFO named *.yaml) would hang that walk —
// and with it the write, inside the single-writer lock on the WriteMerged /
// PR paths. The walk is bounded (scanTree): past the bound the write fails
// closed with ErrTenantTreeScan, later writes fail at once while that walk is
// still stuck, and writes recover once it returns.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestTenantWrite_BlockedTreeScanFailsClosedAndRecovers(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"fifo-t.yaml": tenantBody("fifo-t")})
	fifo := filepath.Join(dir, "stuck.yaml")
	if err := syscall.Mkfifo(fifo, 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	unblock := func() { // open for write, then close: the blocked reader sees EOF
		if f, err := os.OpenFile(fifo, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
			_ = f.Close()
		}
	}
	t.Cleanup(unblock)

	w := NewWriter(dir, dir)
	w.treeScanTimeout = 200 * time.Millisecond
	merge := func([]byte) (string, error) { return tenantBody("fifo-t"), nil }

	start := time.Now()
	_, err := w.WriteMerged(context.Background(), "fifo-t", "op@example.com", merge)
	if !errors.Is(err, ErrTenantTreeScan) || !errors.Is(err, errTreeScanTimeout) {
		t.Fatalf("first write err = %v, want ErrTenantTreeScan wrapping the timeout", err)
	}
	if took := time.Since(start); took > 5*time.Second {
		t.Fatalf("first write took %v with a %v walk bound", took, w.treeScanTimeout)
	}

	// While that walk is still blocked, a second write does not start (and
	// leak) another one behind the same FIFO.
	start = time.Now()
	_, err = w.WriteMerged(context.Background(), "fifo-t", "op@example.com", merge)
	if !errors.Is(err, ErrTenantTreeScan) || !errors.Is(err, errTreeScanStuck) {
		t.Fatalf("second write err = %v, want ErrTenantTreeScan wrapping errTreeScanStuck", err)
	}
	if took := time.Since(start); took >= w.treeScanTimeout {
		t.Errorf("second write took %v: it waited on a new walk instead of failing at once", took)
	}

	// Release the FIFO and remove it: the stuck walk returns, the counter
	// drains, and the next write walks a healthy tree and goes through.
	unblock()
	deadline := time.Now().Add(5 * time.Second)
	for w.stuckTreeScans.Load() != 0 {
		if time.Now().After(deadline) {
			t.Fatalf("stuck walk never drained (stuckTreeScans=%d)", w.stuckTreeScans.Load())
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err := os.Remove(fifo); err != nil {
		t.Fatal(err)
	}
	if _, err := w.WriteMerged(context.Background(), "fifo-t", "op@example.com",
		func([]byte) (string, error) { return "tenants:\n  fifo-t:\n    _silent_mode: \"critical\"\n", nil }); err != nil {
		t.Fatalf("write after the walk recovered: %v", err)
	}
}
