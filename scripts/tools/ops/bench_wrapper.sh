#!/usr/bin/env bash
# bench_wrapper.sh — clean stdout Go benchmark runner (Planning §3 A-15).
#
# Purpose
#   v2.1.0 LL (benchmark-playbook.md §"Go benchmark log 噪音致 output 爆量"):
#   go test -bench output gets drowned in ~732 KB of log.Printf noise when
#   run through `docker exec`. `2>/dev/null` is unreliable in some pipe
#   setups (stdout gets dropped too). The defense upgrade is to always run
#   with `-json` and filter events via bench_filter.go.
#
# Behaviour
#   1. Runs `go test -bench ... -json <args>` with stderr captured to a log.
#   2. Pipes stdout into bench_filter.go which retains only suite headers,
#      per-bench `ns/op` rows, and PASS/FAIL summaries.
#   3. Writes:
#        BENCH_OUT_DIR/bench.out.txt — clean benchmark results (stdout)
#        BENCH_OUT_DIR/bench.err.log — raw stderr (log.Printf, compile errors)
#        BENCH_OUT_DIR/bench.raw.jsonl — original -json event stream
#
# Usage
#   scripts/tools/ops/bench_wrapper.sh -bench=. -benchmem -run=^$ \
#       -count=1 -timeout=15m ./components/threshold-exporter/app/...
#
#   BENCH_OUT_DIR=_out scripts/tools/ops/bench_wrapper.sh -bench=BenchmarkFoo ...
#
# Environment
#   BENCH_OUT_DIR   — output directory (default: current working directory).
#                     Created if missing.
#   BENCH_GO        — go binary path (default: `go` in PATH).
#
# Exit codes
#   0   — benchmark run completed (regardless of PASS / FAIL of individual
#         benchmarks; check bench.out.txt for "FAIL" summary line).
#   1   — go test itself failed (compile error, panic, missing deps).
#         bench.err.log has details.
#   2   — argument / environment error (wrapper refused to run).
#
# Concurrency
#   Like run_hooks_sandbox.sh, the default output paths are shared. If you
#   want to run two benchmark suites in parallel, set a unique BENCH_OUT_DIR
#   per invocation, e.g. `BENCH_OUT_DIR=_out/$$ bench_wrapper.sh ...`.

set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <go test args>" >&2
    echo "Example: $0 -bench=. -benchmem -run=^$ -count=1 ./..." >&2
    exit 2
fi

GO_BIN="${BENCH_GO:-go}"
if ! command -v "$GO_BIN" >/dev/null 2>&1; then
    echo "[bench_wrapper] go binary not found: $GO_BIN (set BENCH_GO to override)" >&2
    exit 2
fi

OUT_DIR="${BENCH_OUT_DIR:-.}"
mkdir -p "$OUT_DIR"
OUT_TXT="$OUT_DIR/bench.out.txt"
ERR_LOG="$OUT_DIR/bench.err.log"
RAW_JSONL="$OUT_DIR/bench.raw.jsonl"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILTER="$SCRIPT_DIR/bench_filter.go"
if [ ! -f "$FILTER" ]; then
    echo "[bench_wrapper] filter not found: $FILTER" >&2
    exit 2
fi

echo "[bench_wrapper] cmd:  $GO_BIN test -json $*"
echo "[bench_wrapper] raw:  $RAW_JSONL"
echo "[bench_wrapper] err:  $ERR_LOG"
echo "[bench_wrapper] out:  $OUT_TXT"
echo "---"

# PIPESTATUS[0] = go test exit, PIPESTATUS[1] = tee, PIPESTATUS[2] = go run filter.
# `set -o pipefail` propagates the leftmost non-zero, so go test failure
# surfaces as a non-zero wrapper exit while still producing partial output.
"$GO_BIN" test -json "$@" 2>"$ERR_LOG" \
    | tee "$RAW_JSONL" \
    | "$GO_BIN" run "$FILTER" \
    | tee "$OUT_TXT"

GO_RC="${PIPESTATUS[0]}"
if [ "$GO_RC" -ne 0 ]; then
    echo "[bench_wrapper] go test exited $GO_RC — see $ERR_LOG" >&2
    exit 1
fi

echo "---"
echo "[bench_wrapper] done. Clean result in $OUT_TXT"
