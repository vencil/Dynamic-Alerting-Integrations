package gitops

// #2153: a walk abandoned at treeScanTimeout keeps running in its goroutine.
// When it finally returns, its result must not become the prior — the caller
// already failed closed on it, and only a walk handed back to a caller
// qualifies. The walk is held past the bound through the onTreeScan seam
// (it used to be held by a FIFO, which the walker no longer opens, #2939).

import (
	"path/filepath"
	"testing"
	"time"
)

func TestTreePrior_AbandonedWalkNeverBecomesPrior(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "ab-a.yaml"), tenantBody("ab-a"))
	w := &Writer{configDir: dir, treeScanTimeout: 200 * time.Millisecond}
	good := mustScan(t, w)

	_, _, release := holdFirstWalk(t, w)
	if _, err := w.scanTree(); err == nil {
		t.Fatalf("a walk held past the bound returned in time")
	}
	release()
	waitDrained(t, &w.stuckTreeScans)
	if w.treePrior.Load() != good {
		t.Errorf("the abandoned walk installed its result as the prior")
	}
}
