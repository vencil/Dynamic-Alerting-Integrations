package account

import (
	"context"
	"sort"
	"sync"
	"testing"
)

// fakeWriter is an in-memory RegistryWriter: it holds the "committed"
// registry bytes and serialises MutateConfigFile under its own mutex,
// exactly as the real gitops.Writer serialises on w.mu. This lets the
// allocator's allocate-in-lock concurrency be tested with NO git shell-out
// (the real git path is exercised by the gitops package's own tests).
type fakeWriter struct {
	mu      sync.Mutex
	data    []byte // current committed bytes (nil = file absent)
	commits int    // number of actual writes (no-op transforms don't count)
}

func (f *fakeWriter) MutateConfigFile(_ context.Context, _, _, _ string, transform func([]byte) ([]byte, error)) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	next, err := transform(f.data)
	if err != nil {
		return err
	}
	if next == nil {
		return nil // no change → no commit (matches the real writer)
	}
	f.data = next
	f.commits++
	return nil
}

func TestAllocator_EnsureAllocatesAndPersists(t *testing.T) {
	t.Parallel()
	fw := &fakeWriter{}
	a := NewAllocator(fw)

	id, err := a.EnsureAccountID(context.Background(), "tenant-alpha", "ops@example.com")
	if err != nil {
		t.Fatalf("EnsureAccountID: %v", err)
	}
	if id != FirstTenantAccountID {
		t.Errorf("first id = %d, want %d", id, FirstTenantAccountID)
	}
	if fw.commits != 1 {
		t.Errorf("commits = %d, want 1", fw.commits)
	}
	// The committed bytes round-trip to a registry holding the allocation.
	reg, err := Parse(fw.data)
	if err != nil {
		t.Fatalf("Parse committed: %v", err)
	}
	if got, ok := reg.Lookup("tenant-alpha"); !ok || got != id {
		t.Errorf("committed registry lookup = (%d, %v), want (%d, true)", got, ok, id)
	}
}

func TestAllocator_EnsureIdempotentNoSecondCommit(t *testing.T) {
	t.Parallel()
	fw := &fakeWriter{}
	a := NewAllocator(fw)
	ctx := context.Background()

	first, err := a.EnsureAccountID(ctx, "tenant-x", "ops@example.com")
	if err != nil {
		t.Fatalf("first ensure: %v", err)
	}
	again, err := a.EnsureAccountID(ctx, "tenant-x", "ops@example.com")
	if err != nil {
		t.Fatalf("second ensure: %v", err)
	}
	if again != first {
		t.Errorf("idempotent ensure id = %d, want stable %d", again, first)
	}
	if fw.commits != 1 {
		t.Errorf("commits = %d, want 1 (second ensure must not re-commit)", fw.commits)
	}
}

// TestAllocator_ConcurrentEnsureNeverDuplicates is the core race test: many
// concurrent onboardings of DISTINCT tenants must each get a unique id with
// no gaps and no collisions, because the writer serialises the read-modify-
// write. (No t.Parallel — this test itself spawns the concurrency.)
func TestAllocator_ConcurrentEnsureNeverDuplicates(t *testing.T) {
	fw := &fakeWriter{}
	a := NewAllocator(fw)
	ctx := context.Background()

	const n = 50
	ids := make([]uint32, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			id, err := a.EnsureAccountID(ctx, tenantName(i), "ops@example.com")
			if err != nil {
				t.Errorf("ensure %d: %v", i, err)
				return
			}
			ids[i] = id
		}(i)
	}
	wg.Wait()

	// Every id is distinct.
	seen := map[uint32]bool{}
	for _, id := range ids {
		if seen[id] {
			t.Fatalf("duplicate id %d across concurrent onboardings — allocation raced", id)
		}
		seen[id] = true
	}
	// The id set is exactly the contiguous block [floor, floor+n).
	sorted := append([]uint32(nil), ids...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	for i, id := range sorted {
		if want := FirstTenantAccountID + uint32(i); id != want {
			t.Errorf("sorted id[%d] = %d, want contiguous %d", i, id, want)
		}
	}
	// The committed counter advanced exactly n past the floor.
	reg, err := Parse(fw.data)
	if err != nil {
		t.Fatalf("Parse committed: %v", err)
	}
	if reg.NextAccountID != FirstTenantAccountID+n {
		t.Errorf("NextAccountID = %d, want %d", reg.NextAccountID, FirstTenantAccountID+n)
	}
}

func TestAllocator_BackfillAllocatesMissingOnly(t *testing.T) {
	t.Parallel()
	fw := &fakeWriter{}
	a := NewAllocator(fw)
	ctx := context.Background()

	// Pre-allocate one tenant lazily.
	if _, err := a.EnsureAccountID(ctx, "existing", "ops@example.com"); err != nil {
		t.Fatalf("seed ensure: %v", err)
	}

	res, err := a.Backfill(ctx, []string{"existing", "new-b", "new-a"}, "ops@example.com")
	if err != nil {
		t.Fatalf("Backfill: %v", err)
	}
	if res.AlreadyPresent != 1 {
		t.Errorf("AlreadyPresent = %d, want 1", res.AlreadyPresent)
	}
	// Allocation order is sorted tenant-id order: new-a before new-b.
	want := []string{"new-a", "new-b"}
	if len(res.Allocated) != len(want) {
		t.Fatalf("Allocated = %v, want %v", res.Allocated, want)
	}
	for i := range want {
		if res.Allocated[i] != want[i] {
			t.Errorf("Allocated[%d] = %q, want %q", i, res.Allocated[i], want[i])
		}
	}

	reg, _ := Parse(fw.data)
	// existing kept 1000; new-a/new-b got 1001/1002 in sorted order.
	if id, _ := reg.Lookup("existing"); id != FirstTenantAccountID {
		t.Errorf("existing id = %d, want %d", id, FirstTenantAccountID)
	}
	if id, _ := reg.Lookup("new-a"); id != FirstTenantAccountID+1 {
		t.Errorf("new-a id = %d, want %d", id, FirstTenantAccountID+1)
	}
	if id, _ := reg.Lookup("new-b"); id != FirstTenantAccountID+2 {
		t.Errorf("new-b id = %d, want %d", id, FirstTenantAccountID+2)
	}
}

func TestAllocator_BackfillIdempotent(t *testing.T) {
	t.Parallel()
	fw := &fakeWriter{}
	a := NewAllocator(fw)
	ctx := context.Background()
	tenants := []string{"a", "b", "c"}

	if _, err := a.Backfill(ctx, tenants, "ops@example.com"); err != nil {
		t.Fatalf("first backfill: %v", err)
	}
	commitsAfterFirst := fw.commits

	res, err := a.Backfill(ctx, tenants, "ops@example.com")
	if err != nil {
		t.Fatalf("second backfill: %v", err)
	}
	if len(res.Allocated) != 0 {
		t.Errorf("second backfill allocated %v, want none", res.Allocated)
	}
	if res.AlreadyPresent != len(tenants) {
		t.Errorf("AlreadyPresent = %d, want %d", res.AlreadyPresent, len(tenants))
	}
	if fw.commits != commitsAfterFirst {
		t.Errorf("idempotent backfill committed again: %d → %d", commitsAfterFirst, fw.commits)
	}
}

// TestAllocator_EnsureFailsOnCorruptRegistry: a committed registry the pure
// core rejects (counter below an allocated id) must surface as an error,
// not a silent re-issue.
func TestAllocator_EnsureFailsOnCorruptRegistry(t *testing.T) {
	t.Parallel()
	fw := &fakeWriter{data: []byte("schema_version: v1\nnext_account_id: 1000\nallocations:\n  a: 1005\n")}
	a := NewAllocator(fw)
	if _, err := a.EnsureAccountID(context.Background(), "b", "ops@example.com"); err == nil {
		t.Fatal("EnsureAccountID should fail on a corrupt registry")
	}
	if fw.commits != 0 {
		t.Errorf("corrupt registry must not commit; commits = %d", fw.commits)
	}
}

func tenantName(i int) string {
	const letters = "abcdefghijklmnopqrstuvwxyz"
	return "tenant-" + string(letters[i%26]) + string(letters[(i/26)%26])
}
