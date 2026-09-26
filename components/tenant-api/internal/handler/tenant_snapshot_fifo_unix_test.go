//go:build unix

package handler

// Split from tenant_snapshot_writer_test.go: syscall.Mkfifo exists only on
// unix, as in confd/enumerate_fifo_test.go.

import (
	"context"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
)

// S4: a load that never returns (a FIFO named *.yaml: the exporter's walker
// blocks reading it) holds the tree lock only until the deadline. The list
// answers unknown promptly, and a write goes through.
func TestTenantSnapshots_BlockedLoadReleasesTheTreeLock(t *testing.T) {
	t.Parallel()
	work, _ := snapshotRepo(t)
	fifo := filepath.Join(work, "t-fifo.yaml")
	if err := syscall.Mkfifo(fifo, 0o644); err != nil {
		t.Skipf("mkfifo: %v", err)
	}
	t.Cleanup(func() { // unblock the leaked reader: open for write, then close
		if f, err := os.OpenFile(fifo, os.O_WRONLY|syscall.O_NONBLOCK, 0); err == nil {
			_ = f.Close()
		}
	})
	w := gitops.NewWriter(work, work)
	cache := wireTenantSnapshots(w, false)
	const deadline = time.Second
	cache.loadDeadline = deadline
	d := &Deps{ConfigDir: work, RBAC: openModeRBAC(t), SearchCache: cache}

	answered := make(chan *SearchResponse, 1)
	go func() { answered <- openSearch(t, d) }()
	var resp *SearchResponse
	select {
	case resp = <-answered:
	case <-time.After(5 * deadline):
		t.Fatalf("search still blocked after %v with a load deadline of %v", 5*deadline, deadline)
	}
	if resp.ConfigDerivation.LoadError != loadTimeoutError {
		t.Errorf("load_error = %q, want %q", resp.ConfigDerivation.LoadError, loadTimeoutError)
	}
	for _, it := range resp.Items {
		if it.ConfigDerived != nil {
			t.Errorf("derived state from a load that never finished: %+v", it)
		}
	}
	// A second request does not start a second blocked load: while the first
	// is stuck it answers at once instead of waiting out another deadline.
	start := time.Now()
	if again := openSearch(t, d); again.ConfigDerivation.LoadError != loadTimeoutError {
		t.Errorf("second load_error = %q, want %q", again.ConfigDerivation.LoadError, loadTimeoutError)
	}
	if took := time.Since(start); took >= deadline/2 {
		t.Errorf("second request took %v: it started another load behind the stuck one", took)
	}
	writeDone := make(chan error, 1)
	go func() {
		_, err := w.Write(context.Background(), snapTenant, "alice@example.com", snapSilentYAML)
		writeDone <- err
	}()
	select {
	case err := <-writeDone:
		if err != nil {
			t.Errorf("Write: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Write blocked behind a stuck reader load")
	}
}
