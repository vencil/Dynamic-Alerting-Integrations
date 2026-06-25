package platform

import (
	"net/http"
	"testing"
	"time"
)

// TestNewHTTPClient_IsolatedTransport is the regression guard for #932: the
// forge clients MUST NOT share http.DefaultTransport. A shared pool let
// httptest.Server.Close() — which calls
// http.DefaultTransport.CloseIdleConnections() — flush a connection out from
// under a parallel in-flight request, surfacing as a flaky
// "CloseIdleConnections called" transport error under `-race -count=10`.
// If someone reverts to `&http.Client{Timeout: ...}` (nil Transport →
// DefaultTransport), this test fails.
func TestNewHTTPClient_IsolatedTransport(t *testing.T) {
	t.Parallel()

	c := NewHTTPClient()

	if c.Timeout != 30*time.Second {
		t.Errorf("Timeout = %v, want 30s", c.Timeout)
	}

	tr, ok := c.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("Transport = %T, want a non-nil *http.Transport (nil falls back to the shared DefaultTransport)", c.Transport)
	}
	if tr == http.DefaultTransport {
		t.Fatal("client shares http.DefaultTransport; #932 requires an isolated per-client pool")
	}
}

// TestNewHTTPClient_DistinctPoolsPerClient guards that two clients do not share
// a transport instance, so one client's idle-pool churn cannot disturb another.
func TestNewHTTPClient_DistinctPoolsPerClient(t *testing.T) {
	t.Parallel()

	a := NewHTTPClient()
	b := NewHTTPClient()

	if a.Transport == b.Transport {
		t.Fatal("two NewHTTPClient() instances share one *http.Transport; pools must be per-client (#932)")
	}
}
