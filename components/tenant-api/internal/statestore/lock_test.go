//go:build unix && !aix && !solaris

package statestore

import (
	"context"
	"strconv"
	"sync"
	"testing"
	"time"
)

// #2884: two stores over one directory stand for the server and
// `tenant-api state restore` — two processes, two process mutexes. Their
// read-transform-writes on one record must not interleave: each increments
// a counter, and every increment must survive. (flock locks belong to the
// open file description, so two stores in one process contend as two
// processes do.)
func TestDirStore_WritesFromTwoStoresDoNotOverwrite(t *testing.T) {
	dir := t.TempDir()
	const perStore = 40
	var wg sync.WaitGroup
	for i := 0; i < 2; i++ {
		s, err := NewDir(dir, nil)
		if err != nil {
			t.Fatal(err)
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < perStore; j++ {
				if _, err := s.Mutate(context.Background(), "_counter.yaml", Meta{Entity: "test"}, func(cur []byte) ([]byte, error) {
					n, _ := strconv.Atoi(string(cur))
					time.Sleep(200 * time.Microsecond) // widen the read→write window
					return []byte(strconv.Itoa(n + 1)), nil
				}); err != nil {
					t.Error(err)
					return
				}
			}
		}()
	}
	wg.Wait()
	s, _ := NewDir(dir, nil)
	b, _, err := s.Get(context.Background(), "_counter.yaml")
	if err != nil {
		t.Fatal(err)
	}
	if got := string(b); got != strconv.Itoa(2*perStore) {
		t.Errorf("counter = %s, want %d: a write from one store overwrote the other's", got, 2*perStore)
	}
	if keys, _ := s.Keys(context.Background(), ""); len(keys) != 1 {
		t.Errorf("Keys = %v, want only the record (the lock file is not one)", keys)
	}
}
