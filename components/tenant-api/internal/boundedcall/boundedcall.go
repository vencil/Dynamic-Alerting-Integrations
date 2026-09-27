// Package boundedcall bounds a call that cannot be cancelled — a conf.d read
// takes no context, and a file whose read never returns (a FIFO named
// *.yaml, a hung network mount) would otherwise block its caller forever.
//
// The mechanism is the one the gitops writer's conf.d walk introduced
// (#2078), moved here so the tenant GET's root platform read (#2208) uses the
// same one rather than a copy: the call runs on its own goroutine; past the
// timeout the caller gets ErrTimedOut and the call is left running, counted
// as stuck until it returns; while any call is stuck, new calls fail at once
// with ErrStuck instead of starting another one behind the same file.
//
// ⚠️ That bounds new calls only AFTER the first timeout: callers that start
// within the same timeout window can each block too. A stuck call ends only
// when the read it is blocked in returns (for a FIFO: once it is opened for
// writing) or the process restarts; removing the file does not unblock a read
// already in progress.
package boundedcall

import (
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"
)

var (
	// ErrTimedOut: this call outlived its bound and was abandoned.
	ErrTimedOut = errors.New("timed out")
	// ErrStuck: an earlier abandoned call has not returned yet, so this one
	// was not started.
	ErrStuck = errors.New("an earlier call is still blocked")
	// ErrPanicked: the call panicked. The wrapped text carries the panic
	// value, for the server log only — never hand it to a client.
	ErrPanicked = errors.New("call panicked")
)

// Run calls fn on its own goroutine and waits at most timeout for it.
// stuck counts the abandoned calls sharing it; a non-zero count fails Run at
// once with ErrStuck. A panic in fn is recovered and returned as ErrPanicked
// (wrapping the panic value). On any error the zero T is returned.
func Run[T any](timeout time.Duration, stuck *atomic.Int32, fn func() T) (T, error) {
	return RunCommit(timeout, stuck, fn, nil)
}

// RunCommit is Run plus commit, which is called with fn's result only when
// the call is handed back to its caller: it returned (no panic) and was not
// abandoned. commit runs on the call's goroutine under the same mutex that
// decides abandonment, so "the result was published" and "the caller gave
// up" can never both happen — the writer's conf.d walk publishes its prior
// scan this way (#2153): a walk that errored, panicked or outlived its
// caller must never become the next walk's prior. A nil commit is Run.
func RunCommit[T any](timeout time.Duration, stuck *atomic.Int32, fn func() T, commit func(T)) (T, error) {
	var zero T
	if stuck.Load() > 0 {
		return zero, ErrStuck
	}
	type outcome struct {
		value    T
		panicked bool
		reason   any
	}
	done := make(chan outcome, 1) // buffered: an abandoned call must not block on send
	// mu orders "the call finished" against "the caller gave up", so exactly
	// one side accounts for an abandoned call: the caller increments only if
	// the call has not finished, and the call decrements only if abandoned.
	var mu sync.Mutex
	finished, abandoned := false, false
	go func() {
		var o outcome
		// ⛔ RECOVER HERE, NOT IN THE CALLER. fn runs on this goroutine, which
		// no caller's recover (chi's Recoverer on an HTTP handler) reaches: an
		// unrecovered panic here kills the process. The deferred block also
		// does the finished/stuck accounting, so a panicking call — in time
		// or after it was abandoned — leaves the counter exactly as a
		// returning one would.
		defer func() {
			if r := recover(); r != nil {
				o = outcome{panicked: true, reason: r}
			}
			mu.Lock()
			finished = true
			if abandoned {
				stuck.Add(-1)
			} else if !o.panicked && commit != nil {
				commit(o.value)
			}
			mu.Unlock()
			done <- o
		}()
		o.value = fn()
	}()
	result := func(o outcome) (T, error) {
		if o.panicked {
			return zero, fmt.Errorf("%w: %v", ErrPanicked, o.reason)
		}
		return o.value, nil
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case o := <-done:
		return result(o)
	case <-timer.C:
		mu.Lock()
		if finished { // lost the race to a call that just completed: use it
			mu.Unlock()
			return result(<-done)
		}
		abandoned = true
		stuck.Add(1)
		mu.Unlock()
		return zero, ErrTimedOut
	}
}

// DefaultTimeout is a Guard's bound when its Timeout is not positive. It is
// the writer's conf.d walk bound: a healthy read takes milliseconds.
const DefaultTimeout = 5 * time.Second

// Guard is Run with its timeout and stuck counter kept together, for callers
// that have no struct of their own to hold them. The zero value is ready to
// use (DefaultTimeout).
type Guard struct {
	Timeout time.Duration
	stuck   atomic.Int32
}

// Do is Run under g.
func Do[T any](g *Guard, fn func() T) (T, error) {
	return Run(g.timeout(), &g.stuck, fn)
}

// timeout returns the bound Do applies.
func (g *Guard) timeout() time.Duration {
	if g.Timeout <= 0 {
		return DefaultTimeout
	}
	return g.Timeout
}

// Stuck is the number of abandoned calls that have not returned yet.
func (g *Guard) Stuck() int32 { return g.stuck.Load() }
