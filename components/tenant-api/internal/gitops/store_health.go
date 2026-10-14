package gitops

// The tenant_api_store_unreadable_entries gauge's source (#2939 D3c, R4-N4).
//
// The gauge must be true while nobody writes — an alert that only moves
// when someone tries a write misses an idle fault and never clears after a
// repair — so it does not reuse the write path's walks: StoreUnreadable walks
// conf.d itself, at scrape time, classification only
// (cfg.ScanDirTreeFiles: no tenant file is parsed; a special file is
// recorded, never opened, so the walk cannot block on a FIFO). Two
// safeguards keep scrapes cheap and safe:
//
//   - a minimum interval (storeScanMinInterval, 30s): a scrape within it of
//     the last completed walk gets that walk's counts;
//   - the walk is bounded (boundedcall, treeScanTimeout) with its own stuck
//     breaker, so a walk blocked on a hung mount fails later scrapes fast —
//     they report the last counts — and never touches the write or read
//     paths' breakers.

import (
	"sync"
	"sync/atomic"
	"time"

	"github.com/vencil/tenant-api/internal/boundedcall"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// defaultStoreScanMinInterval is the gauge walk's minimum interval.
const defaultStoreScanMinInterval = 30 * time.Second

// storeScanCache is the gauge walk's state on a Writer.
type storeScanCache struct {
	lock   sync.Mutex     // not "mu": that name is the tree lock (TestTreeLock_OnlyTheHelpersLockDirectly)
	at     time.Time      // when the last completed walk ran; zero before the first
	counts map[string]int // that walk's StoreFaultCounts
	stuck  atomic.Int32   // the gauge walk's own breaker

	// minInterval and now are per-Writer TEST-ONLY seams: 0 / nil mean
	// defaultStoreScanMinInterval and time.Now. A test sets a fake clock
	// instead of sleeping through the interval.
	minInterval time.Duration
	now         func() time.Time
}

// StoreUnreadable returns the store faults of conf.d (StoreFaultCounts) by
// the walker's reason, walking it when the last walk is older than the
// minimum interval; ok is false only when no walk has completed yet (the
// first one failed or timed out). The map is the caller's.
func (w *Writer) StoreUnreadable() (counts map[string]int, ok bool) {
	c := &w.storeScan
	c.lock.Lock()
	defer c.lock.Unlock()
	now := time.Now
	if c.now != nil {
		now = c.now
	}
	interval := c.minInterval
	if interval <= 0 {
		interval = defaultStoreScanMinInterval
	}
	if c.at.IsZero() || now().Sub(c.at) >= interval {
		if fresh, err := w.scanStoreFaults(); err == nil {
			c.counts, c.at = fresh, now()
		}
	}
	if c.at.IsZero() {
		return nil, false
	}
	out := make(map[string]int, len(c.counts))
	for k, v := range c.counts {
		out[k] = v
	}
	return out, true
}

// scanStoreFaults is the gauge's bounded, classification-only walk.
func (w *Writer) scanStoreFaults() (map[string]int, error) {
	timeout := w.treeScanTimeout
	if timeout <= 0 {
		timeout = defaultTreeScanTimeout
	}
	type result struct {
		counts map[string]int
		err    error
	}
	r, err := boundedcall.Run(timeout, &w.storeScan.stuck, func() result {
		s, err := cfg.ScanDirTreeFiles(w.configDir, discardScanLogger)
		if err != nil {
			return result{err: err}
		}
		return result{counts: StoreFaultCounts(s)}
	})
	if err != nil {
		return nil, err
	}
	return r.counts, r.err
}
