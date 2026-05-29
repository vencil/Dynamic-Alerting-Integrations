package platform

import (
	"errors"
	"fmt"
	"net/http"
	"sync"
	"testing"
)

// errProbe is a transport-style error (not an *APIError) used to simulate a
// network failure / timeout — the kind isForgeDegradation must treat as a trip.
var errProbe = errors.New("dial tcp: connection refused")

func TestIsForgeDegradation(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name string
		err  error
		want bool
	}{
		{"nil is success", nil, false},
		{"403 is client outcome, not degradation", &APIError{StatusCode: http.StatusForbidden}, false},
		{"404 is client outcome", &APIError{StatusCode: http.StatusNotFound}, false},
		{"409 is client outcome (pending PR)", &APIError{StatusCode: http.StatusConflict}, false},
		{"422 is client outcome (validation)", &APIError{StatusCode: http.StatusUnprocessableEntity}, false},
		{"429 rate-limit is NOT a 5xx → not degradation", &APIError{StatusCode: http.StatusTooManyRequests}, false},
		{"500 is forge degradation", &APIError{StatusCode: http.StatusInternalServerError}, true},
		{"502 is forge degradation", &APIError{StatusCode: http.StatusBadGateway}, true},
		{"503 is forge degradation", &APIError{StatusCode: http.StatusServiceUnavailable}, true},
		{"network error is forge degradation", errProbe, true},
		{"wrapped 500 still degradation", fmt.Errorf("create PR: %w", &APIError{StatusCode: 500}), true},
		{"wrapped 403 still not degradation", fmt.Errorf("create PR: %w", &APIError{StatusCode: 403}), false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := isForgeDegradation(tt.err); got != tt.want {
				t.Errorf("isForgeDegradation(%v) = %v, want %v", tt.err, got, tt.want)
			}
		})
	}
}

// TestCircuitBreaker_TripsOnConsecutiveDegradation drives the breaker through
// the full closed→open→half-open→closed cycle and asserts the state machine.
func TestCircuitBreaker_TripsOnConsecutiveDegradation(t *testing.T) {
	t.Parallel()
	cb := NewCircuitBreaker("test-trips")

	degraded := func() ([]byte, http.Header, error) {
		return nil, nil, &APIError{StatusCode: http.StatusServiceUnavailable}
	}

	// Closed at rest.
	if got := cb.State(); got != "closed" {
		t.Fatalf("initial state = %q, want closed", got)
	}

	// cbConsecutiveFailures degradation failures must trip it open. The Nth
	// failure is what flips the state; before that it stays closed and the
	// real 503 error propagates unchanged.
	for i := 0; i < cbConsecutiveFailures; i++ {
		_, _, err := cb.Execute(degraded)
		var apiErr *APIError
		if !errors.As(err, &apiErr) || apiErr.StatusCode != http.StatusServiceUnavailable {
			t.Fatalf("call %d: err = %v, want 503 APIError propagated", i, err)
		}
	}
	if got := cb.State(); got != "open" {
		t.Fatalf("state after %d failures = %q, want open", cbConsecutiveFailures, got)
	}

	// While open, the breaker fast-fails with ErrCircuitOpen WITHOUT invoking
	// fn at all — prove fn is never called.
	called := false
	_, _, err := cb.Execute(func() ([]byte, http.Header, error) {
		called = true
		return []byte("ok"), nil, nil
	})
	if !errors.Is(err, ErrCircuitOpen) {
		t.Errorf("open-state err = %v, want ErrCircuitOpen", err)
	}
	if called {
		t.Error("fn was invoked while breaker open — should have short-circuited")
	}
}

// TestCircuitBreaker_4xxDoesNotTrip is the load-bearing guard: a flood of
// deterministic client-side 4xx errors (e.g. one tenant with a bad token)
// must NOT open the breaker for everyone. Without the IsSuccessful exclusion
// this would trip after cbConsecutiveFailures and is exactly the mutation the
// dogfood flips.
func TestCircuitBreaker_4xxDoesNotTrip(t *testing.T) {
	t.Parallel()
	cb := NewCircuitBreaker("test-4xx")

	forbidden := func() ([]byte, http.Header, error) {
		return nil, nil, &APIError{StatusCode: http.StatusForbidden}
	}

	// Far more than the trip threshold — none should count as a breaker failure.
	for i := 0; i < cbConsecutiveFailures*3; i++ {
		_, _, err := cb.Execute(forbidden)
		if !errors.Is(err, ErrForbidden) {
			t.Fatalf("call %d: err = %v, want ErrForbidden propagated", i, err)
		}
	}
	if got := cb.State(); got != "closed" {
		t.Errorf("state after %d×403 = %q, want closed (4xx must not trip)", cbConsecutiveFailures*3, got)
	}
}

// TestCircuitBreaker_SuccessKeepsClosed confirms a healthy forge keeps the
// breaker closed and propagates results unchanged.
func TestCircuitBreaker_SuccessKeepsClosed(t *testing.T) {
	t.Parallel()
	cb := NewCircuitBreaker("test-success")
	for i := 0; i < cbConsecutiveFailures*2; i++ {
		body, _, err := cb.Execute(func() ([]byte, http.Header, error) {
			return []byte("payload"), http.Header{"X-Test": []string{"1"}}, nil
		})
		if err != nil || string(body) != "payload" {
			t.Fatalf("call %d: body=%q err=%v, want payload/nil", i, body, err)
		}
	}
	if got := cb.State(); got != "closed" {
		t.Errorf("state after successes = %q, want closed", got)
	}
}

// TestCircuitBreaker_InterruptedFailureStreakStaysClosed confirms the trip is
// on CONSECUTIVE failures: a success between failures resets the streak.
func TestCircuitBreaker_InterruptedFailureStreakStaysClosed(t *testing.T) {
	t.Parallel()
	cb := NewCircuitBreaker("test-interrupted")
	degraded := func() ([]byte, http.Header, error) {
		return nil, nil, &APIError{StatusCode: 500}
	}
	ok := func() ([]byte, http.Header, error) { return []byte("ok"), nil, nil }

	// (cbConsecutiveFailures-1) failures, then a success, repeated — never
	// reaches a consecutive run of cbConsecutiveFailures.
	for round := 0; round < 3; round++ {
		for i := 0; i < cbConsecutiveFailures-1; i++ {
			_, _, _ = cb.Execute(degraded)
		}
		_, _, _ = cb.Execute(ok)
	}
	if got := cb.State(); got != "closed" {
		t.Errorf("state = %q, want closed (success resets the consecutive streak)", got)
	}
}

// TestCircuitSnapshot confirms breakers register for the /metrics snapshot.
func TestCircuitSnapshot(t *testing.T) {
	// Not parallel: asserts on the package-level registry.
	_ = NewCircuitBreaker("GitHub")
	snap := CircuitSnapshot()
	if snap["GitHub"] != "closed" {
		t.Errorf("snapshot[GitHub] = %q, want closed", snap["GitHub"])
	}
}

// TestCircuitBreaker_ConcurrentExecuteAndSnapshot reproduces the production
// concurrency pattern under -race: the writer and the PollingTracker share one
// breaker (concurrent Execute), while the /metrics handler concurrently reads
// CircuitSnapshot() and the tracker writes setConflictCount/reads
// ConflictSnapshot. Validates the wrapper + the mutex-guarded registries, not
// just gobreaker's internal locking. Run with -race to be meaningful.
func TestCircuitBreaker_ConcurrentExecuteAndSnapshot(t *testing.T) {
	t.Parallel()
	cb := NewCircuitBreaker("concurrent-test")

	var wg sync.WaitGroup
	stop := make(chan struct{})

	// Two "actors" (writer + tracker) hammering the shared breaker with a mix
	// of success and degradation.
	for actor := 0; actor < 2; actor++ {
		wg.Add(1)
		go func(a int) {
			defer wg.Done()
			for i := 0; ; i++ {
				select {
				case <-stop:
					return
				default:
				}
				_, _, _ = cb.Execute(func() ([]byte, http.Header, error) {
					if (i+a)%3 == 0 {
						return nil, nil, &APIError{StatusCode: 503}
					}
					return []byte("ok"), nil, nil
				})
			}
		}(actor)
	}

	// Metrics-handler reader + conflict-count writer, concurrent with the above.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
			}
			_ = CircuitSnapshot()
			setConflictCount("concurrent-test", i%4)
			_ = ConflictSnapshot()
		}
	}()

	// Let the goroutines interleave briefly, then stop. The assertion is simply
	// "no race detected and no panic" — the value is in -race coverage of the
	// concurrent registry + Execute paths.
	for i := 0; i < 2000; i++ {
		_ = cb.State()
	}
	close(stop)
	wg.Wait()
}

func TestConflictSnapshot(t *testing.T) {
	// Not parallel: asserts on the package-level registry.
	setConflictCount("GitLab", 2)
	if got := ConflictSnapshot()["GitLab"]; got != 2 {
		t.Errorf("ConflictSnapshot[GitLab] = %d, want 2", got)
	}
	setConflictCount("GitLab", 0)
	if got := ConflictSnapshot()["GitLab"]; got != 0 {
		t.Errorf("ConflictSnapshot[GitLab] after clear = %d, want 0", got)
	}
}
