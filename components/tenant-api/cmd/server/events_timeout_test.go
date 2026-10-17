package main

// The request timeout (chi middleware.Timeout) bounds every route except the
// SSE stream GET /api/v1/events (#2520). Under the timeout the stream's
// context is cancelled at the deadline, the handler returns and the
// connection ends — so an event sent after the deadline never arrives, and
// chi writes a 504 onto the open stream. The hub's heartbeat, per-write
// deadline and optional max lifetime bound the stream instead.

import (
	"bufio"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/handler"
)

const shortRequestTimeout = 150 * time.Millisecond

// An event broadcast well after the request timeout still reaches a stream
// opened through the production router.
func TestEventsStreamOutlivesRequestTimeout(t *testing.T) {
	t.Parallel()
	f := newGateFixtureWith(t, anyTenantRBACYAML, false, routerDeps{RequestTimeout: shortRequestTimeout})
	srv := httptest.NewServer(f.router)
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, srv.URL+"/api/v1/events", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("X-Forwarded-Email", exactCaller.email())
	req.Header.Set("X-Forwarded-Groups", exactCaller.group)
	resp, err := srv.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d, want 200", resp.StatusCode)
	}

	lines := make(chan string)
	go func() {
		defer close(lines)
		sc := bufio.NewScanner(resp.Body)
		for sc.Scan() {
			select {
			case lines <- sc.Text():
			case <-ctx.Done():
				return
			}
		}
	}()

	deadline := time.Now().Add(5 * time.Second)
	for f.hub.ClientCount() != 1 {
		if time.Now().After(deadline) {
			t.Fatal("no subscriber")
		}
		time.Sleep(time.Millisecond)
	}
	// Three timeouts past the subscription, then one event.
	time.Sleep(3 * shortRequestTimeout)
	f.hub.Broadcast(handler.ConfigChangeEvent("alpha-one"))

	timeout := time.After(5 * time.Second)
	for {
		select {
		case line, ok := <-lines:
			if !ok {
				t.Fatalf("stream ended before the event sent %v after subscribing arrived (request timeout %v cut it)",
					3*shortRequestTimeout, shortRequestTimeout)
			}
			if strings.Contains(line, `"tenant_id":"alpha-one"`) {
				return
			}
		case <-timeout:
			t.Fatal("event did not arrive within 5s")
		}
	}
}

// Every route other than the stream keeps the request timeout. The injected
// rate limiter sits after the timeout in the chain, so it sees the request
// context exactly as the handler does.
func TestRequestTimeoutStillAppliesToOtherRoutes(t *testing.T) {
	t.Parallel()
	var mu sync.Mutex
	hasDeadline := map[string]bool{}
	probe := func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_, ok := r.Context().Deadline()
			mu.Lock()
			hasDeadline[r.Method+" "+r.URL.Path] = ok
			mu.Unlock()
			if r.URL.Path == "/api/v1/events" {
				return // the deadline is all this test reads; skip the stream
			}
			next.ServeHTTP(w, r)
		})
	}
	f := newGateFixtureWith(t, anyTenantRBACYAML, false, routerDeps{RequestTimeout: time.Minute, RateLimit: probe})
	for _, rt := range []struct{ method, path string }{
		{http.MethodGet, "/api/v1/me"},
		{http.MethodGet, "/api/v1/tenants"},
		{http.MethodGet, "/api/v1/groups"},
		{http.MethodPost, "/api/v1/events"}, // not the stream: no such route, still timed
		{http.MethodGet, "/api/v1/events/"}, // ditto
		{http.MethodGet, "/health"},
		{http.MethodGet, "/api/v1/events"},
	} {
		f.do(t, adminCaller, rt.method, rt.path, "")
	}
	mu.Lock()
	defer mu.Unlock()
	for key, ok := range hasDeadline {
		want := key != "GET /api/v1/events"
		if ok != want {
			t.Errorf("%s: request context has deadline = %v, want %v", key, ok, want)
		}
	}
	if len(hasDeadline) != 7 {
		t.Errorf("probe saw %d requests, want 7: %v", len(hasDeadline), hasDeadline)
	}
}

// The default stays 30s when no timeout is injected.
func TestRequestTimeoutDefault(t *testing.T) {
	t.Parallel()
	var got time.Duration
	probe := func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if d, ok := r.Context().Deadline(); ok {
				got = time.Until(d)
			}
		})
	}
	f := newGateFixtureWith(t, anyTenantRBACYAML, false, routerDeps{RateLimit: probe})
	f.do(t, adminCaller, http.MethodGet, "/api/v1/me", "")
	if got <= 29*time.Second || got > 30*time.Second {
		t.Errorf("default request deadline %v from now, want ~30s", got)
	}
}
