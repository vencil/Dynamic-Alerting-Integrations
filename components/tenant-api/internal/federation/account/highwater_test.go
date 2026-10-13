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
	mark     uint32
	raises   int
	raiseErr error
	onRaise  func(v uint32)
}

func (f *fakeHighWater) Get(context.Context) (uint32, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.mark, nil
}

func (f *fakeHighWater) Raise(_ context.Context, v uint32) (uint32, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.raiseErr != nil {
		return f.mark, f.raiseErr
	}
	if v > f.mark {
		f.mark = v
		f.raises++
		if f.onRaise != nil {
			f.onRaise(v)
		}
	}
	return f.mark, nil
}

func (f *fakeHighWater) get() uint32 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.mark
}

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
		next, mark uint32
		want       hwVerdict
	}{
		{1000, 0, hwOK},      // nothing allocated, no mark
		{1003, 1002, hwOK},   // in step: the mark is the last id
		{1010, 1002, hwOK},   // mark lagging
		{1003, 1003, hwSkip}, // the marked id never reached the registry
		{1002, 1003, hwBehind},
		{1000, 1020, hwBehind}, // registry blank, mark kept (reinstall)
	}
	for _, c := range cases {
		if got := judgeHighWater(c.next, c.mark); got != c.want {
			t.Errorf("judgeHighWater(next=%d, mark=%d) = %d, want %d", c.next, c.mark, got, c.want)
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
	hw.mark = 1004
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
	hw := &fakeHighWater{mark: 1000, raiseErr: errors.New("configmap update: 503")}
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
	hw := &fakeHighWater{mark: 1001}
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
	hw := &fakeHighWater{mark: 0}
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
	hw := &fakeHighWater{mark: 1009}
	fw := &fakeWriter{data: regBytes(t, 1002, map[string]uint32{"t-a": 1000, "t-b": 1001})}

	id, err := NewAllocator(fw, nil).WithHighWater(hw).EnsureAccountID(context.Background(), "t-a", "ops@example.com")
	if err != nil || id != 1000 {
		t.Fatalf("id, err = %d, %v; want 1000, nil", id, err)
	}
}

func TestBackfill_RaisesMarkToTheLastIDAndRefusesWhenBehind(t *testing.T) {
	t.Parallel()
	hw := &fakeHighWater{}
	fw := &fakeWriter{data: regBytes(t, 1000, nil)}
	a := NewAllocator(fw, nil).WithHighWater(hw)
	ctx := context.Background()

	backup := append([]byte(nil), fw.data...)
	if _, err := a.Backfill(ctx, []string{"t-c", "t-a", "t-b", "t-d"}, "ops@example.com"); err != nil {
		t.Fatal(err)
	}
	if hw.get() != 1003 {
		t.Fatalf("mark = %d after backfilling 4 tenants, want 1003", hw.get())
	}

	fw.data = backup
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
		err := VerifyRegistryAgainstHighWater(ctx, s, &fakeHighWater{mark: 1005})
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
		if err := VerifyRegistryAgainstHighWater(ctx, s, &fakeHighWater{mark: 1002}); err != nil {
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
		if err := VerifyRegistryAgainstHighWater(ctx, newMapStore(t, nil), &fakeHighWater{mark: 1020}); err == nil {
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
	if judgeHighWater(m.NextAccountID, 1007) != hwOK {
		t.Fatal("the merged registry would still be refused against the mark")
	}
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
