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
// Everything else (log.Printf output, progress dots, empty lines) is dropped,
// except build-output events, which go to stderr (see filter).
//
// Design notes
// ------------
//   - Stdlib only. Lives in the `benchfilter` module (go.mod beside it) so
//     golangci-lint reads it. The release bench harness copies this file
//     alone and `go run`s it from the exporter module's directory, so it can
//     rely on no sibling file and on no go.mod but the exporter's — at
//     whichever tag is checked out (TestBareCopyNeedsOnlyThisFile).
//   - Bufio scanner buffer is bumped to 16 MiB to survive large -json events
//     (some tests emit long log messages in a single Output field). A line
//     that REACHES that ceiling stops the scan (bufio errors at >=, not >),
//     and `Scan` reports it the same way it reports EOF — so the read error
//     is checked at the end of main. Everything from that line onward is
//     dropped either way; what the check adds is a non-zero rc and a named
//     reason. Not reachable from today's benchmarks: the longest single line
//     measured across the exporter and canary suites is under a kilobyte,
//     four orders of magnitude below the ceiling (exact figures in the #1864
//     commit; the raw streams they came from are gitignored, so treat them as
//     a one-off reading, not a maintained number). This guards the next
//     producer, not a live defect.
//   - Malformed JSON lines are silently skipped — benchmark framework
//     occasionally interleaves non-JSON preamble on some Go versions.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
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

// filter copies the retained lines of a `go test -json` stream from in to
// out, and the text of every build-output event to diag. It is main minus the
// exit codes, so bench_filter_test.go exercises the same loop the release
// harness runs; main decides what the two errors mean.
//
// Why diag and not out (#1872): from go1.24 on, compile diagnostics arrive
// in the -json stream as build-output events and bench.err.log stays empty,
// so dropping them left the job log with only "FAIL pkg [build failed]".
// main passes os.Stderr, which bench_wrapper.sh leaves on the caller's
// stderr. out stays byte-for-byte what it was: benchstat reads a
// "key: value" line as configuration, so a diagnostic of that shape in out
// would split the comparison. diag write errors are not checked (the same
// text is in bench.raw.jsonl), but a stderr that is a pipe whose reader has
// gone ends the process with SIGPIPE (rc 141) as stdout would — and before
// Flush, so bench.out.txt comes out empty. No caller reads the wrapper's
// stderr through a pipe today; before #1872 this filter never wrote there.
func filter(in io.Reader, out, diag io.Writer) (readErr, writeErr error) {
	sc := bufio.NewScanner(in)
	sc.Buffer(make([]byte, 1024*1024), 16*1024*1024)

	// bufio.Writer keeps the first write error and turns every later write
	// into a no-op, so the writes below are deliberately unchecked and the
	// error is read once, at Flush. A stdout that refuses writes (full disk,
	// measured with /dev/full) then exits 1 instead of ending a truncated
	// stream with rc=0. Reachable only when stdout is a file or device: under
	// bench_wrapper.sh it is a pipe, whose only write error is EPIPE, and the
	// Go runtime ends the process with SIGPIPE (rc 141, as before) before
	// Flush can report it.
	w := bufio.NewWriter(out)

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
		if ev.Action == "build-output" {
			_, _ = io.WriteString(diag, ev.Output)
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
	return sc.Err(), w.Flush()
}

func main() {
	readErr, writeErr := filter(os.Stdin, os.Stdout, os.Stderr)
	if writeErr != nil {
		fmt.Fprintln(os.Stderr, "bench_filter: writing stdout:", writeErr)
		os.Exit(1)
	}

	// The comment above covers the WRITE side only; this is the read side of
	// the same contract. `Scan` returning false means EOF or a line at the
	// buffer ceiling, and nothing distinguishes them but `Err`.
	//
	// ⛔ What this does NOT do: the partial output is already flushed above, so
	// bench.out.txt is byte-for-byte what it was before this check existed —
	// measured, every case. The truncation still happens; only the exit code
	// and this stderr line are new.
	//
	// What it replaces is a rc that could not be acted on. Under
	// bench_wrapper.sh the old rc depended on whether the bytes left unread
	// when the scanner gave up fit in a pipe buffer: they did (measured at the
	// ceiling and +64 B) and the pipeline exited 0 with a silently short file;
	// they did not (+64 KiB, +4 MiB) and the upstream `tee` took SIGPIPE, so
	// the pipeline exited 141 — the value this repo's own wrapper warns is
	// read as "a consumer of this script's stdout closed early", i.e. a
	// failure attributed to the wrong thing. Now it is 1 either way, with the
	// reason named.
	//
	// ⚠️ A write failure in the branch above exits first and hides this one.
	// Deliberate — the write error is the more proximate fact — but it means
	// "no space left on device" can be the only thing an operator sees when
	// there is also an oversized line waiting.
	if readErr != nil {
		fmt.Fprintln(os.Stderr, "bench_filter: reading stdin:", readErr)
		os.Exit(1)
	}
}
