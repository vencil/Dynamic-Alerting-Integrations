package handler

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/ws"
)

// exitEnvelope decodes w's body as the error envelope with status.
func exitEnvelope(t *testing.T, what string, w *httptest.ResponseRecorder, status int) ErrorResponse {
	t.Helper()
	if w.Code != status {
		t.Fatalf("%s: status %d, want %d; body %q", what, w.Code, status, w.Body.String())
	}
	var env ErrorResponse
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("%s: body %q is not the envelope: %v", what, w.Body.String(), err)
	}
	return env
}

// #3226: a panic before anything was written is answered 500 INTERNAL_ERROR
// in the envelope, with the request_id the log line carries; a response
// already under way is left as it is; http.ErrAbortHandler still aborts.
func TestRecoverer_AnswersAPanicInTheEnvelope(t *testing.T) {
	t.Parallel()
	serve := func(h http.HandlerFunc) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		middleware.RequestID(Recoverer(h)).ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))
		return w
	}

	w := serve(func(http.ResponseWriter, *http.Request) { panic("boom /srv/secret") })
	env := exitEnvelope(t, "panic", w, http.StatusInternalServerError)
	if env.Code != CodeInternal || env.Error != msgInternal || env.RequestID == "" {
		t.Errorf("panic: envelope %+v, want %s, msgInternal and a request_id", env, CodeInternal)
	}

	// A panic after the answer began aborts the connection: the panic that
	// leaves Recoverer is http.ErrAbortHandler, and nothing is appended.
	func() {
		rec := httptest.NewRecorder()
		defer func() {
			if rvr := recover(); rvr != http.ErrAbortHandler {
				t.Errorf("panic after the answer began: recovered %v, want http.ErrAbortHandler", rvr)
			}
			if rec.Body.String() != "partial" {
				t.Errorf("panic after the answer began: body %q, want nothing appended", rec.Body.String())
			}
		}()
		middleware.RequestID(Recoverer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusAccepted)
			_, _ = w.Write([]byte("partial"))
			panic("late")
		}))).ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/x", nil))
	}()

	func() {
		defer func() {
			if rvr := recover(); rvr != http.ErrAbortHandler {
				t.Errorf("recovered %v, want http.ErrAbortHandler re-panicked", rvr)
			}
		}()
		serve(func(http.ResponseWriter, *http.Request) { panic(http.ErrAbortHandler) })
	}()

	// The writer it hands on keeps what the SSE stream needs.
	Recoverer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if _, ok := w.(http.Flusher); !ok {
			t.Error("Recoverer's writer is not an http.Flusher")
		}
		if u, ok := w.(interface{ Unwrap() http.ResponseWriter }); !ok || u.Unwrap() == nil {
			t.Error("Recoverer's writer does not unwrap (http.ResponseController needs it)")
		}
	})).ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/x", nil))
}

// #3226: past the deadline, a handler that answered nothing gets the 504
// envelope; one that answered — even after the deadline — keeps its answer.
func TestTimeout_EnvelopeOnlyWhenUnanswered(t *testing.T) {
	t.Parallel()
	const d = 20 * time.Millisecond
	serve := func(h http.HandlerFunc) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		middleware.RequestID(Timeout(d)(h)).ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))
		return w
	}

	w := serve(func(_ http.ResponseWriter, r *http.Request) { <-r.Context().Done() })
	env := exitEnvelope(t, "unanswered", w, http.StatusGatewayTimeout)
	if env.Code != CodeUpstream || env.Error != msgRequestTimeout || env.RequestID == "" || w.Header().Get("Retry-After") != "" {
		t.Errorf("unanswered: envelope %+v Retry-After %q, want %s, msgRequestTimeout, no Retry-After",
			env, w.Header().Get("Retry-After"), CodeUpstream)
	}

	w = serve(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
		w.WriteHeader(http.StatusNoContent)
	})
	if w.Code != http.StatusNoContent || w.Body.Len() != 0 {
		t.Errorf("answered after the deadline: %d %q, want the handler's 204", w.Code, w.Body.String())
	}

	w = serve(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok")) })
	if w.Code != http.StatusOK || w.Body.String() != "ok" {
		t.Errorf("answered in time: %d %q", w.Code, w.Body.String())
	}
}

// #3226: the router's bare 405 becomes the envelope and keeps chi's Allow;
// a 405 a handler answers in the envelope itself passes untouched.
func TestMethodNotAllowedEnvelope(t *testing.T) {
	t.Parallel()
	serve := func(h http.HandlerFunc) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		middleware.RequestID(MethodNotAllowedEnvelope(h)).ServeHTTP(w, httptest.NewRequest(http.MethodPatch, "/x", nil))
		return w
	}

	w := serve(func(w http.ResponseWriter, _ *http.Request) { // chi's methodNotAllowedHandler
		w.Header().Add("Allow", "GET")
		w.Header().Add("Allow", "PUT")
		w.WriteHeader(http.StatusMethodNotAllowed)
		_, _ = w.Write(nil)
	})
	env := exitEnvelope(t, "bare 405", w, http.StatusMethodNotAllowed)
	if env.Code != CodeBadRequest || env.RequestID == "" || len(w.Header().Values("Allow")) != 2 {
		t.Errorf("bare 405: envelope %+v Allow %q, want %s and both Allow values", env, w.Header().Values("Allow"), CodeBadRequest)
	}

	w = serve(func(w http.ResponseWriter, r *http.Request) {
		WriteJSONErrorWithCode(w, r, http.StatusMethodNotAllowed, "OWN_CODE", "the handler's own 405")
	})
	if env := exitEnvelope(t, "own 405", w, http.StatusMethodNotAllowed); env.Code != "OWN_CODE" {
		t.Errorf("own 405 rewritten: %+v", env)
	}
}

// #3226: the event stream's refusals are envelopes — 500 when the writer
// cannot flush, 503 with Retry-After while shutting down.
func TestRefuseEventStream(t *testing.T) {
	t.Parallel()
	serve := func(err error) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		middleware.RequestID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			refuseEventStream(w, r, err)
		})).ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/events", nil))
		return w
	}
	env := exitEnvelope(t, "no flusher", serve(ws.ErrStreamingUnsupported), http.StatusInternalServerError)
	if env.Code != CodeInternal || env.Error != msgStreamingUnsupported {
		t.Errorf("no flusher: %+v, want %s with msgStreamingUnsupported shown", env, CodeInternal)
	}
	w := serve(ws.ErrShuttingDown)
	env = exitEnvelope(t, "shutting down", w, http.StatusServiceUnavailable)
	if env.Code != CodeUpstream || env.RetryAfterS != 1 || w.Header().Get("Retry-After") != "1" {
		t.Errorf("shutting down: %+v Retry-After %q, want %s with Retry-After 1", env, w.Header().Get("Retry-After"), CodeUpstream)
	}
}

// #3226: over a real connection, a panic after a flushed partial answer
// fails the client's read — the response is chunked and its terminating
// chunk is never sent — so the truncated body is never read as whole.
func TestRecoverer_PanicAfterFlushFailsAChunkedRead(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(Recoverer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("partial"))
		w.(http.Flusher).Flush()
		panic("late")
	})))
	defer srv.Close()
	resp, err := srv.Client().Get(srv.URL)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	if body, rerr := io.ReadAll(resp.Body); rerr == nil {
		t.Errorf("client read %q with no error; want the aborted connection to fail the read", body)
	}
}
