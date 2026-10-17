package handler

// #1535 item 3: a state-store write can be applied although the caller hears
// an error — the ConfigMap update reached the API server and its response was
// lost. Before the fix only the success path re-read the in-memory snapshot,
// so for up to one reload period after such a 503 a GET did not show what was
// written and a DELETE of it answered 404. mutateState now reloads by outcome:
// synchronously on success, not at all on an error that proves nothing was
// written, and in the background (the error answer does not wait) when the
// outcome is unknown.

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/statestore"
	"github.com/vencil/tenant-api/internal/views"
)

// appliedThenFailedStore applies every Mutate to the store it wraps and then,
// while fail is set, reports it as failed — the shape of a ConfigMap update
// whose response never came back.
type appliedThenFailedStore struct {
	statestore.Store
	fail atomic.Bool
}

func (s *appliedThenFailedStore) Mutate(ctx context.Context, key string, m statestore.Meta, t statestore.Transform) (bool, error) {
	changed, err := s.Store.Mutate(ctx, key, m, t)
	if err == nil && s.fail.Load() {
		return changed, fmt.Errorf("%w: update %s: response lost after the server applied it", statestore.ErrUnavailable, key)
	}
	return changed, err
}

func stateReq(method, path, id, body string) *http.Request {
	req := newRequestWithChiParam(method, path, "id", id, bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	setRequestIdentity(req, "test@example.com")
	return req
}

func wantCode(t *testing.T, what string, got, want int, body string) {
	t.Helper()
	if got != want {
		t.Fatalf("%s: status = %d, want %d; body: %s", what, got, want, body)
	}
}

// eventuallyTimeout bounds how long a background reload may take to show.
const eventuallyTimeout = 2 * time.Second

// wantCodeEventually polls get until it answers want: after an error whose
// outcome is unknown the reload runs in the background, so the snapshot
// converges shortly after the error answer rather than before it.
func wantCodeEventually(t *testing.T, what string, get func() int, want int) {
	t.Helper()
	deadline := time.Now().Add(eventuallyTimeout)
	for {
		got := get()
		if got == want {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("%s: status = %d after %s, want %d", what, got, eventuallyTimeout, want)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestGroupWrite_ErrorAfterApplyStillReloads(t *testing.T) {
	t.Parallel()
	store := &appliedThenFailedStore{Store: mustDirState(t.TempDir())}
	d := &Deps{State: store, Groups: groups.NewManager(store), RBAC: permissiveRBACManager(t)}
	const id = "g-one"
	put := func() int {
		w := executeWithRBAC(t, PutGroup(d), stateReq("PUT", "/api/v1/groups/"+id, id, `{"label":"G","members":["t-one"]}`))
		return w.Code
	}
	get := func() int {
		return executeWithRBAC(t, GetGroup(d), stateReq("GET", "/api/v1/groups/"+id, id, "")).Code
	}
	del := func() int {
		return executeWithRBAC(t, DeleteGroup(d), stateReq("DELETE", "/api/v1/groups/"+id, id, "")).Code
	}

	// PUT applied, answered 503: GET soon sees it, and DELETE finds it.
	store.fail.Store(true)
	wantCode(t, "PUT (applied, then failed)", put(), http.StatusServiceUnavailable, "")
	wantCodeEventually(t, "GET after the failed-but-applied PUT", get, http.StatusOK)
	store.fail.Store(false)
	wantCode(t, "DELETE of the group that PUT wrote", del(), http.StatusOK, "")
	wantCode(t, "GET after the DELETE", get(), http.StatusNotFound, "")

	// The success path still reloads.
	wantCode(t, "PUT", put(), http.StatusOK, "")
	wantCode(t, "GET after a successful PUT", get(), http.StatusOK, "")

	// DELETE applied, answered 503: GET soon no longer shows the group.
	store.fail.Store(true)
	wantCode(t, "DELETE (applied, then failed)", del(), http.StatusServiceUnavailable, "")
	wantCodeEventually(t, "GET after the failed-but-applied DELETE", get, http.StatusNotFound)
}

func TestViewWrite_ErrorAfterApplyStillReloads(t *testing.T) {
	t.Parallel()
	store := &appliedThenFailedStore{Store: mustDirState(t.TempDir())}
	d := &Deps{State: store, Views: views.NewManager(store)}
	const id = "v-one"
	put := func() int {
		return executeWithRBAC(t, PutView(d), stateReq("PUT", "/api/v1/views/"+id, id, `{"label":"V","filters":{"environment":"staging"}}`)).Code
	}
	get := func() int {
		return executeWithRBAC(t, GetView(d), stateReq("GET", "/api/v1/views/"+id, id, "")).Code
	}
	del := func() int {
		return executeWithRBAC(t, DeleteView(d), stateReq("DELETE", "/api/v1/views/"+id, id, "")).Code
	}

	store.fail.Store(true)
	wantCode(t, "PUT (applied, then failed)", put(), http.StatusServiceUnavailable, "")
	wantCodeEventually(t, "GET after the failed-but-applied PUT", get, http.StatusOK)
	store.fail.Store(false)
	wantCode(t, "DELETE of the view that PUT wrote", del(), http.StatusOK, "")
	wantCode(t, "GET after the DELETE", get(), http.StatusNotFound, "")

	wantCode(t, "PUT", put(), http.StatusOK, "")
	wantCode(t, "GET after a successful PUT", get(), http.StatusOK, "")

	store.fail.Store(true)
	wantCode(t, "DELETE (applied, then failed)", del(), http.StatusServiceUnavailable, "")
	wantCodeEventually(t, "GET after the failed-but-applied DELETE", get, http.StatusNotFound)
}

// countingObserver counts reloads (configwatcher.ReloadObserver): every
// Reload records one outcome, a no-op one included.
type countingObserver struct{ n atomic.Int64 }

func (o *countingObserver) RecordReload(string, bool) { o.n.Add(1) }

// gatedStore wraps a store for the mutateState tests below. Mutate either
// applies the write (failWith nil) or, without touching the store, returns
// failWith — a write that was not applied, whatever the error claims. Once
// armed, Get (the reload's read) signals readStarted and then waits for
// release, so a test can hold a reload in flight.
type gatedStore struct {
	statestore.Store
	failWith    error
	armed       atomic.Bool
	readStarted chan struct{}
	release     chan struct{}

	// writes counts Mutate calls; seen records, for each armed read, how
	// many writes had been attempted when it started.
	writes atomic.Int64
	mu     sync.Mutex
	seen   []int64
}

func newGatedStore(t *testing.T, failWith error) *gatedStore {
	s := &gatedStore{
		Store:       mustDirState(t.TempDir()),
		failWith:    failWith,
		readStarted: make(chan struct{}, 64),
		release:     make(chan struct{}),
	}
	return s
}

func (s *gatedStore) Get(ctx context.Context, key string) ([]byte, bool, error) {
	if s.armed.Load() {
		s.mu.Lock()
		s.seen = append(s.seen, s.writes.Load())
		s.mu.Unlock()
		s.readStarted <- struct{}{}
		<-s.release
	}
	return s.Store.Get(ctx, key)
}

func (s *gatedStore) Mutate(ctx context.Context, key string, m statestore.Meta, t statestore.Transform) (bool, error) {
	s.writes.Add(1)
	if s.failWith != nil {
		return false, s.failWith
	}
	return s.Store.Mutate(ctx, key, m, t)
}

// groupsDeps wires a groups manager over s, built before the store is armed
// (the manager's first load reads it), with a reload counter that starts at
// zero (SetReloadObserver replays the first load's outcome; that is not a
// reload).
func groupsDeps(s statestore.Store) (*Deps, *countingObserver) {
	d := &Deps{State: s, Groups: groups.NewManager(s)}
	obs := &countingObserver{}
	d.Groups.SetReloadObserver(obs)
	obs.n.Store(0)
	return d, obs
}

var writeOneGroup = func([]byte) ([]byte, error) {
	return []byte("groups:\n  g-one:\n    label: G\n    members: [t-one]\n"), nil
}

// An error that proves the store was not written triggers no reload: not
// before the answer, and not after it either.
func TestMutateState_NotAppliedErrorDoesNotReload(t *testing.T) {
	t.Parallel()
	errRefused := errors.New("validation refused the write")
	cases := []struct {
		name      string
		storeErr  error // what the store's Mutate returns, nil to run the transform
		transform func([]byte) ([]byte, error)
	}{
		{"transform error", nil, func([]byte) ([]byte, error) { return nil, errRefused }},
		{"wrapped transform error", nil, func([]byte) ([]byte, error) { return nil, fmt.Errorf("group g-one: %w", errRefused) }},
		{"ErrConflict", fmt.Errorf("%w (configmap ns/name)", statestore.ErrConflict), writeOneGroup},
		{"ErrTooLarge", fmt.Errorf("%w: configmap ns/name would hold 1 bytes", statestore.ErrTooLarge), writeOneGroup},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			d, obs := groupsDeps(newGatedStore(t, tc.storeErr))
			if err := d.mutateState(context.Background(), statestore.KeyGroups, "groups", "a@example.com", tc.transform); err == nil {
				t.Fatal("mutateState: want an error")
			}
			// A background reload would land well inside this window.
			time.Sleep(200 * time.Millisecond)
			if n := obs.n.Load(); n != 0 {
				t.Fatalf("reloads after a not-applied error = %d, want 0", n)
			}
		})
	}
}

// An error whose outcome is unknown reloads, but the caller's answer does
// not wait for it: mutateState returns while the reload's read is still held.
func TestMutateState_UnknownOutcomeReloadsWithoutBlocking(t *testing.T) {
	t.Parallel()
	for _, storeErr := range []error{
		fmt.Errorf("%w: update configmap ns/name: response lost", statestore.ErrUnavailable),
		errors.New("an error no one classified"),
	} {
		t.Run(storeErr.Error(), func(t *testing.T) {
			t.Parallel()
			s := newGatedStore(t, storeErr)
			d, obs := groupsDeps(s)
			s.armed.Store(true)
			released := false
			release := func() {
				if !released {
					released = true
					close(s.release)
				}
			}
			t.Cleanup(release) // never leave a reload parked on the gate

			done := make(chan error, 1)
			go func() {
				done <- d.mutateState(context.Background(), statestore.KeyGroups, "groups", "a@example.com", writeOneGroup)
			}()
			select {
			case err := <-done:
				if !errors.Is(err, storeErr) {
					t.Fatalf("mutateState error = %v, want %v", err, storeErr)
				}
			case <-time.After(eventuallyTimeout):
				t.Fatal("mutateState waited for the reload of an unknown-outcome write")
			}
			select {
			case <-s.readStarted:
			case <-time.After(eventuallyTimeout):
				t.Fatal("no reload started after an unknown-outcome write")
			}
			release()
			deadline := time.Now().Add(eventuallyTimeout)
			for obs.n.Load() != 1 {
				if time.Now().After(deadline) {
					t.Fatalf("reloads = %d, want 1", obs.n.Load())
				}
				time.Sleep(5 * time.Millisecond)
			}
		})
	}
}

// Success reloads before mutateState returns, even when the read is slow, so
// the GET after a 2xx sees the write.
func TestMutateState_SuccessReloadsSynchronously(t *testing.T) {
	t.Parallel()
	s := newGatedStore(t, nil)
	d, obs := groupsDeps(s)
	s.armed.Store(true)
	go func() {
		<-s.readStarted
		time.Sleep(100 * time.Millisecond) // a slow read
		close(s.release)
	}()
	if err := d.mutateState(context.Background(), statestore.KeyGroups, "groups", "a@example.com", writeOneGroup); err != nil {
		t.Fatalf("mutateState: %v", err)
	}
	if n := obs.n.Load(); n != 1 {
		t.Fatalf("reloads when mutateState returned = %d, want 1", n)
	}
	if _, ok := d.Groups.GetGroup("g-one"); !ok {
		t.Fatal("the snapshot does not hold the group mutateState just wrote")
	}
}

// A stream of unknown-outcome failures while a background reload is mid-read
// queues ONE more reload, not one per failure — and that one starts reading
// after the last failure, so the snapshot still converges on the last write.
func TestMutateState_UnknownOutcomeReloadsAreCoalesced(t *testing.T) {
	t.Parallel()
	const n = 10
	s := newGatedStore(t, fmt.Errorf("%w: update configmap ns/name: response lost", statestore.ErrUnavailable))
	d, obs := groupsDeps(s)
	s.armed.Store(true)
	var once atomic.Bool
	release := func() {
		if once.CompareAndSwap(false, true) {
			close(s.release)
		}
	}
	t.Cleanup(release)
	fail := func() {
		if err := d.mutateState(context.Background(), statestore.KeyGroups, "groups", "a@example.com", writeOneGroup); err == nil {
			t.Fatal("mutateState: want an error")
		}
	}

	// The first failure's reload starts reading and is held there.
	fail()
	select {
	case <-s.readStarted:
	case <-time.After(eventuallyTimeout):
		t.Fatal("no reload started after the first unknown-outcome write")
	}
	// The rest arrive while it is mid-read, spaced so that each background
	// goroutine a failure starts has run up to where it waits for the lock
	// before the next failure.
	for i := 1; i < n; i++ {
		fail()
		time.Sleep(10 * time.Millisecond)
	}
	release()

	deadline := time.Now().Add(eventuallyTimeout)
	for obs.n.Load() < 2 {
		if time.Now().After(deadline) {
			t.Fatalf("reloads = %d, want 2 (the one mid-read and one after the last write)", obs.n.Load())
		}
		time.Sleep(5 * time.Millisecond)
	}
	time.Sleep(200 * time.Millisecond) // let any surplus reload land
	if got := obs.n.Load(); got != 2 {
		t.Fatalf("reloads after %d unknown-outcome writes = %d, want 2", n, got)
	}
	s.mu.Lock()
	seen := append([]int64(nil), s.seen...)
	s.mu.Unlock()
	if last := seen[len(seen)-1]; last != n {
		t.Fatalf("the last reload started after %d of %d writes (reads saw %v): it may miss the last write", last, n, seen)
	}
}
