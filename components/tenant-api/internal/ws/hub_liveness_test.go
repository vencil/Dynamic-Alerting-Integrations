package ws

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeSSEConn is a ResponseWriter that supports SetWriteDeadline (which
// httptest.NewRecorder does NOT), so the per-write-deadline path (#143) can be
// exercised deterministically without a real TCP socket. With blockAfter >= 0,
// every Write after the blockAfter-th simulates a stuck client: it blocks until
// the currently-set write deadline, then returns os.ErrDeadlineExceeded — the
// exact behavior the real net stack produces when SetWriteDeadline elapses on a
// socket whose buffer a slow client isn't draining.
type fakeSSEConn struct {
	mu         sync.Mutex
	header     http.Header
	deadlines  []time.Time
	written    bytes.Buffer
	writeCount int
	blockAfter int // -1 = never block
}

func newFakeSSEConn(blockAfter int) *fakeSSEConn {
	return &fakeSSEConn{header: http.Header{}, blockAfter: blockAfter}
}

func (f *fakeSSEConn) Header() http.Header { return f.header }
func (f *fakeSSEConn) WriteHeader(int)     {}
func (f *fakeSSEConn) Flush()              {} // satisfies http.Flusher

func (f *fakeSSEConn) SetWriteDeadline(t time.Time) error {
	f.mu.Lock()
	f.deadlines = append(f.deadlines, t)
	f.mu.Unlock()
	return nil
}

func (f *fakeSSEConn) Write(p []byte) (int, error) {
	f.mu.Lock()
	f.writeCount++
	n := f.writeCount
	var d time.Time
	if len(f.deadlines) > 0 {
		d = f.deadlines[len(f.deadlines)-1]
	}
	f.mu.Unlock()

	if f.blockAfter >= 0 && n > f.blockAfter {
		// Stuck client. Model the real net.Conn write-deadline semantics:
		//   - zero deadline  → NO deadline set → a real socket blocks until the
		//     client reads or the conn drops. We model "blocks ~forever" with a
		//     long sleep so a handler that fails to set a per-write deadline is
		//     demonstrably leaked (the test's own timeout then catches it). This
		//     is what makes the per-write-deadline mutation (skip SetWriteDeadline)
		//     fail the stuck-client test — i.e. proves the deadline is load-bearing.
		//   - future deadline → block until it, then fail (the production path).
		//   - past deadline   → fail immediately.
		if d.IsZero() {
			time.Sleep(time.Hour) // effectively "forever" vs any test timeout
			return 0, os.ErrDeadlineExceeded
		}
		if wait := time.Until(d); wait > 0 {
			time.Sleep(wait)
		}
		return 0, os.ErrDeadlineExceeded
	}

	f.mu.Lock()
	f.written.Write(p)
	f.mu.Unlock()
	return len(p), nil
}

func (f *fakeSSEConn) body() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.written.String()
}

func (f *fakeSSEConn) firstDeadline() (time.Time, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.deadlines) == 0 {
		return time.Time{}, false
	}
	return f.deadlines[0], true
}

// TestServeHTTP_StuckClientCleanedUpViaWriteDeadline is the core #143 AC: a
// client that stops draining its socket must NOT hold its serving goroutine
// forever. The heartbeat fires a write, the per-write deadline trips, the write
// errors, and ServeHTTP returns → deferred Unsubscribe runs → ClientCount→0.
// ClientCount==0 is a deterministic proxy for "the serving goroutine exited"
// (the defer only runs on return), avoiding flaky runtime.NumGoroutine polling.
func TestServeHTTP_StuckClientCleanedUpViaWriteDeadline(t *testing.T) {
	t.Parallel()
	h := NewHubWithConfig(Config{
		HeartbeatInterval: 20 * time.Millisecond,
		WriteTimeout:      50 * time.Millisecond,
	})
	// blockAfter=1: the initial "connected" event (write #1) succeeds, then the
	// first heartbeat write blocks → deadline → cleanup. This specifically
	// exercises the heartbeat-driven detection of an idle stuck client.
	conn := newFakeSSEConn(1)

	req := httptest.NewRequest("GET", "/api/v1/events", nil)
	done := make(chan struct{})
	go func() {
		h.ServeHTTP(conn, req)
		close(done)
	}()

	// Without the write deadline this goroutine would block forever on the
	// heartbeat write. With it, cleanup happens within ~heartbeat+writeTimeout
	// (~70ms); allow generous slack for CI scheduling.
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("ServeHTTP did not return — stuck-client write deadline did not fire (goroutine leak)")
	}
	waitForClientCount(t, h, 0, time.Second)
}

// TestServeHTTP_ExemptsGlobalWriteTimeout asserts the handler clears the
// server's global write deadline on connect (#143) — the zero-time
// SetWriteDeadline — so a long-lived SSE stream isn't severed at
// TA_WRITE_TIMEOUT. Without this, the feature can't outlive ~30s.
func TestServeHTTP_ExemptsGlobalWriteTimeout(t *testing.T) {
	t.Parallel()
	h := NewHubWithConfig(Config{HeartbeatInterval: 20 * time.Millisecond, WriteTimeout: 50 * time.Millisecond})
	conn := newFakeSSEConn(0) // block immediately so the handler returns fast

	req := httptest.NewRequest("GET", "/api/v1/events", nil)
	done := make(chan struct{})
	go func() { h.ServeHTTP(conn, req); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("ServeHTTP did not return")
	}

	d, ok := conn.firstDeadline()
	if !ok {
		t.Fatal("handler never called SetWriteDeadline")
	}
	if !d.IsZero() {
		t.Errorf("first SetWriteDeadline = %v, want zero time (clears the global WriteTimeout for the long-lived stream)", d)
	}
}

// TestServeHTTP_HeartbeatEmitted confirms a healthy (non-blocking) client
// receives periodic `: keepalive` comments — the anti-proxy-reaping + liveness
// probe (#143).
func TestServeHTTP_HeartbeatEmitted(t *testing.T) {
	t.Parallel()
	h := NewHubWithConfig(Config{HeartbeatInterval: 15 * time.Millisecond, WriteTimeout: time.Second})
	conn := newFakeSSEConn(-1) // never block — healthy client

	ctx, cancel := context.WithCancel(context.Background())
	req := httptest.NewRequest("GET", "/api/v1/events", nil).WithContext(ctx)
	done := make(chan struct{})
	go func() { h.ServeHTTP(conn, req); close(done) }()

	waitForClientCount(t, h, 1, time.Second)

	// Poll for a heartbeat to land (no blind sleep).
	deadline := time.After(2 * time.Second)
	for {
		if strings.Contains(conn.body(), ": keepalive") {
			break
		}
		select {
		case <-deadline:
			cancel()
			<-done
			t.Fatalf("no heartbeat emitted within timeout; body=%q", conn.body())
		case <-time.After(5 * time.Millisecond):
		}
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("handler did not finish after cancel")
	}
}

// TestServeHTTP_MaxLifetimeCloses confirms the optional hard max-lifetime cap
// closes the connection with a {"type":"close"} event (#143 defense-in-depth).
func TestServeHTTP_MaxLifetimeCloses(t *testing.T) {
	t.Parallel()
	h := NewHubWithConfig(Config{
		HeartbeatInterval: time.Hour, // disabled-in-practice for this test
		WriteTimeout:      time.Second,
		MaxLifetime:       40 * time.Millisecond,
	})
	conn := newFakeSSEConn(-1)
	req := httptest.NewRequest("GET", "/api/v1/events", nil)
	done := make(chan struct{})
	go func() { h.ServeHTTP(conn, req); close(done) }()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("ServeHTTP did not return at max lifetime")
	}
	if !strings.Contains(conn.body(), `"type":"close"`) {
		t.Errorf("expected a close event at max lifetime; body=%q", conn.body())
	}
	waitForClientCount(t, h, 0, time.Second)
}
