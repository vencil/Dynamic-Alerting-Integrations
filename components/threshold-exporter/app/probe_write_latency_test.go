package main

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"testing"
	"time"
)

// BenchmarkProbeWriteLatency splits BenchmarkIncrementalLoad_1000_OneFileChanged's
// loop into its two halves and reports the LATENCY DISTRIBUTION of each, instead
// of reporting one mean.
//
// Why this exists. #1497 lists two candidate mechanisms for that benchmark's
// bimodal ratio. Mechanism 2 (an mtime-tick collision flipping the scanner to a
// different branch) is structurally impossible: the target file is rewritten
// every iteration, so `age > 2*time.Second` always fails before `cur == prev`
// is ever evaluated, and an instrumented branch count measured exactly one
// slow-path file for 285/285 steady-state iterations. Mechanism 1 - a writeback
// flush occasionally stalling the write - was never measured at all. This probe
// is that measurement, and the question it answers is narrow: WHEN A ROUND
// COMES OUT SLOW, IS THE EXCESS IN THE WRITE OR IN THE LOAD?
//
// ⛔ It reports; it decides nothing. There is no threshold in here and no
// pass/fail. A round where nothing is slow is a legitimate outcome and means
// this run did not catch an episode - NOT that no episode exists. Read the
// spread across rounds before reading any single round.
//
// ⚠️ It is NOT a drop-in copy of the benchmark it probes. Two deliberate
// differences, both of which cost time and would be wrong to hide:
//   - `time.Now()` twice per iteration (~50 ns each on the runners we use, vs
//     a ~7.5 ms iteration - under 0.002%, but not zero).
//   - `os.WriteFile`'s error is checked here and ignored in the benchmark. A
//     nil compare, kept because a probe that silently writes nothing would
//     report a beautifully fast write path.
//
// It is named so that it does NOT match the nightly's or the PR gate's
// `BENCH_RE` (`_1000(_|$)|MixedMode|Simulate_DeepChain`) - verified with
// `go test -list`, not by eye. Benchmarks do not run under a plain `go test`,
// so it also costs nothing on any existing job.
//
// Run it:
//
//	go test -run '^$' -bench '^BenchmarkProbeWriteLatency$' -benchtime=1x
//
// `-benchtime=1x` is required, not stylistic: b.N is the round counter here and
// the inner loop is what supplies the iterations.
// probeRound is monotonic across invocations on purpose. Go runs the body once
// with b.N==1 to calibrate even under `-benchtime=Nx`, so a per-invocation
// counter emits two different rounds both labelled `round=0`.
var probeRound int

func BenchmarkProbeWriteLatency(b *testing.B) {
	iters := probeEnvInt("PROBE_ITERS", 400)
	fmt.Printf("PROBEENV goos=%s goarch=%s numcpu=%d go=%s iters=%d b.N=%d\n",
		runtime.GOOS, runtime.GOARCH, runtime.NumCPU(), runtime.Version(), iters, b.N)
	write := make([]int64, iters)
	load := make([]int64, iters)

	for round := 0; round < b.N; round++ {
		// Rebuilt every round, not hoisted. A round has to be one whole
		// benchmark invocation or the rounds are not comparable: the first
		// ~135 iterations after a fresh tree miss the mtime guard and re-hash
		// all 1000 files, so a hoisted setup would make round 0 the only one
		// that pays warm-up (measured locally: load_p50 14.8 ms in round 0 vs
		// 7.1 ms in round 1 at PROBE_ITERS=150). The real benchmark pays that
		// warm-up on every invocation, so this probe must too.
		b.StopTimer()
		dir := buildDirConfig(b, 1000)
		silenceLogs(b)
		mgr := NewConfigManager(dir)
		if err := mgr.fullDirLoad(); err != nil {
			b.Fatal(err)
		}
		targetFile := filepath.Join(dir, "tenant-0500.yaml")
		b.StartTimer()

		for i := 0; i < iters; i++ {
			content := fmt.Sprintf("tenants:\n  tenant-0500:\n    mysql_connections: \"%d\"\n    mysql_threads_running: \"%d\"\n    container_cpu: \"%d\"\n    container_memory: \"%d\"\n",
				50+i%100, 60+i%40, 70+i%30, 80+i%15)
			t0 := time.Now()
			if err := os.WriteFile(targetFile, []byte(content), 0600); err != nil {
				b.Fatal(err)
			}
			t1 := time.Now()
			if err := mgr.IncrementalLoad(); err != nil {
				b.Fatal(err)
			}
			t2 := time.Now()
			write[i] = t1.Sub(t0).Nanoseconds()
			load[i] = t2.Sub(t1).Nanoseconds()
		}
		probeReport(probeRound, b.N, write, load)
		probeRound++
	}
}

// probeReport prints one PROBEROW per round plus the ten slowest iterations.
// Quantiles rather than every iteration: 400 lines per round is not more
// information, it is the same information in a form nobody greps.
//
// `bench_n` is b.N for the invocation this round belongs to, and it is on the
// row because consumers need it: under `-benchtime=Nx` Go still runs the body
// once with b.N==1 to calibrate, so a 3x run emits FOUR rounds - one of them
// the very first pass after process start, with cold caches and first-touch
// page faults. Pooling that round into a spread is how a tool whose entire
// output is a spread reports its own start-up as measurement noise. (The same
// cold-start effect is documented at ~+24.8% in
// `docs/internal/audit-reports/bench-aa-2026-08/README.md` §三.)
func probeReport(round, benchN int, write, load []int64) {
	w, l := probeSorted(write), probeSorted(load)
	fmt.Printf("PROBEROW round=%d bench_n=%d iters=%d"+
		" write_p50=%d write_p90=%d write_p99=%d write_max=%d write_sum=%d"+
		" load_p50=%d load_p90=%d load_p99=%d load_max=%d load_sum=%d\n",
		round, benchN, len(write),
		probeQ(w, 0.50), probeQ(w, 0.90), probeQ(w, 0.99), w[len(w)-1], probeSum(write),
		probeQ(l, 0.50), probeQ(l, 0.90), probeQ(l, 0.99), l[len(l)-1], probeSum(load))

	// The tail is the whole point: if a round is slow because of a stall, the
	// stall is in here and its `w=`/`l=` split says which half it landed in.
	idx := make([]int, len(write))
	for i := range idx {
		idx[i] = i
	}
	sort.Slice(idx, func(a, c int) bool {
		return write[idx[a]]+load[idx[a]] > write[idx[c]]+load[idx[c]]
	})
	for rank, i := range idx {
		if rank == 10 {
			break
		}
		fmt.Printf("PROBETAIL round=%d rank=%d iter=%d w=%d l=%d\n",
			round, rank, i, write[i], load[i])
	}
}

func probeSorted(v []int64) []int64 {
	out := append([]int64(nil), v...)
	sort.Slice(out, func(a, b int) bool { return out[a] < out[b] })
	return out
}

// probeQ indexes the sorted slice directly. No interpolation: these are
// latencies in nanoseconds over a few hundred samples, and an interpolated
// p99 would invent a value no iteration actually took.
func probeQ(sorted []int64, q float64) int64 {
	i := int(q * float64(len(sorted)-1))
	return sorted[i]
}

func probeSum(v []int64) int64 {
	var s int64
	for _, x := range v {
		s += x
	}
	return s
}

func probeEnvInt(key string, def int) int {
	s := os.Getenv(key)
	if s == "" {
		return def
	}
	var n int
	if _, err := fmt.Sscanf(s, "%d", &n); err != nil || n <= 0 {
		return def
	}
	return n
}
