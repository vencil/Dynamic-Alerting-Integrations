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
#        BENCH_OUT_DIR/bench.err.log — compile errors / setup failures (fd 2)
#        BENCH_OUT_DIR/bench.raw.jsonl — original -json event stream
#
#      On a SUCCESSFUL run bench.err.log is normally EMPTY — that is expected,
#      not a sign the capture broke. Under `-json`, cmd/go feeds the test
#      binary's own stdout AND stderr through test2json, so log.Printf output
#      arrives in the JSON stream as {"Action":"output"} events, never on fd 2.
#      Measured (go1.23.12): a benchmark calling log.Println produced 0 bytes
#      in bench.err.log and 6 matching output events on stdout — and so did one
#      writing directly to os.Stderr.
#
#      Compile/setup failures USED TO be the exception and land here, but that
#      is toolchain-dependent and no longer holds on modern Go:
#        go1.23.12 — syntax error produced 47 bytes on fd 2 against 26 on stdout.
#        go1.24.7 / go1.25.1 — syntax error produced **0 bytes** in bench.err.log
#          (measured TRK-381; build failures arrive as JSON events on stdout).
#      The pinned CI toolchain is 1.26 and the dev container is 1.23; 1.26 was
#      NOT measured (not installed in the measuring container) — do not assume it.
#      ⇒ An EMPTY bench.err.log is expected on go1.24+ even for a compile error.
#      Read bench.raw.jsonl (or the filtered "FAIL … [setup failed]" line that
#      bench.out.txt still carries) for those, not bench.err.log.
#
#      This line previously read "raw stderr (log.Printf, compile errors)":
#      the log.Printf half was wrong, the compile-errors half was right.
#      bench_filter.go:12-16 has always described the log.Printf behaviour
#      correctly and is the SSOT for it.
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
# Exit codes — ⛔ there is no exit-code contract, and this comment must not
#   pretend otherwise. `set -euo pipefail`, and nothing is normalised: whichever
#   statement dies first ends the script carrying its own rc, so the reachable
#   set is not enumerable from here.
#   ⛔ Three attempts to enumerate it have been written in this header and each
#      was falsified in the very next review round by the case nobody thought of
#      (a `mkdir` that is not a pre-flight check → 1; SIGPIPE from `… | head`
#      → 141; `go test` rejecting a flag → 2, with none of this script's own
#      `exit`s running). DO NOT ADD A FOURTH LIST. Read stderr and the
#      artefacts. What follows is only what a caller can act on:
#
#   • 0 means the run completed, NOT that every benchmark passed — read the
#     "FAIL" summary line in bench.out.txt. (A benchmark that merely prints the
#     word FAIL lands there too, so the line is a hint, not a verdict.)
#   • Any rc with NO "[bench_wrapper] cmd: …" preamble means the script died
#     before the pipeline ⇒ environment error, not a test failure; read stderr.
#     Measured: an unwritable BENCH_OUT_DIR — a variable the Concurrency note
#     below tells you to set — gives rc 1 and `mkdir: cannot create directory`.
#   • ⛔ Do NOT read 137 as OOM. It means the `go test` DRIVER was SIGKILLed.
#     A real OOM kill takes the highest-badness process, i.e. the compiled test
#     binary, not the thin driver. Measured, same wrapper and module, only the
#     signal target differing: SIGKILL to the driver (os.Getppid()) → 137;
#     SIGKILL to the test binary (os.Getpid()) → 1, with go test printing an
#     ordinary FAIL line. So the realistic OOM case looks like any other 1.
#   • The arg/env refusals below `exit 2` before the pipeline runs (hence no
#     preamble). ⚠️ rc 2 does NOT imply one of them fired: `go test
#     -timeout=notaduration` and `go test -help` also leave rc 2, from inside
#     the pipeline, with the preamble already printed.
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

# ⛔ 不要在這條管線之後讀 `PIPESTATUS`/`$?`，也不要拿掉上面的 `set -o pipefail`。
#   `set -e` + `pipefail`：管線非零時腳本就在**管線那一行**終止，之後的任何
#   rc 檢查都不可達。此處原有一段 `GO_RC="${PIPESTATUS[0]}"` 守衛，它那則
#   `[bench_wrapper] go test exited …` 訊息從來沒有印出來過（TRK-381 / #1771）。
#   重現 bash -c 'set -euo pipefail; (exit 7)|tee /dev/null; echo NOPE' → 不印 NOPE、exit 7
#
#   ⚠️ 它只在 `pipefail` 存在時是死的 —— 拿掉 pipefail 後同一段守衛會醒，因此它
#   同時是一層冗餘後備，刪除移走了它。這行註解把 `pipefail` 一併釘住作為替代。
#   重現 bash -c 'set -eu; (exit 5)|tee /dev/null|cat|tee /dev/null >/dev/null;
#                 GO_RC="${PIPESTATUS[0]}"; [ "$GO_RC" -eq 0 ] || { echo "GUARD FIRED $GO_RC"; exit 1; }'
#   → 印 `GUARD FIRED 5`；把 `-o pipefail` 加回去則不印。
#
#   pipefail 讓 go test 的失敗浮現成非零的 wrapper exit，同時仍產出部分輸出。
#   ⚠️ 機制是「**最右邊**的非零」，不是最左邊（本行原文寫 leftmost，是錯的）：
#   實測 `set -o pipefail; (exit 5)|(exit 0)|(exit 7)|(exit 0)` → rc **7**。
#   實務上 go test 把所有失敗模式都正規化成 rc 1、`go run` 也把 filter 的任何非零
#   壓成 1，所以「最左／最右」今天觀察不到差別。⛔ 本行原文寫「唯一的例外是
#   go test 自己被 SIGKILL」——**那個「唯一」是假的**，已刪：`go test` 自己拒收旗標
#   （實測 `-timeout=notaduration`、`-help`）就讓管線 rc 是 **2**。要看有哪些已量到
#   的逃出值，讀檔頭的 Exit codes 段，並注意它明說自己不窮盡。
"$GO_BIN" test -json "$@" 2>"$ERR_LOG" \
    | tee "$RAW_JSONL" \
    | "$GO_BIN" run "$FILTER" \
    | tee "$OUT_TXT"

echo "---"
echo "[bench_wrapper] done. Clean result in $OUT_TXT"
