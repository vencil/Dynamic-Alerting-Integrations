package boundedcall

// The FIFO tests (gitops writer_declared_elsewhere_fifo_unix_test.go,
// handler tenant_get_fifo_unix_test.go) exercise this through real blocked
// reads on unix; this one pins the contract on every platform with a call
// blocked on a channel.

import (
	"errors"
	"sync/atomic"
	"testing"
	"time"
)

func TestGuardTimesOutFailsFastWhileStuckAndRecovers(t *testing.T) {
	t.Parallel()
	g := &Guard{Timeout: 50 * time.Millisecond}
	release := make(chan struct{})

	if _, err := Do(g, func() int { <-release; return 1 }); !errors.Is(err, ErrTimedOut) {
		t.Fatalf("blocked call: err = %v, want ErrTimedOut", err)
	}
	if g.Stuck() != 1 {
		t.Fatalf("Stuck() = %d, want 1", g.Stuck())
	}
	start := time.Now()
	if _, err := Do(g, func() int { return 2 }); !errors.Is(err, ErrStuck) {
		t.Fatalf("call while stuck: err = %v, want ErrStuck", err)
	}
	if took := time.Since(start); took >= g.Timeout {
		t.Errorf("call while stuck took %v: it waited instead of failing at once", took)
	}

	close(release)
	deadline := time.Now().Add(5 * time.Second)
	for g.Stuck() != 0 {
		if time.Now().After(deadline) {
			t.Fatalf("stuck call never drained")
		}
		time.Sleep(5 * time.Millisecond)
	}
	if v, err := Do(g, func() int { return 3 }); err != nil || v != 3 {
		t.Fatalf("after recovery: (%d, %v), want (3, nil)", v, err)
	}
}

// TestPanicIsAnErrorNotACrash: fn runs on Run's own goroutine, where no
// caller's recover (chi's Recoverer on a GET) can reach it — an unrecovered
// panic there would kill the process. Run must return it as ErrPanicked, and
// the guard must stay usable, whether the panic comes before the bound or
// after the call was abandoned.
func TestPanicIsAnErrorNotACrash(t *testing.T) {
	t.Parallel()
	g := &Guard{Timeout: time.Second}
	_, err := Do(g, func() int { panic("boom") })
	if !errors.Is(err, ErrPanicked) {
		t.Fatalf("err = %v, want ErrPanicked", err)
	}
	if g.Stuck() != 0 {
		t.Fatalf("Stuck() = %d after a panic that returned in time", g.Stuck())
	}
	if v, err := Do(g, func() int { return 4 }); err != nil || v != 4 {
		t.Fatalf("guard after a panic: (%d, %v)", v, err)
	}

	// Abandoned, then panics: the stuck count must still drain.
	slow := &Guard{Timeout: 20 * time.Millisecond}
	release := make(chan struct{})
	if _, err := Do(slow, func() int { <-release; panic("late boom") }); !errors.Is(err, ErrTimedOut) {
		t.Fatalf("err = %v, want ErrTimedOut", err)
	}
	close(release)
	deadline := time.Now().Add(5 * time.Second)
	for slow.Stuck() != 0 {
		if time.Now().After(deadline) {
			t.Fatalf("a call that panicked after being abandoned never drained the stuck count")
		}
		time.Sleep(5 * time.Millisecond)
	}
	if v, err := Do(slow, func() int { return 5 }); err != nil || v != 5 {
		t.Fatalf("guard after a late panic: (%d, %v)", v, err)
	}
}

func TestZeroGuardUsesTheDefaultBound(t *testing.T) {
	t.Parallel()
	var g Guard
	if got := g.timeout(); got != DefaultTimeout {
		t.Fatalf("zero Guard timeout = %v, want %v", got, DefaultTimeout)
	}
	if v, err := Do(&g, func() string { return "ok" }); err != nil || v != "ok" {
		t.Fatalf("(%q, %v)", v, err)
	}
}

// TestRunCommitOnlyPublishesAWalkHandedBack pins #2153's rule as RunCommit
// carries it: commit sees a result only when the call returned in time —
// never for a call that panicked or was abandoned (even once it returns).
func TestRunCommitOnlyPublishesAWalkHandedBack(t *testing.T) {
	var stuck atomic.Int32
	var committed atomic.Int32
	commit := func(int) { committed.Add(1) }

	if v, err := RunCommit(time.Second, &stuck, func() int { return 7 }, commit); err != nil || v != 7 {
		t.Fatalf("in-time call = %v, %v", v, err)
	}
	if committed.Load() != 1 {
		t.Fatalf("in-time call committed %d times, want 1", committed.Load())
	}

	if _, err := RunCommit(time.Second, &stuck, func() int { panic("boom") }, commit); !errors.Is(err, ErrPanicked) {
		t.Fatalf("panicking call err = %v, want ErrPanicked", err)
	}
	if committed.Load() != 1 {
		t.Fatalf("a panicking call was committed")
	}

	release := make(chan struct{})
	returned := make(chan struct{})
	if _, err := RunCommit(20*time.Millisecond, &stuck, func() int {
		<-release
		defer close(returned)
		return 9
	}, commit); !errors.Is(err, ErrTimedOut) {
		t.Fatalf("slow call err = %v, want ErrTimedOut", err)
	}
	close(release)
	<-returned
	deadline := time.Now().Add(2 * time.Second)
	for stuck.Load() != 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if stuck.Load() != 0 {
		t.Fatalf("stuck = %d after the abandoned call returned", stuck.Load())
	}
	if committed.Load() != 1 {
		t.Fatalf("an abandoned call was committed after it returned")
	}
}
