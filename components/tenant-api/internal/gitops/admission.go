package gitops

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"strconv"
)

// ErrWriteOverloaded is returned by a write call when the load-shedding
// admission queue is full (TRK-320 / ADR-023 §C). The write plane is a single
// writer (one in-flight write) with a bounded queue; once running+queued exceeds
// maxWriteAdmit the call fast-fails so a burst of PUTs sheds load instead of
// piling up unbounded goroutines blocked on the lock. The handler maps it to a
// 503 + Retry-After so the client backs off and retries.
var ErrWriteOverloaded = errors.New("write plane overloaded: admission queue full")

// defaultWriteQueueDepth is how many writes may QUEUE behind the single in-flight
// write before further writes are shed. 5 absorbs a modest burst (config writes
// are human-triggered and low-frequency) while keeping the worst-case queued
// goroutine count — and thus tail latency — bounded. Override via
// TA_WRITE_QUEUE_DEPTH; total admitted = 1 in-flight + this.
const defaultWriteQueueDepth = 5

// maxWriteQueueDepth caps TA_WRITE_QUEUE_DEPTH. A queue deeper than this is
// pathological — it defeats the whole point of bounding goroutines — and a value
// near/over int32's range would overflow the conversion below (CodeQL). Either
// way it's a fat-finger, so we reject it back to the default.
const maxWriteQueueDepth = 10_000

// writeQueueDepthFromEnv reads TA_WRITE_QUEUE_DEPTH as a non-negative int,
// falling back to defaultWriteQueueDepth when unset, unparseable, negative, or
// absurdly large (> maxWriteQueueDepth). A value of 0 is honoured (no queue —
// only the single in-flight write is admitted, everything else sheds
// immediately), which is a legitimate aggressive setting. The upper bound also
// makes the int32 conversion provably overflow-free.
func writeQueueDepthFromEnv() int32 {
	v := os.Getenv("TA_WRITE_QUEUE_DEPTH")
	if v == "" {
		return defaultWriteQueueDepth
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < 0 || n > maxWriteQueueDepth {
		slog.Warn("gitops: invalid TA_WRITE_QUEUE_DEPTH — using default",
			"value", v, "default", defaultWriteQueueDepth, "error", err)
		return defaultWriteQueueDepth
	}
	return int32(n)
}

// acquireWrite is the load-shedding gate every write passes before taking w.mu
// (TRK-320 / ADR-023 §C). It must be paired with a deferred releaseWrite() on the
// nil-error path. Boundary (deliberate): context binds ONLY the queue/acquire
// stage — once the token is held the caller enters the critical section and the
// write runs to completion (never half-killed: a severed commit leaves a dirty
// tree, and aborting an already-validated write just makes the client retry and
// duplicate the PR). In-flight duration is bounded by the per-git-command timeout.
//
//  1. Reserve an admission slot; if running+queued already fills maxWriteAdmit,
//     fast-fail with ErrWriteOverloaded (→ 503) WITHOUT queueing.
//  2. Queue for the single execution token, ctx-aware: a client that times out or
//     disconnects while queued is released here and never runs (no orphan write).
//  3. Microsecond gap: the token win and a ctx deadline can land in the same
//     instant, so re-check ctx after acquiring the token but before w.mu.Lock().
func (w *Writer) acquireWrite(ctx context.Context) error {
	if w.writeExec == nil {
		return nil // admission not configured (struct-literal Writer in older tests)
	}

	// Step 1: bounded admission — shed past the queue cap instead of piling up.
	if w.writeInFlight.Add(1) > w.maxWriteAdmit {
		w.writeInFlight.Add(-1)
		return ErrWriteOverloaded
	}

	// Step 2: ctx-aware wait for the execution token (this IS the queue).
	select {
	case <-w.writeExec:
		// Step 3: closing the "won the token AND ctx just expired" microsecond gap.
		if err := ctx.Err(); err != nil {
			w.writeExec <- struct{}{}
			w.writeInFlight.Add(-1)
			return err
		}
		return nil
	case <-ctx.Done():
		// Client gave up while queued — release the slot and don't run the write.
		w.writeInFlight.Add(-1)
		return ctx.Err()
	}
}

// releaseWrite returns the execution token and frees the admission slot. Called
// via defer after a successful acquireWrite, AFTER the write (and w.mu.Unlock)
// completes — so the token reflects "actually executing", not "about to".
func (w *Writer) releaseWrite() {
	if w.writeExec == nil {
		return
	}
	w.writeExec <- struct{}{}
	w.writeInFlight.Add(-1)
}
