package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/go-chi/chi/v5"
	chimw "github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/recipepreview"
)

// fakePreviewer stands in for the recipe-preview container's client.
type fakePreviewer struct {
	verdict  json.RawMessage
	err      error
	got      *recipepreview.Request
	deadline time.Time
	called   int
}

func (f *fakePreviewer) Preview(ctx context.Context, req recipepreview.Request) (json.RawMessage, error) {
	f.called++
	r := req
	f.got = &r
	f.deadline, _ = ctx.Deadline()
	return f.verdict, f.err
}

const previewVerdict = `{"alertname":"Custom_q","supported":true,"states":[{"severity":"warning","state":"firing","reason":"r"}],"warnings":[]}`

// previewRouter wires POST /api/v1/tenants/{id}/recipe-preview behind the
// same read middleware the real server uses, so these tests exercise the
// actual authorization decision (not a stub). RequestID mirrors the
// router-wide production chain (cmd/server/routes.go) so the envelope's
// request_id is populated — the exact-key-set pins below cover it.
func previewRouter(rbacMgr *rbac.Manager, d *Deps) chi.Router {
	r := chi.NewRouter()
	r.Use(chimw.RequestID)
	r.With(rbac.ResolveIdentity(rbacMgr.HeaderResolver()), rbacMgr.Middleware(rbac.PermRead, TenantIDFromPath)).
		Post("/api/v1/tenants/{id}/recipe-preview", PreviewRecipe(d))
	return r
}

// assertExactEnvelopeKeys pins the FULL JSON key set of an error body — not
// subset-presence. The rbac middleware mirrors handler.ErrorResponse by value
// (depguard forbids rbac → handler) and the contract fuzz cannot observe
// middleware responses (wildcard-RBAC fixture), so a universally-emitted
// field added to the handler envelope (or request_id flipping to required)
// with the rbac mirror silently missing it must turn a test red HERE.
func assertExactEnvelopeKeys(t *testing.T, body []byte, want ...string) {
	t.Helper()
	var m map[string]json.RawMessage
	if err := json.Unmarshal(body, &m); err != nil {
		t.Fatalf("decode error body: %v", err)
	}
	got := make([]string, 0, len(m))
	for k := range m {
		got = append(got, k)
	}
	sort.Strings(got)
	sort.Strings(want)
	if !reflect.DeepEqual(got, want) {
		t.Errorf("envelope key set = %v, want exactly %v", got, want)
	}
}

const previewAdmins = `groups:
  - name: admins
    tenants: ["*"]
    permissions: [read, write, admin]
`

func previewRequest(tenant, body string, identity bool) *http.Request {
	req := httptest.NewRequest("POST", "/api/v1/tenants/"+tenant+"/recipe-preview", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if identity {
		req.Header.Set("X-Forwarded-Email", "test@example.com")
		req.Header.Set("X-Forwarded-Groups", "admins")
	}
	// The request timeout middleware gives every request a deadline.
	ctx, cancel := context.WithTimeout(req.Context(), 30*time.Second)
	_ = cancel // released when the test ends; the context outlives the call
	return req.WithContext(ctx)
}

const previewBody = `{"recipe":{"recipe":"threshold","metric":"q","threshold":"1:warning"},"scenario":{"value":5}}`

func TestPreviewRecipe_PassesTheVerdictThrough(t *testing.T) {
	t.Parallel()
	fp := &fakePreviewer{verdict: json.RawMessage(previewVerdict)}
	w := httptest.NewRecorder()
	previewRouter(newRBACManager(t, previewAdmins), &Deps{RecipePreview: fp}).
		ServeHTTP(w, previewRequest("db-a", previewBody, true))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, body: %s", w.Code, w.Body.String())
	}
	if strings.TrimSpace(w.Body.String()) != previewVerdict {
		t.Fatalf("body = %s", w.Body.String())
	}
	if fp.got.Tenant != "db-a" || !strings.Contains(string(fp.got.Recipe), `"metric":"q"`) ||
		string(fp.got.Scenario) != `{"value":5}` {
		t.Fatalf("request to the container = %+v", fp.got)
	}
}

// The tenant comes from the path the caller was authorized for, never from
// the body: a body naming another tenant cannot preview it.
func TestPreviewRecipe_TheTenantIsThePathOne(t *testing.T) {
	t.Parallel()
	fp := &fakePreviewer{verdict: json.RawMessage(previewVerdict)}
	w := httptest.NewRecorder()
	body := `{"tenant":"db-other","recipe":{"recipe":"threshold"},"scenario":{}}`
	previewRouter(newRBACManager(t, previewAdmins), &Deps{RecipePreview: fp}).
		ServeHTTP(w, previewRequest("db-a", body, true))
	if w.Code != http.StatusBadRequest || fp.called != 0 {
		t.Fatalf("status = %d (called %d), want 400 for an unknown field; body: %s", w.Code, fp.called, w.Body.String())
	}
}

// The container's time is the request's, less what tenant-api needs to
// answer: it never computes for a caller that has stopped waiting.
func TestPreviewRecipe_TheContainerGetsTheRequestDeadlineLessTheAnswerMargin(t *testing.T) {
	t.Parallel()
	fp := &fakePreviewer{verdict: json.RawMessage(previewVerdict)}
	req := previewRequest("db-a", previewBody, true)
	reqDeadline, _ := req.Context().Deadline()
	w := httptest.NewRecorder()
	previewRouter(newRBACManager(t, previewAdmins), &Deps{RecipePreview: fp}).ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d", w.Code)
	}
	if fp.deadline.IsZero() {
		t.Fatal("the container call has no deadline")
	}
	if got := reqDeadline.Sub(fp.deadline); got < previewAnswerMargin-50*time.Millisecond || got > previewAnswerMargin+time.Second {
		t.Fatalf("container deadline is %s before the request's, want about %s", got, previewAnswerMargin)
	}
}

func TestPreviewRecipe_ValidatesTheBody(t *testing.T) {
	t.Parallel()
	for name, body := range map[string]string{
		"not json":         `{`,
		"no recipe":        `{"scenario":{}}`,
		"recipe not obj":   `{"recipe":"x"}`,
		"empty recipe":     `{"recipe":{}}`,
		"scenario not obj": `{"recipe":{"recipe":"threshold"},"scenario":5}`,
		"unknown field":    `{"recipe":{"recipe":"threshold"},"deadline_ms":99999999}`,
	} {
		t.Run(name, func(t *testing.T) {
			fp := &fakePreviewer{verdict: json.RawMessage(previewVerdict)}
			w := httptest.NewRecorder()
			previewRouter(newRBACManager(t, previewAdmins), &Deps{RecipePreview: fp}).
				ServeHTTP(w, previewRequest("db-a", body, true))
			if w.Code != http.StatusBadRequest || fp.called != 0 {
				t.Fatalf("status = %d (called %d), body: %s", w.Code, fp.called, w.Body.String())
			}
		})
	}
}

func TestPreviewRecipe_MapsTheContainerOutcomes(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name       string
		err        error
		status     int
		code       errcode.Code
		reason     string
		retryAfter string
	}{
		{"busy", &recipepreview.BusyError{Reason: recipepreview.ReasonBusy}, 429, errcode.PreviewBusy, "busy", "5"},
		{"deadline", &recipepreview.BusyError{Reason: recipepreview.ReasonDeadline}, 429, errcode.PreviewBusy, "deadline", "5"},
		{"no time", &recipepreview.BusyError{Reason: recipepreview.ReasonNoTime}, 429, errcode.PreviewBusy, "no_time", "5"},
		{"rate limited", &recipepreview.RateLimitedError{Message: "rate limit exceeded for this tenant"}, 429, errcode.RateLimited, "", "60"},
		{"rejected", &recipepreview.RejectedError{Status: 400, Message: "`scenario` must be an object"}, 400, errcode.BadRequest, "", ""},
		{"unreachable", &recipepreview.UnavailableError{Reason: recipepreview.ReasonUnreachable, Detail: "dial unix /run/x: no such file"}, 503, errcode.PreviewUnavailable, "unreachable", "5"},
		{"internal", &recipepreview.UnavailableError{Reason: recipepreview.ReasonInternalError, Detail: "500"}, 503, errcode.PreviewUnavailable, "internal_error", "5"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			fp := &fakePreviewer{err: c.err}
			w := httptest.NewRecorder()
			previewRouter(newRBACManager(t, previewAdmins), &Deps{RecipePreview: fp}).
				ServeHTTP(w, previewRequest("db-a", previewBody, true))
			if w.Code != c.status {
				t.Fatalf("status = %d, want %d; body: %s", w.Code, c.status, w.Body.String())
			}
			var env map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
				t.Fatal(err)
			}
			if env["code"] != string(c.code) {
				t.Fatalf("code = %v, want %s", env["code"], c.code)
			}
			if c.reason != "" && env["reason"] != c.reason {
				t.Fatalf("reason = %v, want %s", env["reason"], c.reason)
			}
			if got := w.Header().Get("Retry-After"); got != c.retryAfter {
				t.Fatalf("Retry-After = %q, want %q", got, c.retryAfter)
			}
			// The socket path in a transport error is the server's, not the client's.
			if strings.Contains(w.Body.String(), "/run/x") {
				t.Fatalf("server detail leaked: %s", w.Body.String())
			}
		})
	}
}

func TestPreviewRecipe_NotConfigured(t *testing.T) {
	t.Parallel()
	w := httptest.NewRecorder()
	previewRouter(newRBACManager(t, previewAdmins), &Deps{}).
		ServeHTTP(w, previewRequest("db-a", previewBody, true))
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, body: %s", w.Code, w.Body.String())
	}
	var env map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &env)
	if env["code"] != string(errcode.PreviewNotConfigured) || w.Header().Get("Retry-After") != "" {
		t.Fatalf("env = %v, Retry-After %q", env, w.Header().Get("Retry-After"))
	}
}

// ── the route's read gate (moved here from the /access probe it replaces) ──

func TestPreviewRecipe_Forbidden(t *testing.T) {
	t.Parallel()
	// db-ops can read db-b-* only; asking about db-a must be denied.
	rbacMgr := newRBACManager(t, `groups:
  - name: db-ops
    tenants: ["db-b-*"]
    permissions: [read]
`)
	fp := &fakePreviewer{verdict: json.RawMessage(previewVerdict)}
	req := previewRequest("db-a", previewBody, false)
	req.Header.Set("X-Forwarded-Email", "test@example.com")
	req.Header.Set("X-Forwarded-Groups", "db-ops")
	w := httptest.NewRecorder()
	previewRouter(rbacMgr, &Deps{RecipePreview: fp}).ServeHTTP(w, req)

	if w.Code != http.StatusForbidden || fp.called != 0 {
		t.Fatalf("status = %d (called %d), want 403; body: %s", w.Code, fp.called, w.Body.String())
	}
	// Unified-envelope alignment pin (depguard forbids rbac → handler, so the
	// rbac middleware mirrors the envelope SHAPE by value — this test,
	// running the REAL middleware from the allowed direction, is what keeps
	// the copy honest).
	assertExactEnvelopeKeys(t, w.Body.Bytes(), "error", "code", "request_id", "help", "action")
}

func TestPreviewRecipe_Unauthorized(t *testing.T) {
	t.Parallel()
	fp := &fakePreviewer{verdict: json.RawMessage(previewVerdict)}
	w := httptest.NewRecorder()
	previewRouter(newRBACManager(t, previewAdmins), &Deps{RecipePreview: fp}).
		ServeHTTP(w, previewRequest("db-a", previewBody, false))
	if w.Code != http.StatusUnauthorized || fp.called != 0 {
		t.Fatalf("status = %d (called %d), want 401", w.Code, fp.called)
	}
	assertExactEnvelopeKeys(t, w.Body.Bytes(), "error", "code", "request_id")
	var resp map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["error"] != "missing identity: the request carries no authenticated caller (oauth2-proxy injected no email header)" {
		t.Errorf("401 error = %q, want the middleware's message unchanged", resp["error"])
	}
}

func TestPreviewRecipe_NoRulesDenies(t *testing.T) {
	t.Parallel()
	// There is no open mode (ADR-027 §7): path-less and configured-but-empty
	// _rbac.yaml both answer 403 before the container is asked.
	for name, yaml := range map[string]string{"path-less": "", "empty": "groups: []\n"} {
		t.Run(name, func(t *testing.T) {
			fp := &fakePreviewer{verdict: json.RawMessage(previewVerdict)}
			req := previewRequest("db-a", previewBody, false)
			req.Header.Set("X-Forwarded-Email", "test@example.com")
			w := httptest.NewRecorder()
			previewRouter(newRBACManager(t, yaml), &Deps{RecipePreview: fp}).ServeHTTP(w, req)
			if w.Code != http.StatusForbidden || fp.called != 0 {
				t.Fatalf("status = %d (called %d), want 403", w.Code, fp.called)
			}
		})
	}
}

func TestPreviewRecipe_EmptyID_FailsClosed(t *testing.T) {
	t.Parallel()
	// The RBAC middleware authorizes an EMPTY id under a "*" grant, but "" is
	// not a real tenant: the handler must refuse it (400) before asking.
	fp := &fakePreviewer{verdict: json.RawMessage(previewVerdict)}
	req := previewRequest("", previewBody, true)
	rctx := chi.NewRouteContext()
	rctx.URLParams.Add("id", "")
	req = req.WithContext(context.WithValue(req.Context(), chi.RouteCtxKey, rctx))
	w := httptest.NewRecorder()
	PreviewRecipe(&Deps{RecipePreview: fp}).ServeHTTP(w, req)
	if w.Code != http.StatusBadRequest || fp.called != 0 {
		t.Fatalf("empty-id status = %d (called %d), want 400", w.Code, fp.called)
	}
}
