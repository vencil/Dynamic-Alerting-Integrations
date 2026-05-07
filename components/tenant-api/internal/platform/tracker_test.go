// Tests for PollingTracker — the shared cache+poll engine that github.Tracker
// and gitlab.Tracker both wrap. Audit §5 listed PollingTracker as the #1 Go
// critical-path coverage gap: race conditions or stale-cache bugs hit BOTH
// providers at once. The provider-specific tracker_test.go files exercise
// these paths transitively, but this file pins the contract directly.
package platform

import (
	"errors"
	"sync"
	"testing"
	"time"
)

// fixedLister returns the same PR list every call.
func fixedLister(prs []PRInfo) Lister {
	return func() ([]PRInfo, error) {
		// Defensive copy so callers can mutate without affecting future returns.
		out := make([]PRInfo, len(prs))
		copy(out, prs)
		return out, nil
	}
}

// erroringLister always returns the given error.
func erroringLister(err error) Lister {
	return func() ([]PRInfo, error) {
		return nil, err
	}
}

// countingLister wraps a Lister with a call counter (useful for poll-cadence
// tests). Returns the wrapped lister + a snapshot func.
func countingLister(prs []PRInfo) (Lister, func() int) {
	var mu sync.Mutex
	count := 0
	lister := func() ([]PRInfo, error) {
		mu.Lock()
		count++
		mu.Unlock()
		return prs, nil
	}
	snapshot := func() int {
		mu.Lock()
		defer mu.Unlock()
		return count
	}
	return lister, snapshot
}

// ---------------------------------------------------------------------------
// NewPollingTracker — interval clamping
// ---------------------------------------------------------------------------

func TestNewPollingTracker_BelowFloorClampedToDefault(t *testing.T) {
	t.Parallel()
	tk := NewPollingTracker(fixedLister(nil), "github", 1*time.Second)
	if got := tk.SyncInterval(); got != defaultSyncInterval {
		t.Errorf("interval = %v, want %v (clamped from 1s)", got, defaultSyncInterval)
	}
}

func TestNewPollingTracker_ZeroIntervalClamped(t *testing.T) {
	t.Parallel()
	tk := NewPollingTracker(fixedLister(nil), "github", 0)
	if got := tk.SyncInterval(); got != defaultSyncInterval {
		t.Errorf("interval = %v, want %v (clamped from 0)", got, defaultSyncInterval)
	}
}

func TestNewPollingTracker_AboveFloorPreserved(t *testing.T) {
	t.Parallel()
	tk := NewPollingTracker(fixedLister(nil), "gitlab", 1*time.Hour)
	if got := tk.SyncInterval(); got != 1*time.Hour {
		t.Errorf("interval = %v, want 1h (above floor, no clamp)", got)
	}
}

func TestNewPollingTracker_ExactFloorPreserved(t *testing.T) {
	t.Parallel()
	// 10s is the floor — equal-to should NOT trigger the clamp.
	tk := NewPollingTracker(fixedLister(nil), "github", minSyncInterval)
	if got := tk.SyncInterval(); got != minSyncInterval {
		t.Errorf("interval = %v, want %v (exact floor preserved)", got, minSyncInterval)
	}
}

func TestNewPollingTracker_PendingPRsStartsEmpty(t *testing.T) {
	t.Parallel()
	tk := NewPollingTracker(fixedLister(nil), "github", 1*time.Hour)
	if got := tk.PendingPRs(); len(got) != 0 {
		t.Errorf("PendingPRs() = %v, want empty before first Sync", got)
	}
	if !tk.LastSyncTime().IsZero() {
		t.Error("LastSyncTime() should be zero before first Sync")
	}
}

// ---------------------------------------------------------------------------
// Sync — cache replacement + tenant indexing
// ---------------------------------------------------------------------------

func TestSync_PopulatesCache(t *testing.T) {
	t.Parallel()
	prs := []PRInfo{
		{Number: 1, TenantID: "db-a", State: "open"},
		{Number: 2, TenantID: "db-b", State: "open"},
	}
	tk := NewPollingTracker(fixedLister(prs), "github", 1*time.Hour)
	tk.Sync()
	if got := len(tk.PendingPRs()); got != 2 {
		t.Errorf("len(PendingPRs()) = %d, want 2", got)
	}
	if !tk.HasPendingPR("db-a") || !tk.HasPendingPR("db-b") {
		t.Error("expected both tenants to be tracked")
	}
}

func TestSync_SkipsPRsWithoutTenantID(t *testing.T) {
	t.Parallel()
	prs := []PRInfo{
		{Number: 1, TenantID: "db-a"},
		{Number: 2, TenantID: ""}, // no tenant — not in byTenant index
	}
	tk := NewPollingTracker(fixedLister(prs), "github", 1*time.Hour)
	tk.Sync()
	// Both PRs are in pendingPRs (raw cache), but only db-a is in byTenant.
	if got := len(tk.PendingPRs()); got != 2 {
		t.Errorf("PendingPRs preserves untagged entries; got %d, want 2", got)
	}
	if tk.HasPendingPR("") {
		t.Error("empty-tenant should not be queryable via HasPendingPR")
	}
}

func TestSync_KeepsHighestNumberPerTenant(t *testing.T) {
	t.Parallel()
	prs := []PRInfo{
		{Number: 5, TenantID: "db-a", Title: "newer"},
		{Number: 1, TenantID: "db-a", Title: "older"},
		{Number: 3, TenantID: "db-a", Title: "middle"},
	}
	tk := NewPollingTracker(fixedLister(prs), "github", 1*time.Hour)
	tk.Sync()
	got, ok := tk.PendingPRForTenant("db-a")
	if !ok {
		t.Fatal("expected db-a to be tracked")
	}
	// Highest Number wins (PR numbers / MR IIDs are monotonic).
	if got.Number != 5 {
		t.Errorf("kept PR number = %d, want 5 (highest)", got.Number)
	}
	if got.Title != "newer" {
		t.Errorf("kept PR title = %q, want 'newer'", got.Title)
	}
}

func TestSync_LastSyncTimeAdvances(t *testing.T) {
	t.Parallel()
	tk := NewPollingTracker(fixedLister(nil), "github", 1*time.Hour)
	before := time.Now()
	tk.Sync()
	after := time.Now()
	got := tk.LastSyncTime()
	if got.Before(before) || got.After(after) {
		t.Errorf("LastSyncTime() = %v, want between %v and %v", got, before, after)
	}
}

func TestSync_ListerErrorKeepsStaleData(t *testing.T) {
	t.Parallel()
	prs := []PRInfo{{Number: 1, TenantID: "db-a"}}
	// First sync populates from fixedLister.
	tk := NewPollingTracker(fixedLister(prs), "github", 1*time.Hour)
	tk.Sync()
	if !tk.HasPendingPR("db-a") {
		t.Fatal("setup: db-a should be cached after first Sync")
	}

	// Swap to an erroring lister; subsequent Sync MUST keep stale data.
	tk.lister = erroringLister(errors.New("network down"))
	syncedAt := tk.LastSyncTime()
	tk.Sync()
	if !tk.HasPendingPR("db-a") {
		t.Error("error path dropped cache; tracker should serve stale data on lister error")
	}
	// LastSyncTime should NOT have advanced (Sync returned early).
	if tk.LastSyncTime() != syncedAt {
		t.Error("LastSyncTime advanced despite lister error; should only update on success")
	}
}

func TestSync_ReplacesEntireCache(t *testing.T) {
	t.Parallel()
	tk := NewPollingTracker(fixedLister([]PRInfo{
		{Number: 1, TenantID: "db-a"},
		{Number: 2, TenantID: "db-b"},
	}), "github", 1*time.Hour)
	tk.Sync()

	// Swap lister to return a different set; second Sync REPLACES.
	tk.lister = fixedLister([]PRInfo{
		{Number: 3, TenantID: "db-c"},
	})
	tk.Sync()

	// db-a / db-b are gone (closed PRs no longer appear in lister).
	if tk.HasPendingPR("db-a") || tk.HasPendingPR("db-b") {
		t.Error("Sync should REPLACE cache, not merge — closed PRs must drop")
	}
	if !tk.HasPendingPR("db-c") {
		t.Error("Sync should add new tenants from latest lister result")
	}
}

// ---------------------------------------------------------------------------
// PendingPRs — defensive copy contract
// ---------------------------------------------------------------------------

func TestPendingPRs_ReturnsDefensiveCopy(t *testing.T) {
	t.Parallel()
	tk := NewPollingTracker(fixedLister([]PRInfo{
		{Number: 1, TenantID: "db-a"},
	}), "github", 1*time.Hour)
	tk.Sync()

	// Mutate the returned slice — tracker's internal cache must NOT reflect it.
	out := tk.PendingPRs()
	out[0].Number = 999
	out[0].TenantID = "stolen"

	fresh := tk.PendingPRs()
	if fresh[0].Number != 1 {
		t.Errorf("internal cache mutated by caller: Number = %d, want 1", fresh[0].Number)
	}
	if fresh[0].TenantID != "db-a" {
		t.Errorf("internal cache mutated by caller: TenantID = %q, want db-a", fresh[0].TenantID)
	}
}

// ---------------------------------------------------------------------------
// PendingPRForTenant / HasPendingPR
// ---------------------------------------------------------------------------

func TestPendingPRForTenant_NotFoundReturnsFalse(t *testing.T) {
	t.Parallel()
	tk := NewPollingTracker(fixedLister(nil), "github", 1*time.Hour)
	tk.Sync()
	_, ok := tk.PendingPRForTenant("ghost")
	if ok {
		t.Error("ghost tenant should not be tracked")
	}
}

func TestHasPendingPR_FoundReturnsTrue(t *testing.T) {
	t.Parallel()
	tk := NewPollingTracker(fixedLister([]PRInfo{
		{Number: 1, TenantID: "db-a"},
	}), "github", 1*time.Hour)
	tk.Sync()
	if !tk.HasPendingPR("db-a") {
		t.Error("expected db-a to be tracked after Sync")
	}
}

// ---------------------------------------------------------------------------
// RegisterPR — pre-poll cache injection
// ---------------------------------------------------------------------------

func TestRegisterPR_AddsNewTenant(t *testing.T) {
	t.Parallel()
	tk := NewPollingTracker(fixedLister(nil), "github", 1*time.Hour)
	tk.RegisterPR(PRInfo{Number: 42, TenantID: "fresh-tenant"})
	if !tk.HasPendingPR("fresh-tenant") {
		t.Error("RegisterPR should make tenant queryable")
	}
	if got := len(tk.PendingPRs()); got != 1 {
		t.Errorf("len(PendingPRs()) after RegisterPR = %d, want 1", got)
	}
}

func TestRegisterPR_ReplacesSameTenant(t *testing.T) {
	t.Parallel()
	tk := NewPollingTracker(fixedLister(nil), "github", 1*time.Hour)
	tk.RegisterPR(PRInfo{Number: 1, TenantID: "db-a", Title: "old"})
	tk.RegisterPR(PRInfo{Number: 2, TenantID: "db-a", Title: "new"})

	got, _ := tk.PendingPRForTenant("db-a")
	if got.Number != 2 {
		t.Errorf("tenant entry Number = %d, want 2 (replaced)", got.Number)
	}
	if got.Title != "new" {
		t.Errorf("tenant entry Title = %q, want 'new' (replaced)", got.Title)
	}
	// pendingPRs slice should also have just one db-a entry, not two.
	if got := len(tk.PendingPRs()); got != 1 {
		t.Errorf("len(PendingPRs()) = %d, want 1 (no duplicates after replace)", got)
	}
}

func TestRegisterPR_EmptyTenantAppendsToPendingButNotByTenant(t *testing.T) {
	t.Parallel()
	tk := NewPollingTracker(fixedLister(nil), "github", 1*time.Hour)
	tk.RegisterPR(PRInfo{Number: 7, TenantID: ""})
	if got := len(tk.PendingPRs()); got != 1 {
		t.Errorf("len(PendingPRs()) = %d, want 1 (empty-tenant still in raw cache)", got)
	}
	if tk.HasPendingPR("") {
		t.Error("empty tenant should not be queryable via HasPendingPR")
	}
}

func TestRegisterPR_MultipleDifferentTenants(t *testing.T) {
	t.Parallel()
	tk := NewPollingTracker(fixedLister(nil), "github", 1*time.Hour)
	tk.RegisterPR(PRInfo{Number: 1, TenantID: "db-a"})
	tk.RegisterPR(PRInfo{Number: 2, TenantID: "db-b"})
	tk.RegisterPR(PRInfo{Number: 3, TenantID: "db-c"})

	if got := len(tk.PendingPRs()); got != 3 {
		t.Errorf("len(PendingPRs()) = %d, want 3", got)
	}
	for _, tenant := range []string{"db-a", "db-b", "db-c"} {
		if !tk.HasPendingPR(tenant) {
			t.Errorf("%s should be tracked", tenant)
		}
	}
}

// ---------------------------------------------------------------------------
// WatchLoop — initial sync + ticker + stopCh
// ---------------------------------------------------------------------------

func TestWatchLoop_RunsInitialSync(t *testing.T) {
	t.Parallel()
	prs := []PRInfo{{Number: 1, TenantID: "db-a"}}
	lister, count := countingLister(prs)
	// Long syncInterval so the ticker doesn't fire during the test;
	// only the initial Sync should run before we close stopCh.
	tk := NewPollingTracker(lister, "github", 1*time.Hour)

	stopCh := make(chan struct{})
	done := make(chan struct{})
	go func() {
		tk.WatchLoop(stopCh)
		close(done)
	}()

	// Wait for initial sync to land. Poll the cache to detect completion.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && !tk.HasPendingPR("db-a") {
		time.Sleep(5 * time.Millisecond)
	}
	if !tk.HasPendingPR("db-a") {
		t.Fatal("initial Sync did not run within 2s")
	}

	close(stopCh)
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("WatchLoop did not exit after stopCh close")
	}

	// At least one Sync call happened (the initial one).
	if got := count(); got < 1 {
		t.Errorf("lister call count = %d, want >= 1", got)
	}
}

func TestWatchLoop_TickerTriggersAdditionalSyncs(t *testing.T) {
	t.Parallel()
	lister, count := countingLister(nil)
	// Bypass the constructor's clamp so we can exercise the ticker
	// quickly without waiting 30s. This is safe because we're testing
	// the LOOP behaviour, not the clamp logic.
	tk := &PollingTracker{
		lister:       lister,
		provider:     "github",
		byTenant:     map[string]PRInfo{},
		syncInterval: 50 * time.Millisecond,
	}

	stopCh := make(chan struct{})
	done := make(chan struct{})
	go func() {
		tk.WatchLoop(stopCh)
		close(done)
	}()

	// Wait long enough for at least 2 ticks (initial sync + 1+ ticker).
	time.Sleep(180 * time.Millisecond)
	close(stopCh)
	select {
	case <-done:
	case <-time.After(1 * time.Second):
		t.Fatal("WatchLoop did not exit after stopCh close")
	}

	// Initial sync (1) + at least one ticker fire (>=2 total).
	if got := count(); got < 2 {
		t.Errorf("lister call count = %d, want >= 2 (initial + ticker)", got)
	}
}

func TestWatchLoop_StopChanTerminatesPromptly(t *testing.T) {
	t.Parallel()
	tk := &PollingTracker{
		lister:       fixedLister(nil),
		provider:     "github",
		byTenant:     map[string]PRInfo{},
		syncInterval: 1 * time.Hour, // never tick
	}

	stopCh := make(chan struct{})
	done := make(chan struct{})
	go func() {
		tk.WatchLoop(stopCh)
		close(done)
	}()

	// Initial sync should land quickly; then we close stopCh and expect
	// WatchLoop to exit within a short window.
	close(stopCh)
	select {
	case <-done:
	case <-time.After(500 * time.Millisecond):
		t.Fatal("WatchLoop did not honour stopCh within 500ms")
	}
}
