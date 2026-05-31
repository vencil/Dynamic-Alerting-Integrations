// Package benchcanary provides version-independent CONTROL benchmarks for the
// bench-gate interleaved harness (bench_interleave.sh + bench-gate-pr.yaml).
//
// Why this exists
// ===============
// The bench gate compares a PR's perf against its merge-base. On shared
// GitHub-hosted runners the machine itself drifts mid-run (thermal throttle,
// noisy neighbour, CPU frequency scaling). When the base and pr batches run at
// different times, that drift biases one batch systematically and benchstat —
// which assumes independent samples — misreads the time-correlated bias as a
// "statistically significant regression". This was the recurring false-RED root
// cause (#502 / #608 / #611 / #695; #695 even had ZERO Go changes).
//
// The harness defends against this two ways: (1) it INTERLEAVES base/pr so drift
// hits both sides and cancels, and (2) it runs these CONTROL canaries — code
// that contains NO product logic, so it measures ONLY the runner environment.
// The SAME compiled canary binary runs on both the base and pr sides, so any
// base-vs-pr delta benchstat reports for a canary is PURE environment drift, not
// code. If the canary itself "regresses", the run's runner was unstable → the
// whole comparison is judged INCONCLUSIVE (re-run) rather than gated.
//
// Canaries
// ========
//
//	BenchmarkControlCanaryCPU   — fixed CPU work (hand-rolled FNV-1a over a
//	                              fixed buffer). Sensitive to CPU frequency /
//	                              thermal throttle. GATING: this is the canary
//	                              that drives the INCONCLUSIVE verdict.
//	BenchmarkControlCanarySleep — fixed wall-clock sleep. Measures scheduler /
//	                              timer-wakeup jitter. INFORMATIONAL ONLY: a 3-4%
//	                              drift here is only ~30-40us, well inside the
//	                              jitter a virtualised GH runner shows even when
//	                              healthy, so gating on it would flap
//	                              INCONCLUSIVE constantly. Emitted + rendered for
//	                              human eyes; NOT part of the gate decision.
//
// Stdlib-only, zero external deps, its own go.mod — so the harness can stash it
// outside the checkout and compile/run it identically regardless of which
// commit's tree is checked out.
package benchcanary

import (
	"testing"
	"time"
)

// canaryBuf is a fixed, package-level input so the CPU canary does byte-for-byte
// identical work every iteration and across every run. 4 KiB exercises the ALU
// without meaningfully touching memory bandwidth.
var canaryBuf = func() []byte {
	b := make([]byte, 4096)
	for i := range b {
		b[i] = byte(i * 31)
	}
	return b
}()

// fnv1a is a hand-rolled FNV-1a so the canary has ZERO dependency (not even
// hash/fnv) that could change behaviour across Go versions. Pure integer ALU
// work, no allocation.
func fnv1a(data []byte) uint64 {
	const (
		offset64 = 14695981039346656037
		prime64  = 1099511628211
	)
	h := uint64(offset64)
	for _, c := range data {
		h ^= uint64(c)
		h *= prime64
	}
	return h
}

// canarySink defeats the compiler's dead-code elimination of the hash loop.
var canarySink uint64

// BenchmarkControlCanaryCPU does a fixed amount of pure-CPU work per iteration.
// GATING canary — see package doc.
func BenchmarkControlCanaryCPU(b *testing.B) {
	// Fixed inner repeat so one b.N iteration is a meaningful, stable chunk of
	// CPU work regardless of how the framework sizes b.N.
	const inner = 64
	var h uint64
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		for j := 0; j < inner; j++ {
			h ^= fnv1a(canaryBuf)
		}
	}
	canarySink = h
}

// BenchmarkControlCanarySleep sleeps a fixed 1ms per iteration. INFORMATIONAL
// ONLY — measures scheduler/timer wakeup latency, never gated on. See package
// doc for why a tight threshold here would flap.
func BenchmarkControlCanarySleep(b *testing.B) {
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		time.Sleep(time.Millisecond)
	}
}
