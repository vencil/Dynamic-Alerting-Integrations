package recipepreview

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// serve answers on a Unix socket in a temp dir with h and returns the path.
func serve(t *testing.T, h http.HandlerFunc) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "preview.sock")
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{Handler: h, ReadHeaderTimeout: 5 * time.Second}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })
	return path
}

func answer(status int, body string) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}
}

var req = Request{
	Recipe:   json.RawMessage(`{"recipe":"threshold","metric":"m"}`),
	Tenant:   "shop-a",
	Scenario: json.RawMessage(`{"value":1500}`),
}

func TestPreview_SendsTheRequestAndTheTimeLeft(t *testing.T) {
	var got map[string]any
	var path, method string
	sock := serve(t, func(w http.ResponseWriter, r *http.Request) {
		path, method = r.URL.Path, r.Method
		_ = json.NewDecoder(r.Body).Decode(&got)
		answer(200, `{"alertname":"Custom_x","supported":true,"states":[],"warnings":[]}`)(w, r)
	})
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	verdict, err := NewClient(sock).Preview(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if method != http.MethodPost || path != "/v1/preview" {
		t.Fatalf("%s %s", method, path)
	}
	if got["tenant"] != "shop-a" || got["recipe"].(map[string]any)["metric"] != "m" ||
		got["scenario"].(map[string]any)["value"] != 1500.0 {
		t.Fatalf("request = %v", got)
	}
	ms, ok := got["deadline_ms"].(float64)
	if !ok || ms <= 0 || ms > 4000 {
		t.Fatalf("deadline_ms = %v, want the time left on ctx (0, 4000]", got["deadline_ms"])
	}
	var v map[string]any
	if err := json.Unmarshal(verdict, &v); err != nil || v["alertname"] != "Custom_x" {
		t.Fatalf("verdict = %s (%v)", verdict, err)
	}
}

func TestPreview_RefusesAContextWithoutADeadline(t *testing.T) {
	sock := serve(t, answer(200, `{}`))
	_, err := NewClient(sock).Preview(context.Background(), req)
	var be *BusyError
	if !errors.As(err, &be) || be.Reason != ReasonNoTime {
		t.Fatalf("err = %v, want BusyError no_time", err)
	}
}

func TestPreview_RefusesTooLittleTime(t *testing.T) {
	called := false
	sock := serve(t, func(w http.ResponseWriter, r *http.Request) { called = true; answer(200, `{}`)(w, r) })
	ctx, cancel := context.WithTimeout(context.Background(), MinBudget/2)
	defer cancel()
	_, err := NewClient(sock).Preview(ctx, req)
	var be *BusyError
	if !errors.As(err, &be) || be.Reason != ReasonNoTime {
		t.Fatalf("err = %v, want BusyError no_time", err)
	}
	if called {
		t.Fatal("asked the service with no time to answer")
	}
}

func TestPreview_MapsTheServiceAnswers(t *testing.T) {
	cases := []struct {
		name   string
		status int
		body   string
		check  func(error) bool
	}{
		{"busy", 503, `{"error":"busy","reason":"busy"}`, func(err error) bool {
			var e *BusyError
			return errors.As(err, &e) && e.Reason == ReasonBusy
		}},
		{"deadline", 503, `{"error":"late","reason":"deadline"}`, func(err error) bool {
			var e *BusyError
			return errors.As(err, &e) && e.Reason == ReasonDeadline
		}},
		{"rate limited", 429, `{"error":"rate limit exceeded for this tenant"}`, func(err error) bool {
			var e *RateLimitedError
			return errors.As(err, &e)
		}},
		{"bad request", 400, `{"error":"` + "`recipe` (object) is required" + `"}`, func(err error) bool {
			var e *RejectedError
			return errors.As(err, &e) && e.Message == "`recipe` (object) is required"
		}},
		{"too large", 413, `{"error":"request body too large"}`, func(err error) bool {
			var e *TooLargeError
			return errors.As(err, &e) && e.Limit == MaxRequestBytes
		}},
		{"verdict null", 200, `null`, func(err error) bool {
			var e *UnavailableError
			return errors.As(err, &e) && e.Reason == ReasonBadResponse
		}},
		{"internal", 500, `{"error":"internal error"}`, func(err error) bool {
			var e *UnavailableError
			return errors.As(err, &e) && e.Reason == ReasonInternalError
		}},
		{"verdict not an object", 200, `[1,2]`, func(err error) bool {
			var e *UnavailableError
			return errors.As(err, &e) && e.Reason == ReasonBadResponse
		}},
		{"unknown 503 reason", 503, `{"error":"x"}`, func(err error) bool {
			var e *UnavailableError
			return errors.As(err, &e) && e.Reason == ReasonBadResponse
		}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			sock := serve(t, answer(c.status, c.body))
			ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
			defer cancel()
			_, err := NewClient(sock).Preview(ctx, req)
			if !c.check(err) {
				t.Fatalf("err = %#v", err)
			}
		})
	}
}

// The body as sent carries tenant and deadline_ms on top of the caller's:
// a request under the limit before forwarding can be over it after, and is
// refused here rather than sent for the service to answer 413.
func TestPreview_RefusesARequestOverTheLimitAsSent(t *testing.T) {
	asked := false
	sock := serve(t, func(w http.ResponseWriter, r *http.Request) { asked = true })
	big := Request{
		Recipe: json.RawMessage(`{"recipe":"threshold","name":"` + strings.Repeat("x", MaxRequestBytes-64) + `"}`),
		Tenant: "db-a",
	}
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	_, err := NewClient(sock).Preview(ctx, big)
	var e *TooLargeError
	if !errors.As(err, &e) || asked {
		t.Fatalf("err = %v, asked = %v", err, asked)
	}
}

func TestPreview_NobodyListening(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	_, err := NewClient(filepath.Join(t.TempDir(), "none.sock")).Preview(ctx, req)
	var e *UnavailableError
	if !errors.As(err, &e) || e.Reason != ReasonUnreachable {
		t.Fatalf("err = %v", err)
	}
}

func TestPreview_TheCallerDeadlineEndsTheWait(t *testing.T) {
	release := make(chan struct{})
	sock := serve(t, func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-release:
		case <-r.Context().Done():
		}
	})
	defer close(release)
	ctx, cancel := context.WithTimeout(context.Background(), MinBudget+300*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, err := NewClient(sock).Preview(ctx, req)
	var be *BusyError
	if !errors.As(err, &be) || be.Reason != ReasonDeadline {
		t.Fatalf("err = %v, want BusyError deadline", err)
	}
	if took := time.Since(start); took > 3*time.Second {
		t.Fatalf("waited %s past the caller's deadline", took)
	}
}
