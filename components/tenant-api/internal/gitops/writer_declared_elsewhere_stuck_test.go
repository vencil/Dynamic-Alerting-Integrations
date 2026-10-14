package gitops

// #2078: the declared-elsewhere guard walks conf.d on every tenant write. A
// walk that never returns (a hung mount) would hang the write, inside the
// single-writer lock on the WriteMerged / PR paths. The walk is bounded
// (scanTree): past the bound the write fails closed with ErrTenantTreeScan,
// later writes fail at once while that walk is still stuck, and writes
// recover once it returns.
//
// The walk is held open through the onTreeScan seam (holdFirstWalk). It used
// to be held by a FIFO named *.yaml, which the walker read and blocked on;
// since #2939 the walker never opens one (see
// writer_declared_elsewhere_fifo_unix_test.go).

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestTenantWrite_BlockedTreeScanFailsClosedAndRecovers(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"stuck-t.yaml": tenantBody("stuck-t")})
	w := NewWriter(dir, dir)
	w.treeScanTimeout = 200 * time.Millisecond
	_, _, release := holdFirstWalk(t, w)
	merge := func([]byte) (string, error) { return tenantBody("stuck-t"), nil }

	start := time.Now()
	_, err := w.WriteMerged(context.Background(), "stuck-t", "op@example.com", merge)
	if !errors.Is(err, ErrTenantTreeScan) || !errors.Is(err, errTreeScanTimeout) {
		t.Fatalf("first write err = %v, want ErrTenantTreeScan wrapping the timeout", err)
	}
	if took := time.Since(start); took > 5*time.Second {
		t.Fatalf("first write took %v with a %v walk bound", took, w.treeScanTimeout)
	}

	// While that walk is still blocked, a second write does not start (and
	// leak) another one behind it.
	start = time.Now()
	_, err = w.WriteMerged(context.Background(), "stuck-t", "op@example.com", merge)
	if !errors.Is(err, ErrTenantTreeScan) || !errors.Is(err, errTreeScanStuck) {
		t.Fatalf("second write err = %v, want ErrTenantTreeScan wrapping errTreeScanStuck", err)
	}
	if took := time.Since(start); took >= w.treeScanTimeout {
		t.Errorf("second write took %v: it waited on a new walk instead of failing at once", took)
	}

	// Release the walk: it returns, the counter drains, and the next write
	// walks the tree and goes through.
	release()
	waitDrained(t, &w.stuckTreeScans)
	if _, err := w.WriteMerged(context.Background(), "stuck-t", "op@example.com",
		func([]byte) (string, error) { return "tenants:\n  stuck-t:\n    _silent_mode: \"critical\"\n", nil }); err != nil {
		t.Fatalf("write after the walk recovered: %v", err)
	}
}
