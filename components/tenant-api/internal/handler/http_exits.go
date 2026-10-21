package handler

// #3226: the answers the router and the middleware chain write themselves —
// no route, no such method on a route, the per-request deadline, a recovered
// panic — go through the error kind table like every handler's, as the JSON
// error envelope with a code and the request_id. chi's own answers for them
// are an empty body (404 is http.NotFound's plain text) and say nothing a
// client can branch on.

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"runtime/debug"
	"time"

	"github.com/vencil/tenant-api/internal/errkind"
)

// msgRouteNotFound / msgRequestTimeout are the fixed texts of the router's
// 404 and the deadline's 504. Neither echoes the request's path.
const (
	msgRouteNotFound = "no such route"
	// msgMethodNotAllowed names no method: the request's method is the
	// client's text, and the Allow header lists the ones the route serves.
	msgMethodNotAllowed = "method not allowed on this route; the Allow header lists the methods it serves"
	msgRequestTimeout   = "the request reached the server's per-request deadline before it was answered; " +
		"a write it made may or may not have taken effect — read before resending"
)

// NotFound answers a request no route matches (chi Mux.NotFound).
func NotFound(w http.ResponseWriter, r *http.Request) {
	WriteKind(w, r, errkind.RouteNotFound, msgRouteNotFound)
}

// MethodNotAllowedEnvelope turns the router's own 405 into the error
// envelope (errkind.MethodNotAllowed). chi's 405 handler is kept: it alone
// knows the methods the matched route serves and sets the Allow header RFC
// 9110 requires on a 405 — from state chi does not hand a custom
// Mux.MethodNotAllowed handler (and Mux.Find cannot recompute it through
// nested sub-routers in chi v5.2.5: it reports the enclosing mount's pattern
// for a method the inner router lacks). What it writes is a bare 405 — no
// Content-Type, an empty body — and that is what this middleware replaces,
// keeping the Allow header. A 405 a handler answers itself goes through
// writeJSON, which sets the Content-Type first, and passes untouched.
// Mount it on the root router, in front of the routes.
func MethodNotAllowedEnvelope(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		next.ServeHTTP(&methodWriter{answerWriter: answerWriter{ResponseWriter: w}, r: r}, r)
	})
}

// methodWriter is MethodNotAllowedEnvelope's writer: an answerWriter that
// answers a bare 405 with the envelope and drops the body chi writes after it.
type methodWriter struct {
	answerWriter
	r        *http.Request
	replaced bool
}

func (w *methodWriter) WriteHeader(code int) {
	if code == http.StatusMethodNotAllowed && !w.wrote && w.Header().Get("Content-Type") == "" {
		w.wrote, w.replaced = true, true
		WriteKind(w.ResponseWriter, w.r, errkind.MethodNotAllowed, msgMethodNotAllowed)
		return
	}
	w.answerWriter.WriteHeader(code)
}

func (w *methodWriter) Write(b []byte) (int, error) {
	if w.replaced {
		return len(b), nil
	}
	return w.answerWriter.Write(b)
}

// answerWriter tracks whether anything of the response was written — a
// status, a body byte, or a flush (which sends the header). It stays an
// http.Flusher (with FlushError) and unwraps, like statusWriter, so the SSE
// stream and http.ResponseController behind it see the connection.
type answerWriter struct {
	http.ResponseWriter
	wrote bool
}

func (w *answerWriter) WriteHeader(code int) {
	if code >= 200 { // a 1xx is not the answer
		w.wrote = true
	}
	w.ResponseWriter.WriteHeader(code)
}

func (w *answerWriter) Write(b []byte) (int, error) {
	w.wrote = true
	return w.ResponseWriter.Write(b)
}

func (w *answerWriter) Flush() {
	w.wrote = true
	if f, ok := w.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func (w *answerWriter) FlushError() error {
	w.wrote = true
	return http.NewResponseController(w.ResponseWriter).Flush()
}

func (w *answerWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }

// Timeout bounds a request by d, as chi's middleware.Timeout does — the
// request's context is cancelled at the deadline, and a handler must return on
// it — but when the handler returns past the deadline without having answered,
// the 504 it writes is the error envelope (errkind.RequestTimeout), not an
// empty body. A handler that answered keeps its answer: chi's version wrote a
// second, ignored WriteHeader(504) after it.
func Timeout(d time.Duration) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ctx, cancel := context.WithTimeout(r.Context(), d)
			defer cancel()
			aw := &answerWriter{ResponseWriter: w}
			r = r.WithContext(ctx)
			next.ServeHTTP(aw, r)
			if ctx.Err() == context.DeadlineExceeded && !aw.wrote {
				WriteKind(w, r, errkind.RequestTimeout, msgRequestTimeout)
			}
		})
	}
}

// Recoverer recovers a panic in the handler chain, as chi's
// middleware.Recoverer does: http.ErrAbortHandler is re-panicked (the
// connection is aborted on purpose), anything else is logged with its stack
// and the request_id. When nothing of the response was written yet, the
// answer is 500 INTERNAL_ERROR in the error envelope (msgInternal, the
// request_id finds the log line) — chi wrote a bare 500. A response already
// under way cannot be replaced: the connection is aborted
// (http.ErrAbortHandler), so the client sees a failed read, not a truncated
// answer it could take for a whole one.
func Recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		aw := &answerWriter{ResponseWriter: w}
		defer func() {
			rvr := recover()
			if rvr == nil {
				return
			}
			if rvr == http.ErrAbortHandler {
				panic(rvr)
			}
			slog.Error("panic serving request", "request_id", requestIDOf(r), "method", r.Method,
				"path", r.URL.Path, "panic", fmt.Sprint(rvr), "stack", string(debug.Stack()))
			if aw.wrote {
				// The answer is under way (a status, a body byte or a
				// flush went out): it cannot be replaced, and ending the
				// handler normally would let the client read a truncated
				// response as complete. Abort the connection instead —
				// net/http closes it (HTTP/2: resets the stream) and logs
				// nothing more for ErrAbortHandler.
				panic(http.ErrAbortHandler)
			}
			if r.Header.Get("Connection") == "Upgrade" {
				return
			}
			WriteKind(w, r, errkind.Internal, msgInternal)
		}()
		next.ServeHTTP(aw, r)
	})
}
