// Tests for PollingTracker — the shared cache+poll engine that github.Tracker
// and gitlab.Tracker both wrap. Audit §5 listed PollingTracker as the #1 Go
// critical-path coverage gap: race conditions or stale-cache bugs hit BOTH
// providers at once. The provider-specific tracker_test.go files exercise
// these paths transitively, but this file pins the contract directly.
package platform

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jonboulle/clockwork"
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

// signalingLister wraps a Lister with both a counter AND a signal channel
// that emits AFTER each call. Used by deterministic ticker tests that
// need to wait for the WatchLoop goroutine to have finished consuming a
// fakeClock.Advance() before asserting on call count. Buffered so the
// lister never blocks on a slow-consumer test.
func signalingLister(prs []PRInfo) (lister Lister, snapshot func() int, done <-chan struct{}) {
	var mu sync.Mutex
	count := 0
	signal := make(chan struct{}, 64) // buffer = many ticks even if test is slow
	lister = func() ([]PRInfo, error) {
		mu.Lock()
		count++
		mu.Unlock()
		signal <- struct{}{}
		return prs, nil
	}
	snapshot = func() int {
		mu.Lock()
		defer mu.Unlock()
		return count
	}
	done = signal
	return lister, snapshot, done
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
	// TRK-011 deeper: this test was a documented flake on GH-hosted runners
	// (CI run 25602108441 saw "lister call count = 1, want >= 2"). The old
	// version slept 180ms hoping for at least one 50ms ticker fire — under
	// CPU steal / GC pauses on shared runners, the sleep could return before
	// any tick processed. Replaced with clockwork.FakeClock + per-call
	// signal channel so each tick is fired AND awaited deterministically.
	lister, count, syncDone := signalingLister(nil)
	clock := clockwork.NewFakeClock()
	// Bypass the constructor's interval clamp + plug in the fake clock.
	// Safe because we're testing LOOP behaviour, not the clamp logic.
	tk := &PollingTracker{
		lister:       lister,
		provider:     "github",
		clock:        clock,
		byTenant:     map[string]PRInfo{},
		syncInterval: 50 * time.Millisecond,
	}

	stopCh := make(chan struct{})
	done := make(chan struct{})
	go func() {
		tk.WatchLoop(stopCh)
		close(done)
	}()

	// 1. Wait for the initial Sync (called before the ticker starts).
	waitForSync(t, syncDone, "initial sync")

	// 2. Wait for the WatchLoop goroutine to register its ticker on the
	//    fake clock. Without this, an early Advance() can fire before the
	//    ticker exists and be swallowed.
	clock.BlockUntil(1)

	// 3. Advance the fake clock by syncInterval to fire one tick, then
	//    wait for the goroutine to consume it (signaled by the lister
	//    being called for the second time).
	clock.Advance(50 * time.Millisecond)
	waitForSync(t, syncDone, "sync after tick 1")

	// 4. Advance again — proves the ticker is repeating, not single-shot.
	clock.Advance(50 * time.Millisecond)
	waitForSync(t, syncDone, "sync after tick 2")

	close(stopCh)
	select {
	case <-done:
	case <-time.After(1 * time.Second):
		t.Fatal("WatchLoop did not exit after stopCh close")
	}

	// Initial + 2 deterministic tick-driven syncs = exactly 3.
	if got := count(); got != 3 {
		t.Errorf("lister call count = %d, want 3 (initial + 2 ticks)", got)
	}
}

// waitForSync drains one signal from the signalingLister's done channel
// or fails fast with a contextual message. The 2-second budget is generous
// — fakeClock.Advance is synchronous and the goroutine should consume the
// tick within microseconds; the deadline only protects against a regression
// that breaks the signaling contract.
func waitForSync(t *testing.T, done <-chan struct{}, label string) {
	t.Helper()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatalf("timed out waiting for %s (lister never called)", label)
	}
}

func TestWatchLoop_StopChanTerminatesPromptly(t *testing.T) {
	t.Parallel()
	tk := &PollingTracker{
		lister:       fixedLister(nil),
		provider:     "github",
		clock:        clockwork.NewRealClock(), // 1h interval — clock identity doesn't matter
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

// --- ClaimTenant / ReleaseClaim (dedup atomicity) ---

func newClaimTracker() *PollingTracker {
	return NewPollingTracker(func() ([]PRInfo, error) { return nil, nil }, "test", time.Minute)
}

func TestClaimTenant_BasicLifecycle(t *testing.T) {
	t.Parallel()
	tr := newClaimTracker()

	if !tr.ClaimTenant("db-a") {
		t.Fatal("first claim should succeed")
	}
	if tr.ClaimTenant("db-a") {
		t.Error("second claim for same tenant should fail while claim is held")
	}
	if !tr.ClaimTenant("db-b") {
		t.Error("claim for a different tenant should succeed")
	}

	// Releasing frees the tenant for a fresh claim (e.g. after a failed create).
	tr.ReleaseClaim("db-a")
	if !tr.ClaimTenant("db-a") {
		t.Error("claim should succeed again after release")
	}
}

func TestClaimTenant_BlockedByRegisteredPR(t *testing.T) {
	t.Parallel()
	tr := newClaimTracker()
	tr.RegisterPR(PRInfo{Number: 1, TenantID: "db-a", State: "open"})

	if tr.ClaimTenant("db-a") {
		t.Error("claim should fail when a PR is already registered for the tenant")
	}
}

// TestRegisterPR_ClearsClaim guards the lifecycle hazard: if RegisterPR left
// the synchronous claim in place, the tenant would stay blocked forever once
// the PR later merged out of byTenant (Sync drops it, but a stale claim would
// remain). After register+merge, a new write must be claimable again.
func TestRegisterPR_ClearsClaim(t *testing.T) {
	t.Parallel()
	tr := newClaimTracker()

	if !tr.ClaimTenant("db-a") {
		t.Fatal("initial claim should succeed")
	}
	tr.RegisterPR(PRInfo{Number: 7, TenantID: "db-a", State: "open"})

	// Simulate the PR merging: a poll Sync that no longer lists it drops it
	// from byTenant. (Sync replaces the cache wholesale.)
	tr.Sync() // lister returns nil → byTenant cleared

	if !tr.ClaimTenant("db-a") {
		t.Error("tenant should be claimable again after its PR merged out of cache")
	}
}

// TestClaimTenant_ConcurrentSingleWinner is the core TOCTOU assertion: under
// N goroutines racing to claim the same tenant, exactly one wins. This is the
// race the issue flagged (check-then-RegisterPR window); the synchronous
// check-and-set under the write lock closes it. Run with -race.
func TestClaimTenant_ConcurrentSingleWinner(t *testing.T) {
	t.Parallel()
	tr := newClaimTracker()

	const goroutines = 64
	var wins atomic.Int32
	var wg sync.WaitGroup
	start := make(chan struct{})
	wg.Add(goroutines)
	for i := 0; i < goroutines; i++ {
		go func() {
			defer wg.Done()
			<-start // line everyone up so the claims truly contend
			if tr.ClaimTenant("db-a") {
				wins.Add(1)
			}
		}()
	}
	close(start)
	wg.Wait()

	if got := wins.Load(); got != 1 {
		t.Errorf("exactly one goroutine should win the claim, got %d", got)
	}
}

// TestRefreshNow_PopulatesCache_HappyPath: RefreshNow runs Sync and the cache is
// observably updated by the time it returns (#644).
func TestRefreshNow_PopulatesCache_HappyPath(t *testing.T) {
	pt := NewPollingTracker(
		fixedLister([]PRInfo{{Number: 1, TenantID: "db-a", State: "open"}}),
		"test", 30*time.Second,
	)
	pt.RefreshNow(context.Background())
	if !pt.HasPendingPR("db-a") {
		t.Errorf("after RefreshNow, expected HasPendingPR(db-a) = true")
	}
}

// TestRefreshNow_BoundedByCtx_DoesNotBlockOnSlowLister: a slow lister must NOT
// extend RefreshNow past the ctx deadline (#644 — the whole point of the bound:
// a degraded forge must not extend the 409 response latency). The Sync continues
// in the background; the test waits for it before returning so the goroutine
// does not leak past the test.
func TestRefreshNow_BoundedByCtx_DoesNotBlockOnSlowLister(t *testing.T) {
	unblock := make(chan struct{})
	listerReturned := make(chan struct{})
	lister := func() ([]PRInfo, error) {
		<-unblock
		close(listerReturned)
		return nil, nil
	}
	pt := NewPollingTracker(lister, "test", 30*time.Second)

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	start := time.Now()
	pt.RefreshNow(ctx)
	elapsed := time.Since(start)

	if elapsed > 500*time.Millisecond {
		t.Errorf("RefreshNow took %v, want ~100ms (ctx-bounded — slow lister must NOT extend it)", elapsed)
	}
	// Let the background Sync drain so it does not outlive the test.
	close(unblock)
	<-listerReturned
}

// TestRefreshNow_DedupConcurrentCalls_OneListerCall is the #644 thundering-herd
// regression (Gemini review): N simultaneous same-tenant 409s must collapse to
// exactly ONE underlying Sync→Lister call, not N. Without the in-flight dedup
// in RefreshNow, this would fire N concurrent ListOpenPRs at the forge and trip
// the secondary/abuse rate limit.
func TestRefreshNow_DedupConcurrentCalls_OneListerCall(t *testing.T) {
	t.Parallel()
	// Block the lister briefly so all N goroutines pile up before any completes.
	gate := make(chan struct{})
	var calls atomic.Int32
	lister := func() ([]PRInfo, error) {
		calls.Add(1)
		<-gate
		return nil, nil
	}
	pt := NewPollingTracker(lister, "test", 30*time.Second)

	const n = 20
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			pt.RefreshNow(context.Background())
		}()
	}
	close(start)
	// Give all N goroutines a moment to reach the dedup guard before unblocking.
	time.Sleep(50 * time.Millisecond)
	close(gate)
	wg.Wait()

	if got := calls.Load(); got != 1 {
		t.Errorf("lister called %d times, want exactly 1 (N concurrent RefreshNow must dedup to one Sync)", got)
	}
}

// TestSync_CountsMergeConflicts verifies Sync surfaces conflicting PRs/MRs to
// the ConflictSnapshot for the /metrics gauge (#646). A clean tenant + a
// conflicting tenant → conflict count 1; a later all-clean sync → 0.
func TestSync_CountsMergeConflicts(t *testing.T) {
	// Not parallel: asserts on the package-level conflict registry, keyed by
	// a provider name unique to this test to avoid cross-test interference.
	const provider = "conflict-test-provider"

	conflicted := []PRInfo{
		{Number: 1, TenantID: "tenant-clean", HeadRef: "tenant-api/tenant-clean/1", Mergeable: MergeableOK},
		{Number: 2, TenantID: "tenant-bad", HeadRef: "tenant-api/tenant-bad/1", Mergeable: MergeableConflict},
	}
	pt := NewPollingTracker(fixedLister(conflicted), provider, time.Hour)
	pt.Sync()
	if got := ConflictSnapshot()[provider]; got != 1 {
		t.Errorf("conflict count after mixed sync = %d, want 1", got)
	}

	// Conflict resolved out-of-band → next sync reports 0 (state-coded gauge).
	clean := []PRInfo{
		{Number: 1, TenantID: "tenant-clean", HeadRef: "tenant-api/tenant-clean/1", Mergeable: MergeableOK},
		{Number: 2, TenantID: "tenant-bad", HeadRef: "tenant-api/tenant-bad/1", Mergeable: MergeableOK},
	}
	pt2 := NewPollingTracker(fixedLister(clean), provider, time.Hour)
	pt2.Sync()
	if got := ConflictSnapshot()[provider]; got != 0 {
		t.Errorf("conflict count after clean sync = %d, want 0 (must clear)", got)
	}
}
