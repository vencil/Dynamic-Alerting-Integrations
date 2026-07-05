package main

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// listenUnix must bind cleanly even when a stale socket file is already present
// at the path (a SIGKILL/OOM survivor), and the resulting socket must be mode
// 0660. Unix-only: Windows has no unix-socket file semantics and the CI/dev
// Go tests for UDS run in the Linux dev container.
func TestListenUnix_RebindsOverStaleSocketAndChmods(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("unix domain sockets are not exercised on Windows; runs in the Linux dev container")
	}
	dir := t.TempDir()
	sock := filepath.Join(dir, "human.sock")

	// First bind creates the socket.
	ln1, err := listenUnix(sock)
	if err != nil {
		t.Fatalf("first listenUnix: %v", err)
	}
	// Leave the socket FILE behind to simulate an unclean exit: capture the path,
	// then close the listener but re-create a stale file so the next bind sees a
	// residual socket. (A clean Close unlinks it, which is exactly why we must
	// re-plant one to exercise the unlink-before-bind path deterministically.)
	_ = ln1.Close()
	if _, err := os.Create(sock); err != nil {
		t.Fatalf("plant stale socket file: %v", err)
	}
	if _, err := os.Stat(sock); err != nil {
		t.Fatalf("stale socket file should exist before rebind: %v", err)
	}

	// Second bind must succeed DESPITE the residual file (unlink-before-bind),
	// not fail with EADDRINUSE.
	ln2, err := listenUnix(sock)
	if err != nil {
		t.Fatalf("rebind over stale socket failed (unlink-before-bind regression): %v", err)
	}
	defer ln2.Close()

	info, err := os.Stat(sock)
	if err != nil {
		t.Fatalf("stat rebound socket: %v", err)
	}
	if perm := info.Mode().Perm(); perm != humanSocketPerm {
		t.Errorf("socket mode = %#o, want %#o (chmod after bind)", perm, humanSocketPerm)
	}
}

// An empty path is a caller error, not a silent no-op — the human plane was
// requested, so an unusable path must fail loud (the caller turns this into
// log.Fatalf).
func TestListenUnix_EmptyPathErrors(t *testing.T) {
	if _, err := listenUnix(""); err == nil {
		t.Fatal("listenUnix(\"\") returned nil error, want a failure (fail-loud)")
	}
}
