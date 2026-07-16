package handler

// Tests for the /ready human-plane socket self-dial (ADR-027 D2-B §2.5).
// The kubelet only probes the TCP plane, so this self-dial is the ONLY
// mechanism that turns a dead human UDS listener into a visible NotReady
// instead of a silent outage — which makes the dial itself the thing
// most worth testing.
//
// The tests bind real Unix domain sockets. The dev container (Linux) is
// the intended runner; on a platform where binding a UDS fails (e.g. an
// older Windows host), the socket tests skip with an explanation, per
// the package convention for platform-dependent facilities.
//
// NOT parallel (the socket subtests): Ready writes the process-global
// humanSocketUp gauge via SetHumanSocketUp; state is saved/restored the
// same way metrics_humansocket_test.go does.

import (
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// listenUDS binds a Unix socket at a short path under t.TempDir, skipping
// the test on platforms where UDS binding is unsupported.
func listenUDS(t *testing.T) (net.Listener, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "h.sock")
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Skipf("cannot bind a Unix domain socket on this platform (run in the Linux dev container): %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	return ln, path
}

// serveHealthOnUDS runs an HTTP server on ln whose /health returns status.
func serveHealthOnUDS(t *testing.T, ln net.Listener, status int) {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(status)
	})
	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })
}

// callReady drives the Ready handler once and decodes its JSON body.
func callReady(t *testing.T, d *Deps) (int, map[string]string) {
	t.Helper()
	w := httptest.NewRecorder()
	Ready(d)(w, httptest.NewRequest("GET", "/ready", nil))
	var body map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("unmarshal /ready body %q: %v", w.Body.String(), err)
	}
	return w.Code, body
}

// saveHumanSocketGauge snapshots and restores the process-global gauge
// the socket subtests mutate through Ready → SetHumanSocketUp.
func saveHumanSocketGauge(t *testing.T) {
	t.Helper()
	prev := humanSocketUp.Load()
	t.Cleanup(func() { humanSocketUp.Store(prev) })
}

// (The socket-less Ready paths — ok / missing dir / dir-is-a-file — are
// already covered by TestReady* in handler_test.go; only the human-socket
// self-dial paths live here.)

func TestReady_HumanSocketHealthy(t *testing.T) {
	saveHumanSocketGauge(t)
	ln, path := listenUDS(t)
	serveHealthOnUDS(t, ln, http.StatusOK)

	code, body := callReady(t, &Deps{ConfigDir: t.TempDir(), HumanSocketPath: path})
	if code != http.StatusOK || body["status"] != "ready" {
		t.Fatalf("Ready = (%d, %v), want 200/ready", code, body)
	}
	if !humanSocketUp.Load() {
		t.Error("human_socket_up gauge = false after a successful self-dial, want true")
	}
}

// A missing socket file (the human listener never came up, or its pod
// volume is gone) must fail readiness — this is exactly the silent-outage
// case the self-dial exists to expose.
func TestReady_HumanSocketAbsent(t *testing.T) {
	saveHumanSocketGauge(t)
	SetHumanSocketUp(true) // prove Ready flips it down

	// Probe UDS support first so an unsupported platform skips rather
	// than "passing" for the wrong reason.
	ln, _ := listenUDS(t)
	_ = ln.Close()

	path := filepath.Join(t.TempDir(), "never-created.sock")
	code, body := callReady(t, &Deps{ConfigDir: t.TempDir(), HumanSocketPath: path})
	if code != http.StatusServiceUnavailable {
		t.Fatalf("Ready over an absent socket = %d, want 503", code)
	}
	if body["reason"] != "human_socket_down" {
		t.Errorf("reason = %q, want human_socket_down (body %v)", body["reason"], body)
	}
	if body["human_socket"] != path {
		t.Errorf("human_socket = %q, want %q", body["human_socket"], path)
	}
	if humanSocketUp.Load() {
		t.Error("human_socket_up gauge still true after a failed self-dial")
	}
}

// A half-wedged listener — accepts connections but never answers HTTP —
// must ALSO fail readiness. This pins the design choice documented on
// dialHumanSocket: a real GET /health end-to-end, not a raw connect, so
// "accepts but never responds" cannot masquerade as healthy. The dial
// timeout (500ms) bounds this test's runtime.
func TestReady_HumanSocketAcceptsButNeverResponds(t *testing.T) {
	saveHumanSocketGauge(t)
	ln, path := listenUDS(t)
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			// Hold the connection open, never write a response.
			defer conn.Close()
		}
	}()

	start := time.Now()
	code, body := callReady(t, &Deps{ConfigDir: t.TempDir(), HumanSocketPath: path})
	if code != http.StatusServiceUnavailable || body["reason"] != "human_socket_down" {
		t.Fatalf("Ready over a wedged listener = (%d, %v), want 503/human_socket_down", code, body)
	}
	// The bounded dial must give up around humanSocketDialTimeout — not
	// hang for the probe to pile up on (health.go:11-16).
	if elapsed := time.Since(start); elapsed > 5*time.Second {
		t.Errorf("self-dial took %v, want it bounded near %v", elapsed, humanSocketDialTimeout)
	}
	if humanSocketUp.Load() {
		t.Error("human_socket_up gauge still true after a wedged-listener dial")
	}
}

// A listener that answers with a non-2xx /health is down for readiness
// purposes; the observed status code must be visible in the error.
func TestReady_HumanSocketNon2xx(t *testing.T) {
	saveHumanSocketGauge(t)
	ln, path := listenUDS(t)
	serveHealthOnUDS(t, ln, http.StatusInternalServerError)

	code, body := callReady(t, &Deps{ConfigDir: t.TempDir(), HumanSocketPath: path})
	if code != http.StatusServiceUnavailable || body["reason"] != "human_socket_down" {
		t.Fatalf("Ready over a 500 /health = (%d, %v), want 503/human_socket_down", code, body)
	}
	if got := body["error"]; !strings.Contains(got, "returned status 500") {
		t.Errorf("error = %q, want it to carry the observed status 500", got)
	}
}

func TestErrUnhealthyStatus_Error(t *testing.T) {
	t.Parallel()
	if got := errUnhealthyStatus(503).Error(); got != "human socket /health returned status 503" {
		t.Errorf("Error() = %q", got)
	}
}

func TestItoa(t *testing.T) {
	t.Parallel()
	cases := map[int]string{0: "0", 7: "7", 42: "42", 200: "200", 5030: "5030"}
	for n, want := range cases {
		if got := itoa(n); got != want {
			t.Errorf("itoa(%d) = %q, want %q", n, got, want)
		}
	}
}
