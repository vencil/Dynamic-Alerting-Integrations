package gitops

// #2153: the preview callers (Diff, DryRunValidate) take no lock, so their
// #2078 walks go through scanTreeForRead — one shared in-flight walk, bounded
// by a breaker of its own. These tests pin both halves and the line between
// them: readers share, a stuck read walk fails later reads only, and the
// write path neither joins a read walk nor inherits its breaker.
//
// Walks are held open through the per-Writer onTreeScan seam, which runs
// inside the treeScanTimeout bound; joins are observed through
// onReadScanJoin. No test depends on how long a walk takes.

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// holdFirstWalk makes w's first walk block until the returned release is
// called, and closes entered once that walk has started. Later walks pass
// straight through. walks counts every walk started. release is idempotent
// and registered as a cleanup, so a failing test never leaves the walking
// goroutine blocked.
func holdFirstWalk(t *testing.T, w *Writer) (walks *atomic.Int32, entered <-chan struct{}, release func()) {
	t.Helper()
	walks = new(atomic.Int32)
	in := make(chan struct{})
	gate := make(chan struct{})
	var once sync.Once
	release = func() { once.Do(func() { close(gate) }) }
	t.Cleanup(release)
	w.onTreeScan = func() {
		if walks.Add(1) == 1 {
			close(in)
			<-gate
		}
	}
	return walks, in, release
}

// waitDrained waits for the walk counted in stuck to return.
func waitDrained(t *testing.T, stuck *atomic.Int32) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for stuck.Load() != 0 {
		if time.Now().After(deadline) {
			t.Fatalf("stuck walk never drained (count=%d)", stuck.Load())
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// awaitSignal fails the test if ch does not deliver in time; what names it.
func awaitSignal(t *testing.T, ch <-chan struct{}, what string) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(10 * time.Second):
		t.Fatalf("timed out waiting for %s", what)
	}
}

// A read walk that outlives its bound fails that read closed — and nothing
// else. The write that follows walks on its own breaker and goes through.
// (With one breaker shared by both paths, this write was refused with
// errTreeScanStuck until the read walk returned.)
func TestReadScan_StuckReadWalkDoesNotFailWrites(t *testing.T) {
	t.Parallel()
	dir := seedTreeRepo(t, map[string]string{"rs-w.yaml": tenantBody("rs-w")})
	w := NewWriter(dir, dir)
	w.treeScanTimeout = 100 * time.Millisecond
	walks, _, release := holdFirstWalk(t, w)

	_, err := w.Diff("rs-w", tenantBody("rs-w"))
	if !errors.Is(err, ErrTenantTreeScan) || !errors.Is(err, errTreeScanTimeout) {
		t.Fatalf("read over a held walk: err = %v, want ErrTenantTreeScan wrapping the timeout", err)
	}
	if n := w.stuckReadTreeScans.Load(); n != 1 {
		t.Errorf("stuckReadTreeScans = %d, want 1", n)
	}
	// Only the held walk had to outlive the bound; the write's walk must not
	// race it. The held walk's goroutine never reads this field.
	w.treeScanTimeout = time.Minute

	if _, err := w.WriteMerged(context.Background(), "rs-w", "op@example.com",
		func([]byte) (string, error) { return "tenants:\n  rs-w:\n    _silent_mode: \"critical\"\n", nil }); err != nil {
		t.Fatalf("write while a read walk is stuck: %v", err)
	}
	if n := walks.Load(); n != 2 {
		t.Errorf("walks = %d, want 2 (the held read walk, then the write's own)", n)
	}
	if n := w.stuckTreeScans.Load(); n != 0 {
		t.Errorf("stuckTreeScans = %d: the read walk was counted against writes", n)
	}

	release()
	waitDrained(t, &w.stuckReadTreeScans)
}

// Callers that arrive while a read walk is in flight wait for it instead of
// starting their own, get the same scan, and the scan becomes the prior once.
// The next read after it starts a fresh walk.
func TestReadScan_ConcurrentReadsShareOneWalk(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "rs-a.yaml"), tenantBody("rs-a"))
	w := &Writer{configDir: dir, treeScanTimeout: time.Minute}
	walks, entered, release := holdFirstWalk(t, w)
	joined := make(chan struct{}, 8)
	w.onReadScanJoin = func() { joined <- struct{}{} }

	type got struct {
		scan *cfg.TreeScan
		err  error
	}
	leader := make(chan got, 1)
	go func() {
		s, err := w.scanTreeForRead()
		leader <- got{s, err}
	}()
	awaitSignal(t, entered, "the first read walk to start")

	// Joiners of every kind: the walk itself, and both preview callers.
	var wg sync.WaitGroup
	scans := make(chan got, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			s, err := w.scanTreeForRead()
			scans <- got{s, err}
		}()
	}
	var diffErr, dryErr error
	wg.Add(2)
	go func() { defer wg.Done(); _, diffErr = w.Diff("rs-a", tenantBody("rs-a")) }()
	go func() { defer wg.Done(); _, _, dryErr = w.DryRunValidate("rs-a", tenantBody("rs-a")) }()
	for i := 0; i < 4; i++ {
		awaitSignal(t, joined, "a read caller to join the walk in flight")
	}

	release()
	lead := <-leader
	wg.Wait()
	close(scans)
	if lead.err != nil {
		t.Fatalf("shared walk: %v", lead.err)
	}
	for g := range scans {
		if g.err != nil || g.scan != lead.scan {
			t.Errorf("joiner got (%p, %v), want the shared walk's scan %p", g.scan, g.err, lead.scan)
		}
	}
	if diffErr != nil || dryErr != nil {
		t.Errorf("preview callers on the shared walk: Diff err = %v, DryRunValidate err = %v", diffErr, dryErr)
	}
	if n := walks.Load(); n != 1 {
		t.Fatalf("walks = %d for callers that all arrived during one walk, want 1", n)
	}
	if w.treePrior.Load() != lead.scan {
		t.Errorf("the shared read walk did not become the prior")
	}
	wantLocated(t, lead.scan, "rs-a", "rs-a.yaml")

	if _, err := w.scanTreeForRead(); err != nil {
		t.Fatalf("read after the shared walk ended: %v", err)
	}
	if n := walks.Load(); n != 2 {
		t.Errorf("walks = %d, want 2: a read after the shared walk ended must start a new one", n)
	}
}

// A write never takes a read walk's result: while a read walk is held open,
// the write starts and finishes a walk of its own. A walk started before the
// previous write landed would judge this one against a tree already gone.
func TestReadScan_WriteDoesNotJoinReadWalk(t *testing.T) {
	t.Parallel()
	dir := seedTreeRepo(t, map[string]string{"rs-j.yaml": tenantBody("rs-j")})
	w := NewWriter(dir, dir)
	w.treeScanTimeout = time.Minute
	walks, entered, release := holdFirstWalk(t, w)
	var joins atomic.Int32
	w.onReadScanJoin = func() { joins.Add(1) }

	readDone := make(chan error, 1)
	go func() {
		_, err := w.Diff("rs-j", tenantBody("rs-j"))
		readDone <- err
	}()
	awaitSignal(t, entered, "the read walk to start")

	writeDone := make(chan error, 1)
	go func() {
		_, err := w.WriteMerged(context.Background(), "rs-j", "op@example.com",
			func([]byte) (string, error) { return "tenants:\n  rs-j:\n    _silent_mode: \"critical\"\n", nil })
		writeDone <- err
	}()
	select {
	case err := <-writeDone:
		if err != nil {
			t.Fatalf("write while a read walk is in flight: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatalf("write did not finish while the read walk was held: it waited on that walk")
	}
	if n := walks.Load(); n != 2 {
		t.Errorf("walks = %d, want 2 (the held read walk, then the write's own)", n)
	}
	if n := joins.Load(); n != 0 {
		t.Errorf("%d caller(s) joined the read walk; the write must not", n)
	}

	release()
	if err := <-readDone; err != nil {
		t.Errorf("read on the released walk: %v", err)
	}
}

// The read path's breaker: while a read walk is stuck, later reads fail
// closed at once — typed as ErrTenantTreeScan, like a write's — without
// starting another walk behind it; once it returns, reads walk again.
func TestReadScan_StuckReadWalkFailsLaterReadsUntilItReturns(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "rs-b.yaml"), tenantBody("rs-b"))
	w := &Writer{configDir: dir, treeScanTimeout: 100 * time.Millisecond}
	walks, _, release := holdFirstWalk(t, w)

	_, err := w.Diff("rs-b", tenantBody("rs-b"))
	if !errors.Is(err, ErrTenantTreeScan) || !errors.Is(err, errTreeScanTimeout) {
		t.Fatalf("first read: err = %v, want ErrTenantTreeScan wrapping the timeout", err)
	}

	start := time.Now()
	if _, _, err := w.DryRunValidate("rs-b", tenantBody("rs-b")); !errors.Is(err, ErrTenantTreeScan) || !errors.Is(err, errTreeScanStuck) {
		t.Errorf("dry-run while stuck: err = %v, want ErrTenantTreeScan wrapping errTreeScanStuck", err)
	}
	if _, err := w.Diff("rs-b", tenantBody("rs-b")); !errors.Is(err, ErrTenantTreeScan) || !errors.Is(err, errTreeScanStuck) {
		t.Errorf("diff while stuck: err = %v, want ErrTenantTreeScan wrapping errTreeScanStuck", err)
	}
	if took := time.Since(start); took >= w.treeScanTimeout {
		t.Errorf("reads while stuck took %v: they waited instead of failing at once", took)
	}
	if n := walks.Load(); n != 1 {
		t.Errorf("walks = %d, want 1: a read while stuck must not start another walk", n)
	}

	release()
	waitDrained(t, &w.stuckReadTreeScans)
	w.treeScanTimeout = time.Minute // the recovery walk must not race the bound
	if _, err := w.Diff("rs-b", tenantBody("rs-b")); err != nil {
		t.Fatalf("read after the stuck walk returned: %v", err)
	}
	if n := walks.Load(); n != 2 {
		t.Errorf("walks = %d, want 2 after recovery", n)
	}
	if n := w.stuckTreeScans.Load(); n != 0 {
		t.Errorf("stuckTreeScans = %d: the read walk was counted against writes", n)
	}
}
