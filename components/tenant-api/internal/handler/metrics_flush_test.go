package handler

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

// The writer MetricsMiddleware hands to every handler must stay an
// http.Flusher: GET /api/v1/events refuses a writer that is not one with 500
// "streaming not supported", and the production router mounts this middleware
// in front of it. Built directly (no MetricsMiddleware call) so the test does
// not touch the package-level request counters the sequential tests above
// assert deltas on.
func TestStatusWriter_KeepsFlusherAndUnwrap(t *testing.T) {
	t.Parallel()
	rec := httptest.NewRecorder()
	var w http.ResponseWriter = &statusWriter{ResponseWriter: rec}

	f, ok := w.(http.Flusher)
	if !ok {
		t.Fatal("statusWriter is not an http.Flusher — SSE behind MetricsMiddleware answers 500")
	}
	f.Flush()
	if !rec.Flushed {
		t.Error("Flush did not reach the wrapped writer")
	}
	u, ok := w.(interface{ Unwrap() http.ResponseWriter })
	if !ok || u.Unwrap() != rec {
		t.Error("statusWriter does not unwrap to the wrapped writer (http.ResponseController needs it)")
	}
}

// failingFlushWriter is a connection whose flush fails (a client gone away).
type failingFlushWriter struct{ *httptest.ResponseRecorder }

func (failingFlushWriter) FlushError() error { return errFlushFailed }

var errFlushFailed = errors.New("flush to a dead connection")

// http.ResponseController.Flush must report the wrapped writer's flush error
// through statusWriter: the SSE stream ends a subscription on it. Without
// FlushError the controller stops at statusWriter.Flush and reports nil.
func TestStatusWriter_ForwardsFlushError(t *testing.T) {
	t.Parallel()
	w := &statusWriter{ResponseWriter: failingFlushWriter{httptest.NewRecorder()}}
	if err := http.NewResponseController(w).Flush(); !errors.Is(err, errFlushFailed) {
		t.Errorf("ResponseController.Flush through statusWriter = %v, want %v", err, errFlushFailed)
	}
}
