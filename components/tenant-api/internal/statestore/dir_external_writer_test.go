//go:build unix && !aix && !solaris

package statestore

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

// #1535: what dirStore promises a writer outside tenant-api (an operator's
// script, a hand edit). The lock is advisory: flock on <state-dir>/.lock
// keeps out only the writers that take it too. These tests pin both halves
// of the contract the docs state — without the lock an outside write that
// overlaps a Mutate is silently overwritten; with the lock it survives, in either order — so
// the docs cannot drift from the code.
//
// The outside writer opens its own file description of the lock file, so
// it contends with Mutate as a second process does (flock locks belong to
// the open file description; see lock_test.go).

const extKey = "_groups.yaml"

// extLock takes the exclusive flock an outside writer must take, the way
// flock(1) or a script would: its own open of <dir>/.lock. It returns
// errors rather than failing the test, since callers run it off the test
// goroutine.
func extLock(dir string, nonBlock bool) (release func(), err error) {
	f, err := os.OpenFile(filepath.Join(dir, lockFile), os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, err
	}
	how := syscall.LOCK_EX
	if nonBlock {
		how |= syscall.LOCK_NB
	}
	for {
		err = syscall.Flock(int(f.Fd()), how)
		if err != syscall.EINTR {
			break
		}
	}
	if err != nil {
		_ = f.Close()
		return nil, err
	}
	return func() {
		_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
		_ = f.Close()
	}, nil
}

// extAppend is the outside writer's read-modify-write: read the record,
// append line, write it back.
func extAppend(t *testing.T, dir, line string) {
	t.Helper()
	p := filepath.Join(dir, extKey)
	cur, err := os.ReadFile(p)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		t.Error(err)
		return
	}
	if err := os.WriteFile(p, append(cur, line...), 0o644); err != nil {
		t.Error(err)
	}
}

func appendLine(line string) Transform {
	return func(cur []byte) ([]byte, error) { return append(append([]byte(nil), cur...), line...), nil }
}

func newSeededDirStore(t *testing.T) (Store, string) {
	t.Helper()
	ds := newDirStore(t) // store_test.go: a quiet logger
	s, dir := Store(ds), ds.dir
	if _, err := s.Mutate(context.Background(), extKey, Meta{Entity: "test"}, appendLine("seed\n")); err != nil {
		t.Fatal(err)
	}
	return s, dir
}

func readRecord(t *testing.T, s Store) string {
	t.Helper()
	b, _, err := s.Get(context.Background(), extKey)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// An outside writer that does not take the lock writes while a Mutate is
// between its read and its write. Mutate reports success and its write
// replaces the record with what it read plus its own change: the outside
// write is gone, and nothing says so.
func TestDirStore_UnlockedExternalWriteIsSilentlyOverwritten(t *testing.T) {
	s, dir := newSeededDirStore(t)
	changed, err := s.Mutate(context.Background(), extKey, Meta{Entity: "test"}, func(cur []byte) ([]byte, error) {
		extAppend(t, dir, "external\n") // no lock taken
		return appendLine("tenant-api\n")(cur)
	})
	if err != nil || !changed {
		t.Fatalf("Mutate = (%v, %v), want (true, nil): the overwrite is silent", changed, err)
	}
	if got, want := readRecord(t, s), "seed\ntenant-api\n"; got != want {
		t.Fatalf("record = %q, want %q (the unlocked outside write overwritten)", got, want)
	}
}

// The outside writer asks for the lock while a Mutate holds it: it waits,
// then reads the record Mutate wrote and keeps both changes.
func TestDirStore_LockedExternalWriteDuringMutateSurvives(t *testing.T) {
	s, dir := newSeededDirStore(t)
	blocked := make(chan struct{})
	done := make(chan struct{})
	if _, err := s.Mutate(context.Background(), extKey, Meta{Entity: "test"}, func(cur []byte) ([]byte, error) {
		go func() {
			defer close(done)
			// Prove it contends: the lock is held by Mutate right now. If
			// the probe gets the lock, Mutate does not hold it: release it
			// and stop, or the blocking take below would wait on our own
			// probe forever.
			probe, err := extLock(dir, true)
			close(blocked)
			if err == nil {
				probe()
				t.Error("non-blocking flock during Mutate succeeded: Mutate does not hold the lock")
				return
			}
			if !errors.Is(err, syscall.EWOULDBLOCK) {
				t.Errorf("non-blocking flock during Mutate = %v, want EWOULDBLOCK", err)
				return
			}
			release, err := extLock(dir, false)
			if err != nil {
				t.Error(err)
				return
			}
			defer release()
			extAppend(t, dir, "external\n")
		}()
		<-blocked
		return appendLine("tenant-api\n")(cur)
	}); err != nil {
		t.Fatal(err)
	}
	<-done
	if got, want := readRecord(t, s), "seed\ntenant-api\nexternal\n"; got != want {
		t.Fatalf("record = %q, want %q", got, want)
	}
}

// The outside writer holds the lock first: Mutate waits for it, and reads
// the record only once it has the lock — so it builds on the outside write
// instead of on what the file held before.
func TestDirStore_MutateAfterLockedExternalWriteRereads(t *testing.T) {
	s, dir := newSeededDirStore(t)
	release, err := extLock(dir, false)
	if err != nil {
		t.Fatal(err)
	}
	released := false
	defer func() {
		if !released {
			release()
		}
	}()
	ran := make(chan []byte, 1)
	done := make(chan error, 1)
	go func() {
		_, err := s.Mutate(context.Background(), extKey, Meta{Entity: "test"}, func(cur []byte) ([]byte, error) {
			ran <- append([]byte(nil), cur...)
			return appendLine("tenant-api\n")(cur)
		})
		done <- err
	}()
	select {
	case cur := <-ran:
		t.Fatalf("Mutate ran its transform (on %q) while the outside writer held the lock", cur)
	case <-time.After(100 * time.Millisecond):
	}
	extAppend(t, dir, "external\n")
	release()
	released = true
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if cur := <-ran; string(cur) != "seed\nexternal\n" {
		t.Errorf("transform saw %q, want the outside write", cur)
	}
	if got, want := readRecord(t, s), "seed\nexternal\ntenant-api\n"; got != want {
		t.Fatalf("record = %q, want %q", got, want)
	}
}
