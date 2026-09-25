#!/bin/sh
# go_testlog_exec.sh — `go test -exec` wrapper that records which files each
# test binary opens, for scripts/ops/go_test_reads.py (#1399).
#
# The `testing` package already keeps that log: `-test.testlogfile` makes it
# write one `open <path>` / `stat <path>` / `chdir <dir>` line per os-level
# call (it is how `go test` decides whether a cached result is still valid).
# cmd/go passes the flag only while it is caching, and `-exec` turns caching
# off, so the wrapper passes it itself. `go test` runs this in the package
# directory, so `pwd` is the base every relative path in the log resolves
# against; it is written beside the log because the log does not say.
#
# ⚠️ What the log cannot see: reads before `m.Run` (package init, the part of a
# TestMain that runs first), reads by child processes, and paths a test never
# executes on this runner. go_test_reads.py's docstring says what that costs.
set -eu
bin="$1"
shift
out="${GO_TESTLOG_DIR:?GO_TESTLOG_DIR must name the directory for the logs}/$(basename "$bin").$$.log"
pwd > "$out.cwd"
exec "$bin" -test.testlogfile="$out" "$@"
