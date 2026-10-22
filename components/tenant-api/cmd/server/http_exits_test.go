package main

// #3226: the answers the router and the middleware chain write themselves —
// no route, no such method, the request deadline, a refused event stream —
// are the JSON error envelope (code, request_id), through the production
// router where the router writes them.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/handler"
)

// envelopeOf decodes w's body as the error envelope, after checking the
// status, the JSON content type and the request_id every envelope carries.
func envelopeOf(t *testing.T, what string, w *httptest.ResponseRecorder, status int) handler.ErrorResponse {
	t.Helper()
	if w.Code != status {
		t.Fatalf("%s: status %d, want %d; body %q", what, w.Code, status, w.Body.String())
	}
	if ct := w.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Errorf("%s: Content-Type %q, want application/json", what, ct)
	}
	var env handler.ErrorResponse
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("%s: body %q is not the envelope: %v", what, w.Body.String(), err)
	}
	if env.RequestID == "" {
		t.Errorf("%s: envelope has no request_id: %s", what, w.Body.String())
	}
	return env
}

func TestRouter_NoRouteAndNoMethodAreEnvelopes(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, false)

	w := httptest.NewRecorder()
	f.router.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/no-such-route", nil))
	if env := envelopeOf(t, "no route", w, http.StatusNotFound); env.Code != errcode.NotFound {
		t.Errorf("no route: code %q, want %s", env.Code, errcode.NotFound)
	}

	// /api/v1/tenants/{id} serves GET and PUT. The method is refused by the
	// router itself, before the route's RBAC middleware.
	w = httptest.NewRecorder()
	f.router.ServeHTTP(w, httptest.NewRequest(http.MethodPatch, "/api/v1/tenants/alpha-one", nil))
	if env := envelopeOf(t, "no method", w, http.StatusMethodNotAllowed); env.Code != errcode.MethodNotAllowed {
		t.Errorf("no method: code %q, want %s", env.Code, errcode.MethodNotAllowed)
	}
	// chi's own Allow, one header line per method.
	if got := allowOf(w); got != "GET PUT" {
		t.Errorf("no method: Allow %q, want %q", got, "GET PUT")
	}

	// A sub-router's path (/api/v1/...) and a root path alike.
	w = httptest.NewRecorder()
	f.router.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/health", nil))
	envelopeOf(t, "no method on /health", w, http.StatusMethodNotAllowed)
	if got := allowOf(w); got != "GET" {
		t.Errorf("no method on /health: Allow %q, want GET", got)
	}
}

// allowOf is w's Allow header values, sorted, space-separated.
func allowOf(w *httptest.ResponseRecorder) string {
	vs := append([]string(nil), w.Header().Values("Allow")...)
	sort.Strings(vs)
	return strings.Join(vs, " ")
}

// The deadline's 504 is the envelope when the handler returns without having
// answered; an answer the handler wrote stands. The event stream is exempt.
func TestTimeoutExceptStream_DeadlineIsAnEnvelope(t *testing.T) {
	t.Parallel()
	const d = 20 * time.Millisecond
	waitThen := func(answer bool) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			<-r.Context().Done()
			if answer {
				handler.WriteJSONError(w, r, http.StatusServiceUnavailable, "the handler's own answer")
			}
		})
	}
	mw := timeoutExceptStream(d)
	serve := func(h http.Handler, path string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		w := httptest.NewRecorder()
		middleware.RequestID(mw(h)).ServeHTTP(w, req)
		return w
	}

	env := envelopeOf(t, "silent handler", serve(waitThen(false), "/api/v1/tenants"), http.StatusGatewayTimeout)
	if env.Code != errcode.RequestTimeout || env.Error == "" {
		t.Errorf("silent handler: envelope %+v, want %s with a text", env, errcode.RequestTimeout)
	}

	env = envelopeOf(t, "answering handler", serve(waitThen(true), "/api/v1/tenants"), http.StatusServiceUnavailable)
	if env.Error != "the handler's own answer" {
		t.Errorf("answering handler: envelope %+v, want its own answer", env)
	}

	// The stream is not under the deadline: its context is never cancelled
	// by it, so a handler that returns at once answers what it answers.
	w := serve(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, ok := r.Context().Deadline(); ok {
			w.WriteHeader(http.StatusTeapot)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	}), eventsPath)
	if w.Code != http.StatusNoContent {
		t.Errorf("event stream got a deadline: status %d", w.Code)
	}
}

// The production chain recovers panics with handler.Recoverer (an envelope
// 500), not chi's (a bare 500).
func TestRouter_RecoversWithTheEnvelopeRecoverer(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, false)
	want := reflect.ValueOf(handler.Recoverer).Pointer()
	chiOwn := reflect.ValueOf(middleware.Recoverer).Pointer()
	var found bool
	for _, mw := range f.router.Middlewares() {
		switch reflect.ValueOf(mw).Pointer() {
		case want:
			found = true
		case chiOwn:
			t.Error("the router mounts chi's middleware.Recoverer")
		}
	}
	if !found {
		t.Error("the router does not mount handler.Recoverer")
	}
}

// A stream refused because the server is shutting down: 503 envelope with
// Retry-After, through the production router.
func TestEvents_RefusedWhileShuttingDownIsAnEnvelope(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, false)
	f.hub.Shutdown(0)
	req := httptest.NewRequest(http.MethodGet, eventsPath, nil)
	req.Header.Set("X-Forwarded-Email", exactCaller.email())
	req.Header.Set("X-Forwarded-Groups", exactCaller.group)
	w := httptest.NewRecorder()
	f.router.ServeHTTP(w, req)
	env := envelopeOf(t, "shutting down", w, http.StatusServiceUnavailable)
	if env.Code != errcode.Upstream || env.RetryAfterS != 1 || w.Header().Get("Retry-After") != "1" {
		t.Errorf("shutting down: envelope %+v Retry-After %q, want %s with Retry-After 1",
			env, w.Header().Get("Retry-After"), errcode.Upstream)
	}
}
