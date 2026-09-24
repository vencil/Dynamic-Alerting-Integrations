package main

import (
	"bufio"
	"errors"
	"go/build"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestBareCopyNeedsOnlyThisFile pins what bench-gate-release.yaml's
// "Stash bench harness" step relies on: it copies bench_filter.go alone and
// later `go run`s that copy from the exporter module's directory (#1871).
// So go/build's GoFiles must be just that file, and every import must be in
// the standard library — anything else would resolve, or not, through the
// exporter's go.mod at whichever tag is checked out.
//
// Standard library means "under GOROOT/src", not "first path element has no
// dot": this module's own path is `benchfilter`, so a subpackage import like
// benchfilter/x has no dot and still breaks the bare copy. Not checked, and
// also left behind by the copy: cgo files (not in GoFiles; red here only via
// their import "C" when cgo is on), .s files, //go:embed patterns.
func TestBareCopyNeedsOnlyThisFile(t *testing.T) {
	pkg, err := build.ImportDir(".", 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(pkg.GoFiles) != 1 || pkg.GoFiles[0] != "bench_filter.go" {
		t.Errorf("non-test files %v: the release harness copies bench_filter.go alone, "+
			"so anything it needs from a sibling file is missing there", pkg.GoFiles)
	}
	goroot := build.Default.GOROOT
	for _, imp := range append([]string{"fmt"}, pkg.Imports...) { // fmt: GOROOT itself resolves
		if st, err := os.Stat(filepath.Join(goroot, "src", filepath.FromSlash(imp))); err != nil || !st.IsDir() {
			t.Errorf("import %q is not under GOROOT/src (%q): not the standard library", imp, goroot)
		}
	}
}

// cpuHeader is the suite header scripts/tools/dx/analyze_bench_history.py
// reads back out of the bench-baseline.txt artifact (its `_CPU_RE`) to
// stratify the trend watchdog by runner CPU model. This filter is what keeps
// it there on the paths that go through it — the release harness, and the
// nightly fallback night when the paired reference is unusable; the paired
// path appends the test binary's stdout without filtering (#1873).
const cpuHeader = "cpu: AMD EPYC 7763 64-Core Processor\n"

// TestFilterKeepsExactlyTheRetainedLines pins the whole output of one stream
// rather than a list of lines that must survive: for the shapes this fixture
// carries, keeping one that used to be dropped reds as loudly as dropping one
// that used to be kept. Shapes absent from the fixture are not covered. The
// diag stream is pinned whole the same way: it carries the build-output
// events and nothing else, and none of them reach out (#1872).
func TestFilterKeepsExactlyTheRetainedLines(t *testing.T) {
	in := strings.Join([]string{
		// Non-JSON lines take the text-level fallback.
		`goos: linux`,
		`# preamble that escaped the -json wrapper`,
		`{"Action":"start","Package":"example.com/x"}`,
		`{"Action":"output","Package":"example.com/x","Output":"goarch: amd64\n"}`,
		`{"Action":"output","Package":"example.com/x","Output":"pkg: example.com/x\n"}`,
		`{"Action":"output","Package":"example.com/x","Output":` + jsonString(cpuHeader) + `}`,
		`{"Action":"output","Package":"example.com/x","Output":"2026/09/17 03:00:00 loaded 1000 tenants\n"}`,
		`{"Action":"output","Package":"example.com/x","Test":"BenchmarkScan_1000","Output":"BenchmarkScan_1000\n"}`,
		`{"Action":"output","Package":"example.com/x","Test":"BenchmarkScan_1000","Output":"BenchmarkScan_1000-4   \t      93\t  35422664 ns/op\t    1024 B/op\t      12 allocs/op\n"}`,
		// Fractional ns/op: what a sub-microsecond benchmark emits, and what
		// analyze_bench_history.py's own regex accepts.
		`{"Action":"output","Package":"example.com/x","Test":"BenchmarkFast","Output":"BenchmarkFast-16   \t     100\t         6.570 ns/op\n"}`,
		// go1.24+: compile diagnostics as events, and bench.err.log is empty.
		`{"Action":"build-output","ImportPath":"example.com/y","Output":"# example.com/y\n"}`,
		`{"Action":"build-output","ImportPath":"example.com/y","Output":"./y.go:3:9: syntax error\n"}`,
		`{"Action":"output","Package":"example.com/x","Output":"PASS\n"}`,
		`{"Action":"output","Package":"example.com/x","Output":"ok  \texample.com/x\t12.345s\n"}`,
		`{"Action":"pass","Package":"example.com/x","Elapsed":12.345}`,
		// An Output without its trailing newline gets one.
		`{"Action":"output","Package":"example.com/y","Output":"--- FAIL: TestY (0.00s)"}`,
		`{"Action":"output","Package":"example.com/y","Output":"--- PASS: TestZ (0.00s)\n"}`,
		// The bare FAIL line bench_wrapper.sh tells the operator to look for,
		// and the package line after it.
		`{"Action":"output","Package":"example.com/y","Output":"FAIL\n"}`,
		`{"Action":"output","Package":"example.com/y","Output":"FAIL\texample.com/y\t0.01s\n"}`,
	}, "\n") + "\n"

	want := "goos: linux\n" +
		"goarch: amd64\n" +
		"pkg: example.com/x\n" +
		cpuHeader +
		"BenchmarkScan_1000-4   \t      93\t  35422664 ns/op\t    1024 B/op\t      12 allocs/op\n" +
		"BenchmarkFast-16   \t     100\t         6.570 ns/op\n" +
		"PASS\n" +
		"ok  \texample.com/x\t12.345s\n" +
		"--- FAIL: TestY (0.00s)\n" +
		"--- PASS: TestZ (0.00s)\n" +
		"FAIL\n" +
		"FAIL\texample.com/y\t0.01s\n"

	var out, diag strings.Builder
	readErr, writeErr := filter(strings.NewReader(in), &out, &diag)
	if readErr != nil || writeErr != nil {
		t.Fatalf("filter errors: read=%v write=%v", readErr, writeErr)
	}
	if got, want := diag.String(), "# example.com/y\n./y.go:3:9: syntax error\n"; got != want {
		t.Errorf("diag mismatch\n got: %q\nwant: %q", got, want)
	}
	if !strings.Contains(out.String(), cpuHeader) {
		t.Errorf("the cpu: header was dropped; analyze_bench_history.py --trend-watch " +
			"stratifies by it and would lose the key without any error")
	}
	if got := out.String(); got != want {
		t.Errorf("output mismatch\n got: %q\nwant: %q", got, want)
	}
}

// TestFilterReturnsReadAndWriteErrors covers the two errors main turns into
// exit 1: a line at the scanner ceiling, and an output that refuses writes.
//
// Every case here feeds the cpu: header rather than some other retained line,
// so a change to the retain rules reds the test above — which names that
// contract — instead of reding these with a message about error handling.
func TestFilterReturnsReadAndWriteErrors(t *testing.T) {
	oversized := func(n int) string {
		return `{"Action":"output","Output":"cpu: before\n"}` + "\n" +
			strings.Repeat("x", n) + "\n" +
			`{"Action":"output","Output":"cpu: after\n"}` + "\n"
	}
	// Both sides of the ceiling. Without the second case the buffer could be
	// shrunk to any size, or dropped for bufio's 64 KiB default, and only the
	// first case — which any ceiling satisfies — would still be here.
	t.Run("line at the 16 MiB ceiling", func(t *testing.T) {
		var out strings.Builder
		readErr, writeErr := filter(strings.NewReader(oversized(16*1024*1024)), &out, io.Discard)
		if !errors.Is(readErr, bufio.ErrTooLong) || writeErr != nil {
			t.Fatalf("want read=%v write=<nil>, got read=%v write=%v", bufio.ErrTooLong, readErr, writeErr)
		}
		if got := out.String(); got != "cpu: before\n" {
			t.Errorf("output before the oversized line: got %q", got)
		}
	})
	t.Run("line just under the ceiling", func(t *testing.T) {
		var out strings.Builder
		readErr, writeErr := filter(strings.NewReader(oversized(16*1024*1024-1)), &out, io.Discard)
		if readErr != nil || writeErr != nil {
			t.Fatalf("want no errors, got read=%v write=%v", readErr, writeErr)
		}
		if got := out.String(); got != "cpu: before\ncpu: after\n" {
			t.Errorf("a line under the ceiling must not end the scan: got %q", got)
		}
	})
	t.Run("output refuses writes", func(t *testing.T) {
		in := `{"Action":"output","Output":` + jsonString(cpuHeader) + "}\n"
		readErr, writeErr := filter(strings.NewReader(in), refusingWriter{}, io.Discard)
		if readErr != nil || writeErr == nil {
			t.Fatalf("want read=<nil> write=non-nil, got read=%v write=%v", readErr, writeErr)
		}
	})
}

type refusingWriter struct{}

func (refusingWriter) Write([]byte) (int, error) { return 0, errors.New("refused") }

// jsonString quotes s the way the fixture's JSON needs (only \n is escaped
// by the strings above).
func jsonString(s string) string {
	return `"` + strings.ReplaceAll(s, "\n", `\n`) + `"`
}
