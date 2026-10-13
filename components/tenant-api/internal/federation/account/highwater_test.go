package account

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/vencil/tenant-api/internal/statestore"
)

// fakeHighWater is an in-memory HighWater. raiseErr makes Raise fail without
// changing the mark; onRaise, when set, runs on every successful Raise.
type fakeHighWater struct {
	mu       sync.Mutex
	mark     Mark
	raises   int
	raiseErr error
	afterGet func(f *fakeHighWater) // runs once, after the first Get, under the lock
}

func (f *fakeHighWater) Get(context.Context) (Mark, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	m := f.mark
	if f.afterGet != nil {
		hook := f.afterGet
		f.afterGet = nil
		hook(f)
	}
	return m, nil
}

func (f *fakeHighWater) Raise(_ context.Context, m Mark) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.raiseErr != nil {
		return false, f.raiseErr
	}
	if m.Max > f.mark.Max {
		f.mark = m
		f.raises++
		return true, nil
	}
	return false, nil
}

func (f *fakeHighWater) get() uint32 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.mark.Max
}

// at is a mark reserved one id at a time (Base == Max).
func at(max uint32) Mark { return Mark{Max: max, Base: max} }

func regBytes(t *testing.T, next uint32, allocs map[string]uint32) []byte {
	t.Helper()
	r := newRegistry()
	r.NextAccountID = next
	for k, v := range allocs {
		r.Allocations[k] = v
	}
	b, err := r.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func parsed(t *testing.T, b []byte) *Registry {
	t.Helper()
	r, err := Parse(b)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func TestJudgeHighWater(t *testing.T) {
	t.Parallel()
	cases := []struct {
		next uint32
		mark Mark
		want hwVerdict
	}{
		{1000, Mark{}, hwOK},                        // nothing allocated, no mark
		{1003, at(1002), hwOK},                      // in step: the mark is the last id
		{1010, at(1002), hwOK},                      // mark lagging
		{1003, at(1003), hwSkip},                    // the marked id never reached the registry
		{1002, at(1003), hwBehind},                  // went back past the base
		{1000, Mark{Max: 1009, Base: 1000}, hwSkip}, // a 10-id batch never reached the registry
		{1004, Mark{Max: 1009, Base: 1000}, hwSkip}, // ...or was rolled back to inside itself
		{999, Mark{Max: 1009, Base: 1000}, hwBehind},
		{1000, Mark{Max: 1020}, hwBehind}, // no base recorded: only the last id may be missing
		{1000, at(1020), hwBehind},        // registry blank, mark kept (reinstall)
	}
	for _, c := range cases {
		if got := judgeHighWater(c.next, c.mark); got != c.want {
			t.Errorf("judgeHighWater(next=%d, mark=%+v) = %d, want %d", c.next, c.mark, got, c.want)
		}
	}
}

// The ticket's acceptance case: an older registry applied while the pod
// runs makes the next allocation fail instead of re-issuing an id.
func TestAllocator_RegistryPutBackWhileRunning_RefusesToReissue(t *testing.T) {
	t.Parallel()
	hw := &fakeHighWater{}
	fw := &fakeWriter{data: regBytes(t, 1000, nil)}
	a := NewAllocator(fw, nil).WithHighWater(hw)
	ctx := context.Background()

	backup := append([]byte(nil), fw.data...)
	for _, tn := range []string{"t-a", "t-b", "t-c"} {
		if _, err := a.EnsureAccountID(ctx, tn, "ops@example.com"); err != nil {
			t.Fatal(err)
		}
	}
	if got := hw.get(); got != 1002 {
		t.Fatalf("mark = %d after allocating 1000..1002, want 1002", got)
	}

	fw.data = backup // `kubectl apply` of the old copy
	_, err := a.EnsureAccountID(ctx, "t-new", "ops@example.com")
	if !errors.Is(err, ErrRegistryBehindHighWater) {
		t.Fatalf("allocation against the put-back registry: err = %v, want ErrRegistryBehindHighWater", err)
	}
	if !bytesEqual(fw.data, backup) {
		t.Fatalf("the refused allocation wrote the registry:\n%s", fw.data)
	}
}

// The mark is written before the registry: at the moment the registry
// write lands, the mark already covers the new id.
func TestAllocator_RaisesMarkBeforeRegistryWrite(t *testing.T) {
	t.Parallel()
	hw := &fakeHighWater{}
	fw := &fakeWriter{data: regBytes(t, 1005, map[string]uint32{"t-a": 1004})}
	hw.mark = at(1004)
	var markAtWrite uint32
	w := &observingWriter{fakeWriter: fw, onCommit: func() { markAtWrite = hw.get() }}

	id, err := NewAllocator(w, nil).WithHighWater(hw).EnsureAccountID(context.Background(), "t-b", "ops@example.com")
	if err != nil {
		t.Fatal(err)
	}
	if id != 1005 {
		t.Fatalf("id = %d, want 1005", id)
	}
	if markAtWrite != 1005 {
		t.Fatalf("mark when the registry write landed = %d, want 1005 (written first)", markAtWrite)
	}
}

// A failed mark write means no registry write: the id is never handed out
// unrecorded.
func TestAllocator_MarkWriteFailure_WritesNothing(t *testing.T) {
	t.Parallel()
	hw := &fakeHighWater{mark: at(1000), raiseErr: errors.New("configmap update: 503")}
	fw := &fakeWriter{data: regBytes(t, 1001, map[string]uint32{"t-a": 1000})}
	before := append([]byte(nil), fw.data...)

	if _, err := NewAllocator(fw, nil).WithHighWater(hw).EnsureAccountID(context.Background(), "t-b", "ops@example.com"); err == nil {
		t.Fatal("allocation succeeded although the mark could not be written")
	}
	if !bytesEqual(fw.data, before) || fw.commits != 0 {
		t.Fatalf("registry written after a failed mark write:\n%s", fw.data)
	}
}

// The mark was written but the registry write failed (or exactly one
// allocation was rolled back): the next allocation skips that id instead of
// refusing, and does not hand it out.
func TestAllocator_MarkOneAhead_SkipsTheMarkedID(t *testing.T) {
	t.Parallel()
	hw := &fakeHighWater{mark: at(1001)}
	fw := &fakeWriter{data: regBytes(t, 1001, map[string]uint32{"t-a": 1000})}

	id, err := NewAllocator(fw, nil).WithHighWater(hw).EnsureAccountID(context.Background(), "t-b", "ops@example.com")
	if err != nil {
		t.Fatal(err)
	}
	if id != 1002 {
		t.Fatalf("id = %d, want 1002 (1001 is recorded by the mark, so it is skipped)", id)
	}
	if r := parsed(t, fw.data); r.NextAccountID != 1003 {
		t.Fatalf("next_account_id = %d, want 1003", r.NextAccountID)
	}
	if hw.get() != 1002 {
		t.Fatalf("mark = %d, want 1002", hw.get())
	}
}

// A mark behind the registry (first start after upgrade, or its ConfigMap
// restored older) is raised — also on the idempotent path, which writes no
// registry.
func TestAllocator_LaggingMarkIsRaised(t *testing.T) {
	t.Parallel()
	hw := &fakeHighWater{}
	fw := &fakeWriter{data: regBytes(t, 1003, map[string]uint32{"t-a": 1000, "t-b": 1001, "t-c": 1002})}

	id, err := NewAllocator(fw, nil).WithHighWater(hw).EnsureAccountID(context.Background(), "t-b", "ops@example.com")
	if err != nil {
		t.Fatal(err)
	}
	if id != 1001 || fw.commits != 0 {
		t.Fatalf("id = %d, commits = %d; want the existing 1001 and no registry write", id, fw.commits)
	}
	if hw.get() != 1002 {
		t.Fatalf("mark = %d, want 1002 (raised to what the registry accounts for)", hw.get())
	}
}

// An existing tenant keeps its id even while the registry is behind: its own
// entry is not what the rollback lost, and refusing it would only take
// tokens away from tenants that are fine.
func TestAllocator_BehindRegistry_ExistingTenantStillResolves(t *testing.T) {
	t.Parallel()
	hw := &fakeHighWater{mark: at(1009)}
	fw := &fakeWriter{data: regBytes(t, 1002, map[string]uint32{"t-a": 1000, "t-b": 1001})}

	id, err := NewAllocator(fw, nil).WithHighWater(hw).EnsureAccountID(context.Background(), "t-a", "ops@example.com")
	if err != nil || id != 1000 {
		t.Fatalf("id, err = %d, %v; want 1000, nil", id, err)
	}
}

// A batch is one reservation: the mark goes to its last id, based on the
// registry the batch was planned from. A registry put back to before an
// earlier, committed allocation is refused.
func TestBackfill_RaisesMarkToTheLastIDAndRefusesWhenBehind(t *testing.T) {
	t.Parallel()
	hw := &fakeHighWater{}
	fw := &fakeWriter{data: regBytes(t, 1000, nil)}
	a := NewAllocator(fw, nil).WithHighWater(hw)
	ctx := context.Background()

	backup := append([]byte(nil), fw.data...)
	if _, err := a.EnsureAccountID(ctx, "t-0", "ops@example.com"); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Backfill(ctx, []string{"t-c", "t-a", "t-b", "t-d"}, "ops@example.com"); err != nil {
		t.Fatal(err)
	}
	if got := hw.mark; got != (Mark{Max: 1004, Base: 1001}) {
		t.Fatalf("mark after backfilling 4 tenants onto next=1001 = %+v, want {1004 1001}", got)
	}

	fw.data = backup // next=1000: before t-0's committed allocation
	if _, err := a.Backfill(ctx, []string{"t-e"}, "ops@example.com"); !errors.Is(err, ErrRegistryBehindHighWater) {
		t.Fatalf("backfill against the put-back registry: err = %v, want ErrRegistryBehindHighWater", err)
	}
}

func TestVerifyRegistryAgainstHighWater(t *testing.T) {
	t.Parallel()
	ctx := context.Background()

	t.Run("behind refuses to start", func(t *testing.T) {
		t.Parallel()
		s := newMapStore(t, map[string][]byte{RegistryFileName: regBytes(t, 1002, map[string]uint32{"t-a": 1000, "t-b": 1001})})
		err := VerifyRegistryAgainstHighWater(ctx, s, &fakeHighWater{mark: at(1005)})
		if err == nil {
			t.Fatal("started against a registry 4 allocations behind the mark")
		}
	})
	t.Run("lagging mark is raised", func(t *testing.T) {
		t.Parallel()
		s := newMapStore(t, map[string][]byte{RegistryFileName: regBytes(t, 1002, map[string]uint32{"t-a": 1000, "t-b": 1001})})
		hw := &fakeHighWater{}
		if err := VerifyRegistryAgainstHighWater(ctx, s, hw); err != nil {
			t.Fatal(err)
		}
		if hw.get() != 1001 {
			t.Fatalf("mark = %d, want 1001", hw.get())
		}
	})
	t.Run("one ahead starts; the next allocation skips", func(t *testing.T) {
		t.Parallel()
		s := newMapStore(t, map[string][]byte{RegistryFileName: regBytes(t, 1002, map[string]uint32{"t-a": 1000, "t-b": 1001})})
		if err := VerifyRegistryAgainstHighWater(ctx, s, &fakeHighWater{mark: at(1002)}); err != nil {
			t.Fatal(err)
		}
	})
	t.Run("fresh install records nothing", func(t *testing.T) {
		t.Parallel()
		hw := &fakeHighWater{}
		if err := VerifyRegistryAgainstHighWater(ctx, newMapStore(t, nil), hw); err != nil {
			t.Fatal(err)
		}
		if hw.raises != 0 {
			t.Fatalf("a registry with nothing allocated wrote a mark (%d)", hw.get())
		}
	})
	t.Run("blank registry with a kept mark refuses", func(t *testing.T) {
		t.Parallel()
		if err := VerifyRegistryAgainstHighWater(ctx, newMapStore(t, nil), &fakeHighWater{mark: at(1020)}); err == nil {
			t.Fatal("started from the floor although ids up to 1020 were allocated")
		}
	})
}

func TestMergeForRestore_GoesAboveTheHighWaterMark(t *testing.T) {
	t.Parallel()
	bak := regBytes(t, 1002, map[string]uint32{"t-a": 1000, "t-b": 1001})
	m, rep, err := MergeForRestore(bak, bak, nil, RestoreLimits{HighWater: 1007})
	if err != nil {
		t.Fatal(err)
	}
	if m.NextAccountID != 1008 || !rep.Changed() {
		t.Fatalf("next = %d (changed %v), want 1008: ids up to the mark were handed out", m.NextAccountID, rep.Changed())
	}
	if judgeHighWater(m.NextAccountID, at(1007)) != hwOK {
		t.Fatal("the merged registry would still be refused against the mark")
	}
}

// A Backfill batch whose registry write fails leaves the mark several ids
// ahead. The batch is one reservation, so the next allocation skips it whole
// — and startup does not refuse — instead of wedging until a restore.
func TestBackfill_FailedRegistryWrite_IsSkippedNotWedged(t *testing.T) {
	t.Parallel()
	hw := &fakeHighWater{}
	fw := &failingWriter{fakeWriter: &fakeWriter{data: regBytes(t, 1000, nil)}, fail: errors.New("update: etcdserver timeout")}
	ctx := context.Background()

	if _, err := NewAllocator(fw, nil).WithHighWater(hw).Backfill(ctx, []string{"t-a", "t-b", "t-c"}, "ops@example.com"); err == nil {
		t.Fatal("backfill reported success although the registry write failed")
	}
	if got := hw.get(); got != 1002 {
		t.Fatalf("mark = %d, want 1002 (reserved before the failed write)", got)
	}

	fw.fail = nil
	s := newMapStore(t, map[string][]byte{RegistryFileName: fw.data})
	if err := VerifyRegistryAgainstHighWater(ctx, s, hw); err != nil {
		t.Fatalf("startup refused after a failed batch write: %v", err)
	}
	id, err := NewAllocator(fw, nil).WithHighWater(hw).EnsureAccountID(ctx, "t-d", "ops@example.com")
	if err != nil {
		t.Fatal(err)
	}
	if id != 1003 {
		t.Fatalf("id = %d, want 1003 (1000..1002 were reserved, so they are skipped)", id)
	}
}

// A compare-and-swap retry of the same call — another record in the same
// ConfigMap changed, the registry did not — keeps the plan: no id skipped.
func TestAllocator_RetryOfTheSameCall_SkipsNothing(t *testing.T) {
	t.Parallel()
	data := regBytes(t, 1002, map[string]uint32{"t-a": 1000, "t-b": 1001})
	hw := &fakeHighWater{mark: at(1001)}
	fw := &fakeWriter{data: data, rerunOnce: append([]byte(nil), data...)}

	id, err := NewAllocator(fw, nil).WithHighWater(hw).EnsureAccountID(context.Background(), "t-c", "ops@example.com")
	if err != nil {
		t.Fatal(err)
	}
	if id != 1002 {
		t.Fatalf("id = %d, want 1002 (the retry found its own reservation and kept it)", id)
	}
	if r := parsed(t, fw.data); r.NextAccountID != 1003 {
		t.Fatalf("next_account_id = %d, want 1003", r.NextAccountID)
	}
}

// Another replica reserves the same id between this call's read of the mark
// and its own Raise, commits, and then the registry is put back. The retry
// must not take the identical mark for its own reservation and hand the id
// out again.
func TestAllocator_SomeoneElsesIdenticalMarkIsNotOurs(t *testing.T) {
	t.Parallel()
	reg := regBytes(t, 1005, map[string]uint32{"t-a": 1000, "t-b": 1001, "t-c": 1002, "t-d": 1003, "t-e": 1004})
	hw := &fakeHighWater{mark: at(1004), afterGet: func(f *fakeHighWater) { f.mark = at(1005) }}
	fw := &fakeWriter{data: reg, rerunOnce: append([]byte(nil), reg...)}

	id, err := NewAllocator(fw, nil).WithHighWater(hw).EnsureAccountID(context.Background(), "t-new", "ops@example.com")
	if err != nil {
		t.Fatal(err)
	}
	if id == 1005 {
		t.Fatal("handed out 1005, which another replica reserved and may have issued")
	}
	if id != 1006 {
		t.Fatalf("id = %d, want 1006", id)
	}
}

// A failed batch, then a failed single allocation, then a third: the third
// succeeds. The second reservation keeps the registry's own counter as its
// base, so two failures in a row are still read as "never reached the
// registry", not as a rollback.
func TestAllocator_TwoFailedWritesInARow_ThirdSucceeds(t *testing.T) {
	t.Parallel()
	hw := &fakeHighWater{}
	fw := &failingWriter{fakeWriter: &fakeWriter{data: regBytes(t, 1000, nil)}, fail: errors.New("update: etcdserver timeout")}
	a := NewAllocator(fw, nil).WithHighWater(hw)
	ctx := context.Background()

	if _, err := a.Backfill(ctx, []string{"t-a", "t-b", "t-c"}, "op"); err == nil {
		t.Fatal("backfill succeeded")
	}
	if _, err := a.EnsureAccountID(ctx, "t-d", "op"); err == nil {
		t.Fatal("allocation succeeded")
	}
	fw.fail = nil
	id, err := a.EnsureAccountID(ctx, "t-e", "op")
	if err != nil {
		t.Fatalf("third allocation after two failed writes: %v", err)
	}
	if id != 1004 {
		t.Fatalf("id = %d, want 1004 (1000..1003 were reserved by the failed writes)", id)
	}
}

// failingWriter fails every registry write with fail while it is set.
type failingWriter struct {
	*fakeWriter
	fail error
}

func (f *failingWriter) Mutate(ctx context.Context, key string, m statestore.Meta, t statestore.Transform) (bool, error) {
	if f.fail == nil {
		return f.fakeWriter.Mutate(ctx, key, m, t)
	}
	if _, err := t(f.data); err != nil {
		return false, err
	}
	return false, f.fail
}

// observingWriter calls onCommit right before a registry write lands.
type observingWriter struct {
	*fakeWriter
	onCommit func()
}

func (o *observingWriter) Mutate(ctx context.Context, key string, m statestore.Meta, t statestore.Transform) (bool, error) {
	return o.fakeWriter.Mutate(ctx, key, m, func(cur []byte) ([]byte, error) {
		next, err := t(cur)
		if err == nil && next != nil {
			o.onCommit()
		}
		return next, err
	})
}

func bytesEqual(a, b []byte) bool { return string(a) == string(b) }

// newMapStore is a directory store holding recs.
func newMapStore(t *testing.T, recs map[string][]byte) statestore.Store {
	t.Helper()
	s, err := statestore.NewDir(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	for k, v := range recs {
		if _, err := s.Mutate(context.Background(), k, statestore.Meta{Entity: "test"},
			func([]byte) ([]byte, error) { return v, nil }); err != nil {
			t.Fatal(err)
		}
	}
	return s
}

// The worked example in ADR-021 ("單調性靠什麼守"), number for number.
func TestADR021Example(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	backup := regBytes(t, 1001, map[string]uint32{"t-a": 1000})

	// Live: t-b allocated after the backup, so the mark is {1001, base 1001}.
	hw := &fakeHighWater{}
	fw := &fakeWriter{data: append([]byte(nil), backup...)}
	a := NewAllocator(fw, nil).WithHighWater(hw)
	if id, err := a.EnsureAccountID(ctx, "t-b", "op"); err != nil || id != 1001 {
		t.Fatalf("t-b = %d, %v", id, err)
	}
	if hw.mark != (Mark{Max: 1001, Base: 1001}) {
		t.Fatalf("mark = %+v, want {1001 1001}", hw.mark)
	}
	current := append([]byte(nil), fw.data...)

	// kubectl apply of the backup: t-c skips 1001.
	fw.data = append([]byte(nil), backup...)
	if id, err := a.EnsureAccountID(ctx, "t-c", "op"); err != nil || id != 1002 {
		t.Fatalf("after the put-back t-c = %d, %v; want 1002", id, err)
	}

	// state restore instead (default margin 10000): t-b kept, next 11001.
	m, _, err := MergeForRestore(current, backup, nil, RestoreLimits{HighWater: 1001, Margin: DefaultRestoreMargin(2)})
	if err != nil {
		t.Fatal(err)
	}
	if id, ok := m.Lookup("t-b"); !ok || id != 1001 || m.NextAccountID != 11001 {
		t.Fatalf("restored: t-b = %d (%v), next = %d; want t-b 1001, next 11001", id, ok, m.NextAccountID)
	}
	merged, _ := m.Marshal()
	fw.data = merged
	if id, err := a.EnsureAccountID(ctx, "t-c2", "op"); err != nil || id != 11001 {
		t.Fatalf("after the restore t-c = %d, %v; want 11001", id, err)
	}
}
