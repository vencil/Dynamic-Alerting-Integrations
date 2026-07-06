package main

import (
	"fmt"
	"net"
	"os"
)

// humanSocketPerm is the mode set on the Unix domain socket after bind. 0660 =
// owner+group read/write, no world access. The pod runs both tenant-api and the
// oauth2-proxy sidecar under the same pod-level uid/gid (65534, enforced by the
// Helm/raw securityContext and pinned by a render test), so group access is
// exactly the sidecar and nothing else. World bits stay off so a mis-mounted
// hostPath can't widen reachability.
const humanSocketPerm = 0o660

// listenUnix creates the human-plane Unix domain socket listener.
//
// It deliberately does NOT degrade to "skip the socket" on any failure: a caller
// that asked for --human-socket and cannot get one is a human-plane outage, and
// the whole point of ADR-027 D2-B is that human traffic MUST leave the network
// 8080 plane. Returning an error here is turned into log.Fatalf by the caller
// (MED-7 fail-loud), matching the TCP listener's own fail-loud contract — never
// a silent single-listener fallback that would route humans back over 8080.
//
// unlink-before-bind: a Go UnixListener unlinks its socket file on a clean
// Close, but a SIGKILL / OOM-kill leaves a stale socket file behind that would
// make the next bind fail with EADDRINUSE. We os.Remove it first (tolerating
// "not exist") so a crashed pod's successor binds cleanly.
func listenUnix(path string) (net.Listener, error) {
	if path == "" {
		return nil, fmt.Errorf("human socket path is empty")
	}
	// Remove a stale socket left by an unclean exit. Only ENOENT is tolerated;
	// any other removal error (e.g. it's a directory, or a permission problem)
	// is surfaced rather than blindly retried into a confusing bind failure.
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("remove stale socket %q: %w", path, err)
	}
	ln, err := net.Listen("unix", path)
	if err != nil {
		return nil, fmt.Errorf("listen unix %q: %w", path, err)
	}
	if err := os.Chmod(path, humanSocketPerm); err != nil {
		// Close the listener we just opened so a chmod failure doesn't leak an
		// open-but-unusable socket before we fail loud.
		_ = ln.Close()
		return nil, fmt.Errorf("chmod socket %q to %#o: %w", path, humanSocketPerm, err)
	}
	return ln, nil
}
