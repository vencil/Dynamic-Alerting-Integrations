package platform

import (
	"fmt"
	"sync"
	"testing"
	"time"
)

const batchRefA = BatchBranchPrefix + "20261017-101010"

// #3123 review F1: a batch record registered while the forge call is in flight
// must survive that Sync (the listing may not show the PR yet). Sync used to
// alias the record map it compared against, so such a record was always
// dropped.
func TestSync_KeepsBatchRecordRegisteredDuringForgeCall(t *testing.T) {
	t.Cleanup(ResetMetricsRegistriesForTest)
	var tr *PollingTracker
	tr = NewPollingTracker(func() ([]PRInfo, error) {
		// The forge call is in flight: a batch PR is created and registered.
		tr.RegisterPR(PRInfo{Number: 9, HeadRef: batchRefA, Tenants: []string{"tenant-b1", "tenant-b2"}})
		return nil, nil // the listing does not show it yet
	}, "test", time.Hour)

	tr.Sync()

	tr.mu.RLock()
	got := tr.batchTenants[batchRefA]
	tr.mu.RUnlock()
	if len(got) != 2 {
		t.Fatalf("record registered during the forge call was dropped: batchTenants[%q] = %v", batchRefA, got)
	}

	// Once it was known before a Sync and the listing still lacks it, it goes.
	tr.lister = func() ([]PRInfo, error) { return nil, nil }
	tr.Sync()
	tr.mu.RLock()
	_, still := tr.batchTenants[batchRefA]
	tr.mu.RUnlock()
	if still {
		t.Errorf("record of a batch no longer listed was kept")
	}
}

// A listed batch PR gets back the tenants recorded for it, and the listing's
// branch-derived TenantID is cleared.
func TestSync_ReattachesRecordedBatchTenants(t *testing.T) {
	t.Cleanup(ResetMetricsRegistriesForTest)
	tr := NewPollingTracker(func() ([]PRInfo, error) {
		return []PRInfo{{Number: 9, HeadRef: batchRefA, TenantID: "batch"}}, nil
	}, "test", time.Hour)
	tr.RegisterPR(PRInfo{Number: 9, HeadRef: batchRefA, Tenants: []string{"tenant-b1"}})
	tr.Sync()
	prs := tr.PendingPRs()
	if len(prs) != 1 || prs[0].TenantID != "" || len(prs[0].Tenants) != 1 || prs[0].Tenants[0] != "tenant-b1" {
		t.Errorf("after Sync: %+v, want the batch PR with its recorded tenant and no TenantID", prs)
	}
}

// #3123 review F1: RegisterPR and Sync run concurrently in production (a
// PR-mode batch vs WatchLoop / RefreshNow). Run under -race: the previous
// Sync read the record map outside the lock while RegisterPR wrote it.
func TestSync_ConcurrentRegisterPRIsRaceFree(t *testing.T) {
	t.Cleanup(ResetMetricsRegistriesForTest)
	tr := NewPollingTracker(func() ([]PRInfo, error) {
		return []PRInfo{{Number: 1, HeadRef: batchRefA}}, nil
	}, "test", time.Hour)
	var wg sync.WaitGroup
	for g := 0; g < 4; g++ {
		wg.Add(2)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 200; i++ {
				tr.RegisterPR(PRInfo{Number: i, HeadRef: fmt.Sprintf("%s%d-%d", BatchBranchPrefix, g, i), Tenants: []string{"tenant-r"}})
			}
		}(g)
		go func() {
			defer wg.Done()
			for i := 0; i < 50; i++ {
				tr.Sync()
			}
		}()
	}
	wg.Wait()
}
