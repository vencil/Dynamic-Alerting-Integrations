// bench_filter.go — stdout sanitizer for `go test -bench ... -json`.
//
// Why this exists
// ===============
// Planning §3 A-15 + benchmark-playbook v2.1.0 LL: Go benchmarks sometimes
// write large log volume via log.Printf() during Setup + per-iteration. Even
// when the benchmark itself adds silenceLogs(b), any shared helper or library
// log call can pollute stdout, making `ns/op` lines hard to grep through a
// `docker exec` pipeline (where 2>/dev/null is unreliable).
//
// The project-wide defense upgrade is to run benchmarks with `-json` and
// filter events. `-json` routes Go's internal benchmark framing through
// stdout as one JSON object per line, while log.Printf / fmt.Println still
// appear but as `{"Action":"output", "Output":"..."}` events. Raw log lines
// are therefore always wrapped; we keep only the ones that look like
// benchmark result headers, footers, and the per-bench `ns/op` rows.
//
// Usage
// =====
//
//	go test -bench=. -benchmem -run=^$ -json ./... \
//	    | go run scripts/tools/ops/bench_filter.go
//
// Wrapped by scripts/tools/ops/bench_wrapper.sh for Makefile consumption
// (`make bench`). The wrapper redirects stderr to a log file so callers
// get a clean stdout stream suitable for piping to CHANGELOG.md or
// a benchmark comparison tool.
//
// Output line categories retained
// ===============================
//  1. Benchmark result rows       — "BenchmarkX-8    1000   1234 ns/op ..."
//  2. Suite headers               — "goos: linux" / "goarch: amd64" /
//     "pkg: ..." / "cpu: ..."
//  3. Pass/Fail summary           — "PASS" / "FAIL" / "ok pkg ...s" /
//     "FAIL\tpkg\ts"
//
// Everything else (log.Printf output, progress dots, empty lines) is dropped.
//
// Design notes
// ------------
//   - Stdlib only. Lives in the `benchfilter` module (go.mod beside it) so
//     golangci-lint reads it; `go run` still works on a bare copy outside any
//     module, which is how the release bench harness stashes it.
//   - Bufio scanner buffer is bumped to 16 MiB to survive large -json events
//     (some tests emit long log messages in a single Output field). A line
//     past that ceiling stops the scan, and `Scan` reports it the same way it
//     reports EOF — so the read error is checked at the end of main, or the
//     stream would end truncated at rc=0. Not reachable from today's
//     benchmarks: the longest single line measured across the exporter and
//     canary suites is under a kilobyte, four orders of magnitude below the
//     ceiling. This guards the next producer, not a live defect.
//   - Malformed JSON lines are silently skipped — benchmark framework
//     occasionally interleaves non-JSON preamble on some Go versions.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"strings"
)

// Matches a Go benchmark result line, e.g.
//
//	"BenchmarkFoo-8       1000     1234 ns/op       4096 B/op       10 allocs/op"
//
// Intentionally tolerant: any amount of whitespace between fields, and the
// second numeric field can be int or fractional.
var benchResultRe = regexp.MustCompile(`^Benchmark[^\s]+\s+\d+\s+\d+(?:\.\d+)?\s+ns/op`)

// Line prefixes we always retain (suite metadata + pass/fail).
var retainPrefixes = []string{
	"goos:",
	"goarch:",
	"pkg:",
	"cpu:",
	"ok ",
	"FAIL\t",
	"--- FAIL",
	"--- PASS",
}

type event struct {
	Action string `json:"Action"`
	Output string `json:"Output"`
}

func keepLine(raw string) bool {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return false
	}
	if trimmed == "PASS" || trimmed == "FAIL" {
		return true
	}
	if benchResultRe.MatchString(trimmed) {
		return true
	}
	for _, p := range retainPrefixes {
		if strings.HasPrefix(trimmed, p) {
			return true
		}
	}
	return false
}

func main() {
	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, 1024*1024), 16*1024*1024)

	// bufio.Writer keeps the first write error and turns every later write
	// into a no-op, so the writes below are deliberately unchecked and the
	// error is read once, at Flush. A stdout that refuses writes (full disk,
	// measured with /dev/full) then exits 1 instead of ending a truncated
	// stream with rc=0. Reachable only when stdout is a file or device: under
	// bench_wrapper.sh it is a pipe, whose only write error is EPIPE, and the
	// Go runtime ends the process with SIGPIPE (rc 141, as before) before
	// Flush can report it.
	w := bufio.NewWriter(os.Stdout)

	for sc.Scan() {
		var ev event
		if err := json.Unmarshal(sc.Bytes(), &ev); err != nil {
			// Pre-amble / stray non-JSON lines (some Go versions).
			// Fall back to the text-level retain check so we don't drop
			// legitimate headers that escaped the -json wrapper.
			raw := sc.Text()
			if keepLine(raw) {
				_, _ = w.WriteString(raw)
				_ = w.WriteByte('\n')
			}
			continue
		}
		if ev.Action != "output" {
			continue
		}
		if keepLine(ev.Output) {
			// Output already contains its own newline as produced by the
			// test binary; preserve byte-for-byte.
			_, _ = w.WriteString(ev.Output)
			if !strings.HasSuffix(ev.Output, "\n") {
				_ = w.WriteByte('\n')
			}
		}
	}
	if err := w.Flush(); err != nil {
		fmt.Fprintln(os.Stderr, "bench_filter: writing stdout:", err)
		os.Exit(1)
	}

	// The comment above covers the WRITE side only; this is the read side of
	// the same contract. `Scan` returning false means EOF or a line past the
	// buffer ceiling, and nothing distinguishes them but `Err`. Unlike the
	// Flush branch, this one is reachable through bench_wrapper.sh: the error
	// comes from the input, not from a pipe, so there is no SIGPIPE to end the
	// process first, and the wrapper's `set -o pipefail` turns the non-zero
	// into a failed benchmark run instead of a short bench.out.txt that reads
	// like a clean one.
	if err := sc.Err(); err != nil {
		fmt.Fprintln(os.Stderr, "bench_filter: reading stdin:", err)
		os.Exit(1)
	}
}
