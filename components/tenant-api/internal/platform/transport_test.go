package platform

// Tests for JSONRoundTrip — the shared forge transport. The main target
// is the TRK-319 rate-limit branch (transport.go:101-104): a 429/403
// rate-limit response must come back as an *APIError whose RateLimited /
// RetryAfter fields feed the circuit breaker's degradation signal
// (isForgeDegradation), while a plain permission 403 stays a
// deterministic client error. The no-leak contract (upstream body never
// on the error) is asserted on every non-2xx case.

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// roundTrip runs JSONRoundTrip against srv with a recognizable auth stamp.
func roundTrip(t *testing.T, srv *httptest.Server, method string, body interface{}) ([]byte, http.Header, error) {
	t.Helper()
	client := NewHTTPClient(5 * time.Second)
	defer client.Transport.(*http.Transport).CloseIdleConnections()
	return JSONRoundTrip(client, "GitHub", srv.URL, method, "/repos/o/r/pulls", body, func(h http.Header) {
		h.Set("Authorization", "Bearer test-token")
	})
}

func TestJSONRoundTrip_Success(t *testing.T) {
	t.Parallel()
	var gotAuth, gotContentType string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotContentType = r.Header.Get("Content-Type")
		w.Header().Set("X-Probe", "yes")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	body, headers, err := roundTrip(t, srv, "POST", map[string]string{"k": "v"})
	if err != nil {
		t.Fatalf("JSONRoundTrip: %v", err)
	}
	if string(body) != `{"ok":true}` {
		t.Errorf("body = %q", body)
	}
	if headers.Get("X-Probe") != "yes" {
		t.Error("response headers not returned")
	}
	if gotAuth != "Bearer test-token" {
		t.Errorf("setAuth not applied: Authorization = %q", gotAuth)
	}
	if gotContentType != "application/json" {
		t.Errorf("Content-Type = %q, want application/json for a JSON body", gotContentType)
	}
}

// A nil body must not fabricate a Content-Type header.
func TestJSONRoundTrip_NilBodyNoContentType(t *testing.T) {
	t.Parallel()
	var gotContentType string
	var hadBody bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotContentType = r.Header.Get("Content-Type")
		hadBody = r.ContentLength > 0
		_, _ = w.Write([]byte(`[]`))
	}))
	defer srv.Close()

	if _, _, err := roundTrip(t, srv, "GET", nil); err != nil {
		t.Fatalf("JSONRoundTrip: %v", err)
	}
	if gotContentType != "" || hadBody {
		t.Errorf("nil body sent Content-Type %q / body=%v, want neither", gotContentType, hadBody)
	}
}

// An unmarshalable body fails BEFORE any network call.
func TestJSONRoundTrip_MarshalErrorNoRequest(t *testing.T) {
	t.Parallel()
	var hits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits++ }))
	defer srv.Close()

	_, _, err := roundTrip(t, srv, "POST", map[string]interface{}{"bad": make(chan int)})
	if err == nil || !strings.Contains(err.Error(), "marshal body") {
		t.Fatalf("err = %v, want marshal-body failure", err)
	}
	if hits != 0 {
		t.Errorf("server hit %d times for an unmarshalable body, want 0", hits)
	}
}

// TestJSONRoundTrip_RateLimit429 is the TRK-319 wiring test: a 429 with
// Retry-After must propagate the rate-limit degradation signal — the
// APIError carries RateLimited=true and the server-advised back-off, and
// isForgeDegradation treats it as forge degradation (so the circuit
// breaker protects the write plane instead of sailing through a "4xx").
func TestJSONRoundTrip_RateLimit429(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Retry-After", "7")
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"message":"API rate limit exceeded for installation ID 12345"}`))
	}))
	defer srv.Close()

	_, headers, err := roundTrip(t, srv, "GET", nil)
	if err == nil {
		t.Fatal("expected an error for a 429 response")
	}
	var apiErr *APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("err = %T (%v), want *APIError", err, err)
	}
	if apiErr.StatusCode != http.StatusTooManyRequests {
		t.Errorf("StatusCode = %d, want 429", apiErr.StatusCode)
	}
	if !apiErr.RateLimited {
		t.Error("RateLimited = false for a 429, want true — the breaker would miss the degradation window")
	}
	if apiErr.RetryAfter != 7*time.Second {
		t.Errorf("RetryAfter = %v, want 7s from the Retry-After header", apiErr.RetryAfter)
	}
	if !isForgeDegradation(err) {
		t.Error("isForgeDegradation = false for a rate-limited response, want true")
	}
	// Response headers are still handed back alongside the error.
	if headers.Get("Retry-After") != "7" {
		t.Errorf("headers lost on the error path: Retry-After = %q", headers.Get("Retry-After"))
	}
	// No-leak contract: the upstream body never reaches the error string.
	if strings.Contains(err.Error(), "rate limit exceeded") || strings.Contains(err.Error(), "12345") {
		t.Errorf("error leaked upstream body: %v", err)
	}
}

// A GitHub SECONDARY rate limit arrives as a 403 — indistinguishable from
// a permission 403 by status alone. With a Retry-After signal it must be
// flagged RateLimited (degradation), and must NOT match ErrForbidden:
// surfacing a transient back-off as "insufficient permissions" would send
// the operator chasing token scopes.
func TestJSONRoundTrip_SecondaryRateLimit403(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Retry-After", "60")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"message":"You have exceeded a secondary rate limit"}`))
	}))
	defer srv.Close()

	_, _, err := roundTrip(t, srv, "POST", map[string]string{"k": "v"})
	var apiErr *APIError
	if !errors.As(err, &apiErr) || !apiErr.RateLimited {
		t.Fatalf("err = %v, want a RateLimited APIError for a secondary-rate-limit 403", err)
	}
	if apiErr.RetryAfter != 60*time.Second {
		t.Errorf("RetryAfter = %v, want 60s", apiErr.RetryAfter)
	}
	if errors.Is(err, ErrForbidden) {
		t.Error("a rate-limited 403 matched ErrForbidden — it must stay on the degradation path, not the permission path")
	}
	if !isForgeDegradation(err) {
		t.Error("isForgeDegradation = false for a secondary rate limit, want true")
	}
}

// A plain permission 403 (no rate-limit signal anywhere) stays a
// deterministic client error: ErrForbidden matches, the breaker is NOT
// fed a degradation signal.
func TestJSONRoundTrip_PermissionForbidden403(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"message":"Resource not accessible by personal access token"}`))
	}))
	defer srv.Close()

	_, _, err := roundTrip(t, srv, "POST", map[string]string{"k": "v"})
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.RateLimited {
		t.Fatalf("err = %v, want a non-RateLimited APIError for a permission 403", err)
	}
	if !errors.Is(err, ErrForbidden) {
		t.Errorf("errors.Is(err, ErrForbidden) = false, want true: %v", err)
	}
	if isForgeDegradation(err) {
		t.Error("isForgeDegradation = true for a permission 403 — a deterministic client error must not trip the breaker")
	}
}

// A transport-level failure (server unreachable) wraps as "http request".
func TestJSONRoundTrip_TransportError(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	srv.Close() // immediately: connection refused

	_, _, err := roundTrip(t, srv, "GET", nil)
	if err == nil || !strings.Contains(err.Error(), "http request") {
		t.Fatalf("err = %v, want http-request transport failure", err)
	}
	if !isForgeDegradation(err) {
		t.Error("isForgeDegradation = false for a transport error, want true")
	}
}
