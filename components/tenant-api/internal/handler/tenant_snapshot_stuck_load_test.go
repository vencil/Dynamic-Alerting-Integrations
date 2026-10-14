package handler

import (
	"sync"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
)

// S4: a load that never returns (a hung mount) holds the tree lock only
// until the deadline. The list answers unknown promptly, and the lock is
// free. The load is held open through the cache's afterLoad seam, which runs
// inside the bounded load; it used to be held by a FIFO named *.yaml, which
// the exporter's walker no longer opens (#2939).
func TestTenantSnapshots_BlockedLoadReleasesTheTreeLock(t *testing.T) {
	t.Parallel()
	work, _ := snapshotRepo(t)
	w := gitops.NewWriter(work, work)
	cache := wireTenantSnapshots(w, false)
	const deadline = time.Second
	cache.loadDeadline = deadline
	gate := make(chan struct{})
	var once sync.Once
	release := func() { once.Do(func() { close(gate) }) }
	t.Cleanup(release)
	cache.afterLoad = func() { <-gate }
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
	// TryWithTreeLock is the probe: a write that takes the tree lock without
	// walking conf.d (#2856). It reports false when anything holds the lock.
	if !w.TryWithTreeLock(func() {}) {
		t.Fatal("the writer lock is held behind a stuck reader load")
	}
}
