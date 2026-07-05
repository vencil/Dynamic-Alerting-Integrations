package main

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"os"
	"runtime"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/rbac"
)

// TestDualListener_ConnContextTagsAndShutdown wires the SAME two-server setup
// main() uses (shared handler, per-server ConnContext stamping tcp/uds) and
// proves end-to-end that:
//   - a request over the TCP server sees ListenerTCP in its context (T1b);
//   - a request over the UDS server sees ListenerUDS (T1b);
//   - graceful Shutdown of both returns and the socket file is unlinked (T1g).
//
// This is the integration counterpart to the unit-level rbac.WithListener
// round-trip test: it verifies the ConnContext hooks are actually invoked by
// net/http on each accepted connection, not just that the helpers round-trip.
func TestDualListener_ConnContextTagsAndShutdown(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("unix domain sockets are not exercised on Windows; runs in the Linux dev container")
	}

	// Shared handler reports the listener its request context carries.
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		l, _ := rbac.ListenerFromContext(r.Context())
		fmt.Fprint(w, l.String())
	})

	// ── TCP server ────────────────────────────────────────────────────────────
	tcpLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("tcp listen: %v", err)
	}
	srvTCP := &http.Server{
		Handler: handler,
		ConnContext: func(ctx context.Context, _ net.Conn) context.Context {
			return rbac.WithListener(ctx, rbac.ListenerTCP)
		},
	}
	go func() { _ = srvTCP.Serve(tcpLn) }()

	// ── UDS server (via the real listenUnix helper) ───────────────────────────
	sock := t.TempDir() + "/human.sock"
	udsLn, err := listenUnix(sock)
	if err != nil {
		t.Fatalf("listenUnix: %v", err)
	}
	srvUDS := &http.Server{
		Handler: handler,
		ConnContext: func(ctx context.Context, _ net.Conn) context.Context {
			return rbac.WithListener(ctx, rbac.ListenerUDS)
		},
	}
	go func() { _ = srvUDS.Serve(udsLn) }()

	// ── T1b: TCP request → "tcp" ──────────────────────────────────────────────
	if got := getBody(t, http.DefaultClient, "http://"+tcpLn.Addr().String()+"/"); got != "tcp" {
		t.Errorf("TCP listener request saw listener %q, want tcp", got)
	}

	// ── T1b: UDS request → "uds" ──────────────────────────────────────────────
	udsClient := &http.Client{Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", sock)
		},
	}}
	if got := getBody(t, udsClient, "http://unix/"); got != "uds" {
		t.Errorf("UDS listener request saw listener %q, want uds", got)
	}

	// ── T1g: graceful shutdown of both; socket file unlinked ──────────────────
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srvUDS.Shutdown(ctx); err != nil {
		t.Errorf("UDS shutdown: %v", err)
	}
	if err := srvTCP.Shutdown(ctx); err != nil {
		t.Errorf("TCP shutdown: %v", err)
	}
	// Go's UnixListener unlinks the socket on Close (Shutdown closes it).
	if _, err := os.Stat(sock); !os.IsNotExist(err) {
		t.Errorf("socket file still present after shutdown (stat err=%v), want unlinked", err)
	}
}

func getBody(t *testing.T, c *http.Client, url string) string {
	t.Helper()
	resp, err := c.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	buf := make([]byte, 64)
	n, _ := resp.Body.Read(buf)
	return string(buf[:n])
}
