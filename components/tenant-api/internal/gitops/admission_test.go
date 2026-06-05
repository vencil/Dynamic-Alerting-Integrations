package gitops

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestWriteQueueDepthFromEnv covers TA_WRITE_QUEUE_DEPTH parsing + the fallback
// for unset/garbage/negative (0 is a valid aggressive setting and is honoured).
func TestWriteQueueDepthFromEnv(t *testing.T) {
	cases := []struct {
		name string
		env  string
		want int32
	}{
		{"unset → default", "", defaultWriteQueueDepth},
		{"valid", "12", 12},
		{"zero is honoured", "0", 0},
		{"at cap is honoured", "10000", maxWriteQueueDepth},
		{"negative → default", "-3", defaultWriteQueueDepth},
		{"garbage → default", "abc", defaultWriteQueueDepth},
		{"over cap → default (no int32 overflow)", "100000", defaultWriteQueueDepth},
		{"huge value past int32 → default (no overflow)", "3000000000", defaultWriteQueueDepth},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("TA_WRITE_QUEUE_DEPTH", tc.env)
			if got := writeQueueDepthFromEnv(); got != tc.want {
				t.Errorf("writeQueueDepthFromEnv() = %d, want %d", got, tc.want)
			}
		})
	}
}

// TestAcquireWrite_ShedsWhenFull is TRK-320 acceptance criterion 1: once the
// admitted (running + queued) count reaches maxWriteAdmit, further acquires
// fast-fail with ErrWriteOverloaded instead of blocking.
func TestAcquireWrite_ShedsWhenFull(t *testing.T) {
	t.Parallel()
	w := NewWriter(t.TempDir(), t.TempDir())
	w.maxWriteAdmit = 2 // 1 in-flight + 1 queued

	ctx := context.Background()
	// First acquire holds the single execution token.
	if err := w.acquireWrite(ctx); err != nil {
		t.Fatalf("acquire #1: %v", err)
	}
	// Second is admitted into the queue (would block on the token) — but we can't
	// block the test goroutine, so park it in a goroutine and confirm it's queued
	// by observing the third acquire shed.
	queued := make(chan error, 1)
	go func() { queued <- w.acquireWrite(ctx) }() // occupies the 1 queue slot, blocks on token

	// Give the goroutine a moment to enter the queue (inFlight → 2).
	waitFor(t, func() bool { return w.writeInFlight.Load() == 2 })

	// Third acquire must shed immediately.
	if err := w.acquireWrite(ctx); !errors.Is(err, ErrWriteOverloaded) {
		t.Fatalf("acquire #3 = %v, want ErrWriteOverloaded", err)
	}

	// Release the token → the queued acquire proceeds.
	w.releaseWrite()
	if err := <-queued; err != nil {
		t.Fatalf("queued acquire after release: %v", err)
	}
	w.releaseWrite()
	if got := w.writeInFlight.Load(); got != 0 {
		t.Errorf("inFlight after drain = %d, want 0", got)
	}
}

// TestAcquireWrite_CtxCancelledWhileQueued is TRK-320 acceptance criterion 2: a
// client that disconnects/times out WHILE QUEUED is released immediately and
// never gets the token (so its write never runs — no orphan write).
func TestAcquireWrite_CtxCancelledWhileQueued(t *testing.T) {
	t.Parallel()
	w := NewWriter(t.TempDir(), t.TempDir())
	w.maxWriteAdmit = 5

	// Hold the execution token so the next acquire must queue.
	if err := w.acquireWrite(context.Background()); err != nil {
		t.Fatalf("prime acquire: %v", err)
	}
	defer w.releaseWrite()

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- w.acquireWrite(ctx) }()
	waitFor(t, func() bool { return w.writeInFlight.Load() == 2 }) // queued

	cancel() // client gives up while queued

	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("queued acquire after cancel = %v, want context.Canceled", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("queued acquire did not return after ctx cancel — goroutine leaked (mutex-style block)")
	}
	// The slot was released, so only the primed holder remains.
	waitFor(t, func() bool { return w.writeInFlight.Load() == 1 })
}

// TestWrite_CancelledCtxDoesNotWrite is the end-to-end orphan-write guard: a
// Write whose ctx is already done while another write holds the token must NOT
// execute (no file created), proving admission is enforced before the critical
// section.
func TestWrite_CancelledCtxDoesNotWrite(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	w := NewWriter(dir, dir)
	w.maxWriteAdmit = 5

	// Occupy the token so the cancelled Write must queue then bail.
	if err := w.acquireWrite(context.Background()); err != nil {
		t.Fatalf("prime acquire: %v", err)
	}
	defer w.releaseWrite()

	ctx, cancel := context.WithCancel(context.Background())
	cancel() // already done before the call

	err := w.Write(ctx, "db-a", "alice@example.com", "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n")
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("Write with cancelled ctx = %v, want context.Canceled", err)
	}
	if _, statErr := os.Stat(filepath.Join(dir, "db-a.yaml")); statErr == nil {
		t.Error("db-a.yaml was written despite a cancelled ctx — orphan write not prevented (TRK-320)")
	}
}

// TestWrite_AdmissionDisabledOnStructLiteral guards backward compat: a Writer
// built via struct literal (no NewWriter) has a nil writeExec, so acquireWrite is
// a no-op and writes proceed (older tests construct Writers this way).
func TestWrite_AdmissionDisabledOnStructLiteral(t *testing.T) {
	t.Parallel()
	w := &Writer{} // no admission configured
	if err := w.acquireWrite(context.Background()); err != nil {
		t.Errorf("acquireWrite on a nil-admission Writer = %v, want nil (no-op)", err)
	}
	w.releaseWrite() // must not panic
}

// waitFor polls cond until true or fails the test after a short deadline.
func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatal("condition not met within deadline")
		}
		time.Sleep(time.Millisecond)
	}
}
