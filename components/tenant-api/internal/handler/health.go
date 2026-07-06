package handler

import (
	"context"
	"net"
	"net/http"
	"os"
	"time"
)

// humanSocketDialTimeout bounds the readiness self-dial over the human-plane
// Unix socket. Short on purpose: the dial is a same-pod loopback over a UDS, so
// a healthy socket answers in well under a millisecond; anything approaching
// this bound already means the human listener is wedged. Kept below the probe's
// own period so a hung dial can't stack up across successive readiness checks.
const humanSocketDialTimeout = 500 * time.Millisecond

// Health handles GET /health — always returns 200 OK.
func Health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// Ready handles GET /ready — returns 503 when the config dir is not
// stat-able (e.g. ConfigMap mount failed, PV detached). Returns 200
// only when the directory is readable, so K8s drains traffic away
// from a pod whose tenant data the app cannot serve.
//
// ADR-027 D2-B §2.5: when the human-plane Unix socket is enabled
// (d.HumanSocketPath != ""), Ready ALSO self-dials GET /health over that socket.
// The kubelet only probes the TCP plane, so without this a dead human listener
// would be a SILENT outage (browsers via oauth2-proxy → UDS get nothing while
// the pod stays Ready). Failing readiness here makes the pod NotReady → the
// Service stops routing to it → the failure is visible, not silent. The dial
// result also drives the tenant_api_human_socket_up gauge for alerting.
func Ready(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		info, err := os.Stat(d.ConfigDir)
		if err != nil {
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{
				"status":     "not_ready",
				"config_dir": d.ConfigDir,
				"error":      err.Error(),
			})
			return
		}
		if !info.IsDir() {
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{
				"status":     "not_ready",
				"config_dir": d.ConfigDir,
				"error":      "config_dir is not a directory",
			})
			return
		}

		// Human-plane socket liveness (only when enabled).
		if d.HumanSocketPath != "" {
			if err := dialHumanSocket(r.Context(), d.HumanSocketPath); err != nil {
				SetHumanSocketUp(false)
				writeJSON(w, http.StatusServiceUnavailable, map[string]string{
					"status":       "not_ready",
					"config_dir":   d.ConfigDir,
					"reason":       "human_socket_down",
					"human_socket": d.HumanSocketPath,
					"error":        err.Error(),
				})
				return
			}
			SetHumanSocketUp(true)
		}

		writeJSON(w, http.StatusOK, map[string]string{
			"status":     "ready",
			"config_dir": d.ConfigDir,
		})
	}
}

// dialHumanSocket performs a bounded GET /health over the pod-internal human
// Unix socket. A non-2xx status or any transport error is a failure. It uses a
// one-shot http.Transport with a unix DialContext so it exercises the real
// listener end-to-end (accept → route → 200), not merely a raw socket connect —
// a half-wedged server that accepts but never responds must still fail.
func dialHumanSocket(parent context.Context, path string) error {
	ctx, cancel := context.WithTimeout(parent, humanSocketDialTimeout)
	defer cancel()

	tr := &http.Transport{
		DialContext: func(dctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(dctx, "unix", path)
		},
		// This transport is single-use; don't leave idle conns lingering.
		DisableKeepAlives: true,
	}
	defer tr.CloseIdleConnections()

	// The host in the URL is ignored by the unix DialContext but must be a valid
	// placeholder for the HTTP client.
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://unix/health", nil)
	if err != nil {
		return err
	}
	resp, err := (&http.Client{Transport: tr}).Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return &net.OpError{Op: "dial-health", Net: "unix", Err: errUnhealthyStatus(resp.StatusCode)}
	}
	return nil
}

// errUnhealthyStatus is a tiny error type so a non-2xx self-dial reports the
// observed status code without allocating fmt machinery on the hot readiness
// path (probes run every few seconds for the pod's whole lifetime).
type errUnhealthyStatus int

func (e errUnhealthyStatus) Error() string {
	return "human socket /health returned status " + itoa(int(e))
}

// itoa is a minimal, allocation-light int→string for small non-negative codes.
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [4]byte
	i := len(buf)
	for n > 0 && i > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}
