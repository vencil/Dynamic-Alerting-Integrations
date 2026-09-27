//go:build unix

package gitops

// #2153: a walk abandoned at treeScanTimeout keeps running in its goroutine.
// When it finally returns, its result must not become the prior — the caller
// already failed closed on it, and only a walk handed back to a caller
// qualifies.

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestTreePrior_AbandonedWalkNeverBecomesPrior(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "ab-a.yaml"), tenantBody("ab-a"))
	w := &Writer{configDir: dir, treeScanTimeout: 200 * time.Millisecond}
	good := mustScan(t, w)

	fifo := filepath.Join(dir, "ab-stuck.yaml")
	if err := syscall.Mkfifo(fifo, 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	unblock := func() {
		if f, err := os.OpenFile(fifo, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
			_ = f.Close()
		}
	}
	t.Cleanup(unblock)

	if _, err := w.scanTree(); err == nil {
		t.Fatalf("walk over a blocked FIFO returned in time")
	}
	unblock()
	deadline := time.Now().Add(5 * time.Second)
	for w.stuckTreeScans.Load() != 0 {
		if time.Now().After(deadline) {
			t.Fatalf("stuck walk never drained")
		}
		time.Sleep(10 * time.Millisecond)
	}
	if w.treePrior.Load() != good {
		t.Errorf("the abandoned walk installed its result as the prior")
	}
}
