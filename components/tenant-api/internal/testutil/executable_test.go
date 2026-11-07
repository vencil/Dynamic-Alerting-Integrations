package testutil

// WriteExecutable under the race it exists for: goroutines keep forking while
// others write a stub and run it at once. os.WriteFile runs in the same
// harness only as a measure of the harness (logged, not asserted: how often
// it loses depends on the machine); WriteExecutable must never lose.

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
)

// raceWriteThenRun returns how many of its runs failed with ETXTBSY.
func raceWriteThenRun(t *testing.T, write func(string, []byte, os.FileMode) error) int {
	dir := t.TempDir()
	stop := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ { // background forkers
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				_ = exec.Command("/bin/true").Run()
			}
		}()
	}
	busy := 0
	var mu sync.Mutex
	var wg2 sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg2.Add(1)
		go func(g int) {
			defer wg2.Done()
			for i := 0; i < 150; i++ {
				p := filepath.Join(dir, fmt.Sprintf("s%d-%d.sh", g, i))
				if err := write(p, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
					t.Error(err)
					return
				}
				if err := exec.Command(p).Run(); err != nil && strings.Contains(err.Error(), "text file busy") {
					mu.Lock()
					busy++
					mu.Unlock()
				}
			}
		}(g)
	}
	wg2.Wait()
	close(stop)
	wg.Wait()
	return busy
}

func TestWriteExecutableUnderConcurrentForks(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("runs /bin/true and a shell stub")
	}
	t.Logf("os.WriteFile in the same harness: %d of 1200 runs hit ETXTBSY", raceWriteThenRun(t, os.WriteFile))
	if n := raceWriteThenRun(t, WriteExecutable); n != 0 {
		t.Errorf("WriteExecutable: %d of 1200 runs hit ETXTBSY, want 0", n)
	}
}
