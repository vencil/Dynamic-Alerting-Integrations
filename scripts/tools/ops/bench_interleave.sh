#!/usr/bin/env bash
# bench_interleave.sh — interleaved base/pr benchmark runner for the bench gate.
#
# Why this exists
# ===============
# The PR bench gate compares a PR against its merge-base. The previous "v5"
# topology ran the FULL base batch first, then the FULL pr batch. On shared
# GitHub-hosted runners the machine drifts mid-run (thermal throttle, noisy
# neighbour, CPU frequency scaling), so the two batches ran under different
# conditions and the pr batch was systematically biased. benchstat assumes
# independent samples and misread that time-correlated bias as a "statistically
# significant regression" — the recurring false-RED root cause (#502/#608/#611/
# #695; #695 had ZERO Go changes yet still went red).
#
# This harness eliminates that bias structurally:
#
#   1. INTERLEAVE. Run base and pr alternately, one round at a time
#      (base, pr, base, pr, ...). Any time-correlated drift now hits BOTH sides
#      within the same round and cancels, restoring the independent-samples
#      assumption benchstat needs.
#
#   2. PRE-COMPILE (decouple compile from run). `go test -bench` recompiles the
#      test binary on every invocation; the Go compiler is CPU-heavy and would
#      inject a thermal spike right before each measurement. We compile each
#      side's test binaries ONCE up front with `go test -c`, then the loop only
#      EXECUTES binaries — no compiler load between measurements.
#
#   3. NO mid-loop drop_caches. The old sequential harness dropped OS caches
#      between the base and pr batches. In an interleaved loop that would be
#      asymmetric (base always warm from the prior round's pr, pr always cold)
#      and would REINTRODUCE a systematic pr-slower bias. Interleaving already
#      gives both sides symmetric warm caches, so there is no drop_caches here.
#
# Control canaries (benchcanary module) run each round too, so the caller can
# tell "this run's runner drifted" apart from "this PR is slower" — see
# bench-canary/canary_test.go and the Compare step in bench-gate-pr.yaml.
#
# Inputs (environment)
#   BASE_DIR           app dir of the base (merge-base) worktree
#                      e.g. <wt-base>/components/threshold-exporter/app
#   PR_DIR             app dir of the pr (PR head) worktree
#   CANARY_DIR         path to the stashed bench-canary module
#   BENCH_OUT_DIR      output dir (created); writes base.txt / pr.txt / err.log
#   ROUNDS             interleave rounds = samples per bench per side (default 6)
#   BENCH_RE           main-bench selection regex
#                      (default '_1000(_|$)|MixedMode|Simulate_DeepChain')
#   BENCHTIME          per-bench time for the main benches (default 3s)
#   CANARY_BENCHTIME   per-bench time for the canaries (default 1s)
#   BENCH_GO           go binary (default: `go` in PATH)
#
# Output
#   $BENCH_OUT_DIR/bench-base.txt   base samples (ROUNDS per bench), benchstat-ready
#   $BENCH_OUT_DIR/bench-pr.txt     pr samples
#   $BENCH_OUT_DIR/bench.err.log    stderr from every binary run (log noise, etc.)
#
# The caller runs benchstat on the two files. This script intentionally does NOT
# run benchstat itself — the workflow applies its own `-filter` / shape asserts,
# and `make bench-interleave` chains benchstat separately.
#
# Exit codes
#   0   all rounds completed
#   2   argument / environment error
#   3   a benchmark binary failed to compile or run (see bench.err.log)

set -euo pipefail

GO_BIN="${BENCH_GO:-go}"
ROUNDS="${ROUNDS:-6}"
BENCH_RE="${BENCH_RE:-_1000(_|\$)|MixedMode|Simulate_DeepChain}"
BENCHTIME="${BENCHTIME:-3s}"
CANARY_BENCHTIME="${CANARY_BENCHTIME:-1s}"

die() { echo "[bench_interleave] $*" >&2; exit 2; }

[ -n "${BASE_DIR:-}" ]   || die "BASE_DIR not set"
[ -n "${PR_DIR:-}" ]     || die "PR_DIR not set"
[ -n "${CANARY_DIR:-}" ] || die "CANARY_DIR not set"
[ -d "$BASE_DIR" ]       || die "BASE_DIR not a dir: $BASE_DIR"
[ -d "$PR_DIR" ]         || die "PR_DIR not a dir: $PR_DIR"
[ -d "$CANARY_DIR" ]     || die "CANARY_DIR not a dir: $CANARY_DIR"
command -v "$GO_BIN" >/dev/null 2>&1 || die "go binary not found: $GO_BIN"

OUT_DIR="${BENCH_OUT_DIR:-.}"
mkdir -p "$OUT_DIR"
BASE_TXT="$OUT_DIR/bench-base.txt"
PR_TXT="$OUT_DIR/bench-pr.txt"
ERR_LOG="$OUT_DIR/bench.err.log"
: > "$BASE_TXT"; : > "$PR_TXT"; : > "$ERR_LOG"

BIN_DIR="$(mktemp -d)"
trap 'rm -rf "$BIN_DIR"' EXIT

# ── 1. Pre-compile every test binary ONCE (decouple compile from run) ──────────
# The main benches span two packages (app = package main; app/pkg/config), and
# `go test -c` is per-package, so each side needs two binaries. The canary is its
# own module → one binary shared by both sides (byte-identical control).
compile() {
  local label="$1" dir="$2" pkg="$3" out="$4"
  echo "[bench_interleave] compile $label ($pkg)"
  if ! ( cd "$dir" && "$GO_BIN" test -c -o "$out" "$pkg" ) 2>>"$ERR_LOG"; then
    echo "[bench_interleave] FAILED to compile $label — see $ERR_LOG" >&2
    exit 3
  fi
}

compile "base/app"    "$BASE_DIR" "."           "$BIN_DIR/base_app.test"
compile "base/config" "$BASE_DIR" "./pkg/config" "$BIN_DIR/base_config.test"
compile "pr/app"      "$PR_DIR"   "."           "$BIN_DIR/pr_app.test"
compile "pr/config"   "$PR_DIR"   "./pkg/config" "$BIN_DIR/pr_config.test"
compile "canary"      "$CANARY_DIR" "."         "$BIN_DIR/canary.test"

# ── 2. Run a test binary once, appending its clean stdout to a side file ───────
# Compiled test binaries take the `-test.*` flag spelling (not the `go test`
# spelling). silenceLogs() already routes benchmark log output to io.Discard; any
# residual stderr (compile-time-absent here, but e.g. runtime warnings) goes to
# the err log. stdout is the benchstat-ready stream (goos/goarch/pkg/cpu headers
# + Benchmark rows + PASS).
run_main() {
  local bin="$1" out="$2"
  "$bin" -test.bench="$BENCH_RE" -test.benchmem -test.count=1 \
         -test.run='^$' -test.timeout=15m -test.benchtime="$BENCHTIME" \
    >>"$out" 2>>"$ERR_LOG"
}

run_canary() {
  local out="$1"
  "$BIN_DIR/canary.test" -test.bench=. -test.benchmem -test.count=1 \
         -test.run='^$' -test.benchtime="$CANARY_BENCHTIME" \
    >>"$out" 2>>"$ERR_LOG"
}

# ── 3. Interleave: each round runs both sides back-to-back, ALTERNATING which
# side leads. Interleaving cancels the large inter-batch drift; alternating the
# lead (base-first on odd rounds, pr-first on even) also nulls the small
# intra-round asymmetry — without it the leading side would always sample a few
# seconds earlier within every round, a systematic (if second-order) bias. Each
# side's samples always go to its own file, so benchstat still pairs correctly.
echo "[bench_interleave] $ROUNDS interleaved rounds (lead alternates) — bench='$BENCH_RE' benchtime=$BENCHTIME"
run_side() {  # $1=label $2=app.test $3=config.test $4=outfile
  echo "[bench_interleave] round $r/$ROUNDS — $1"
  run_main "$2" "$4"
  run_main "$3" "$4"
  run_canary "$4"
}
for r in $(seq 1 "$ROUNDS"); do
  if [ $((r % 2)) -eq 1 ]; then
    run_side base "$BIN_DIR/base_app.test" "$BIN_DIR/base_config.test" "$BASE_TXT"
    run_side pr   "$BIN_DIR/pr_app.test"   "$BIN_DIR/pr_config.test"   "$PR_TXT"
  else
    run_side pr   "$BIN_DIR/pr_app.test"   "$BIN_DIR/pr_config.test"   "$PR_TXT"
    run_side base "$BIN_DIR/base_app.test" "$BIN_DIR/base_config.test" "$BASE_TXT"
  fi
done

# `grep -c` exits 1 on zero matches; guard so it never aborts under pipefail
# (and would survive a future `shopt -s inherit_errexit`).
base_rows=$(grep -c '^Benchmark' "$BASE_TXT" || true)
pr_rows=$(grep -c '^Benchmark' "$PR_TXT" || true)
echo "[bench_interleave] done — base=${base_rows:-0} rows, pr=${pr_rows:-0} rows"
echo "[bench_interleave] base: $BASE_TXT"
echo "[bench_interleave] pr:   $PR_TXT"
echo "[bench_interleave] err:  $ERR_LOG"
