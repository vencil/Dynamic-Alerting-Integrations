#!/usr/bin/env bash
# bench_gate_compare.sh — shared interleaved-bench + benchstat-verdict core for
# the three bench workflows (extracted from bench-gate-pr.yaml's inline steps
# in the two-stage C→B trigger rework, 2026-07, so the consumers cannot drift):
#
#   .github/workflows/bench-gate-pr.yaml      PR gate        (PR head vs merge-base)
#   .github/workflows/bench-attrib-main.yaml  post-merge attribution (HEAD vs HEAD^)
#   .github/workflows/bench-on-demand.yaml    /bench comment (PR head vs merge-base)
#
# Design rationale lives where it always did:
#   - v1→v6 topology history, governance, threshold provenance
#       → bench-gate-pr.yaml header
#   - interleave / pre-compile / no-mid-loop-drop_caches
#       → bench_interleave.sh header
# This script owns the MECHANICS: stash harness → worktrees → interleave →
# benchstat → verdict, emitted as GitHub step outputs.
#
# Inputs (environment)
#   BASE_SHA       (required) baseline commit (merge-base / HEAD^)
#   HEAD_SHA       (required) commit under test (PR head / main HEAD)
#   ROUNDS         default 6. DO NOT LOWER: benchstat's Mann-Whitney U
#                  per-side sample count IS ROUNDS; the two-sided minimum
#                  reachable p-value is 2/C(2n,n):
#                    n=6 → ≈0.0022 < α=0.01 ✓ | n=4 → ≈0.029 ✗ | n=2 → ≈0.33 ✗
#                  With ROUNDS<5 the α=0.01 significance gate is
#                  MATHEMATICALLY unreachable — every run reads "no
#                  significant regression" and the gate degrades to a
#                  permanently-green ornament. Statistical power lives in the
#                  sample count; tune BENCHTIME for wall-time instead.
#   BENCHTIME      default 1s (2026-07 stage-C slimming; was 3s)
#   BENCH_RE       default '_1000(_|$)|MixedMode|Simulate_DeepChain'
#   BENCH_OUT_DIR  default "$RUNNER_TEMP/bench-out" (mktemp -d fallback locally)
#   GITHUB_OUTPUT  step-output sink; falls back to "$BENCH_OUT_DIR/step-outputs.txt"
#   REPLICATE_TIMING_ONLY  default 1. A run whose only flagged rows are sec/op
#                  gets a second interleaved pass (see the Verdict section);
#                  0 reports the first pass as-is. Budget the caller's
#                  timeout-minutes for TWO passes.
#
# Requires: cwd = repo root of a FULL-HISTORY checkout (fetch-depth: 0 — both
# SHAs must resolve to commits), `go` and `python3` on PATH. Installs
# benchstat if absent.
#
# Outputs ($GITHUB_OUTPUT keys, written by scripts/tools/dx/bench_verdict.py)
#   regression=true|false      regressions        (multiline benchstat rows)
#   regression_basis=deterministic|timing-replicated|timing-not-replicated|
#                    timing-unreplicated|clean|inconclusive
#   unreplicated               (multiline; timing-only rows the second pass
#                              did not flag — reported, not counted)
#   inconclusive=true|false    inconclusive_reason
#   inconclusive_cpus / inconclusive_canary       (multiline, may be absent)
#   benchstat_txt=<path>       first-pass benchstat output, for summary rendering
#
# Exit codes — FAIL-LOUD policy: a verdict (even "regression detected") exits
# 0; the CALLER decides policy (comment / label / issue / nothing). Any
# infra or shape failure — missing benchstat, compile error, benchstat
# output-shape drift — exits non-zero: it must never masquerade as
# "no regression".

set -euo pipefail

die() { echo "[bench_gate_compare] $*" >&2; exit 2; }

[ -n "${BASE_SHA:-}" ] || die "BASE_SHA not set"
[ -n "${HEAD_SHA:-}" ] || die "HEAD_SHA not set"
git rev-parse --verify -q "$BASE_SHA^{commit}" >/dev/null || die "BASE_SHA does not resolve: $BASE_SHA (need fetch-depth: 0)"
git rev-parse --verify -q "$HEAD_SHA^{commit}" >/dev/null || die "HEAD_SHA does not resolve: $HEAD_SHA (need fetch-depth: 0)"
command -v go >/dev/null 2>&1 || die "go not on PATH"

ROUNDS="${ROUNDS:-6}"
BENCHTIME="${BENCHTIME:-1s}"
BENCH_RE="${BENCH_RE:-_1000(_|\$)|MixedMode|Simulate_DeepChain}"

TMP_ROOT="${RUNNER_TEMP:-$(mktemp -d)}"
OUT_DIR="${BENCH_OUT_DIR:-$TMP_ROOT/bench-out}"
mkdir -p "$OUT_DIR"
STEP_OUT="${GITHUB_OUTPUT:-$OUT_DIR/step-outputs.txt}"

# ── benchstat ────────────────────────────────────────────────────────────────
# @latest acceptable: benchstat output format stable for ~2 years. Pin to a
# specific version if a future release ever breaks either the text format
# bench_verdict.py parses OR the `-filter` benchfilter unit syntax
# `.unit:(sec/op OR B/op OR allocs/op)` (#608) — the #611 shape assertions in
# bench_verdict.py fail loud and point here. golang.org/x/perf has NO semver tags (only
# pseudo-versions), so pin to the last known-good pseudo-version, e.g.:
#   go install golang.org/x/perf/cmd/benchstat@v0.0.0-20260512194132-3cf34090a3db
if ! command -v benchstat >/dev/null 2>&1; then
  echo "[bench_gate_compare] installing benchstat"
  go install golang.org/x/perf/cmd/benchstat@latest
  export PATH="$PATH:$(go env GOPATH)/bin"
fi
command -v benchstat >/dev/null 2>&1 || die "benchstat install failed"

# ── Stash the interleave runner + control-canary module OUTSIDE the checkout ─
# so both sides use the SAME harness + SAME canary regardless of which
# commit's tree is being benched. A PR that modified either must not bench
# base with the old version and head with the new one — that would be an
# unfair comparison. (The canary module is also version-independent by
# construction: its own go.mod, stdlib-only.)
HARNESS_DIR="$TMP_ROOT/bench-harness"
rm -rf "$HARNESS_DIR"
mkdir -p "$HARNESS_DIR"
cp scripts/tools/ops/bench_interleave.sh "$HARNESS_DIR/"
cp -r scripts/tools/ops/bench-canary "$HARNESS_DIR/bench-canary"

# ── Two worktrees so base + head are checked out SIMULTANEOUSLY ──────────────
# The interleave runner needs both trees alive at once to alternate between
# them. Worktrees (vs sequential `git checkout <sha>`) also mean the main
# checkout is never mutated — no dirty-tree-blocks-checkout failure mode.
WT_BASE="$TMP_ROOT/wt-base"
WT_HEAD="$TMP_ROOT/wt-head"
cleanup() {
  git worktree remove --force "$WT_BASE" >/dev/null 2>&1 || true
  git worktree remove --force "$WT_HEAD" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup  # clear leftovers from a prior local run
git worktree add --detach "$WT_BASE" "$BASE_SHA"
git worktree add --detach "$WT_HEAD" "$HEAD_SHA"
echo "[bench_gate_compare] base worktree HEAD: $(git -C "$WT_BASE" rev-parse HEAD)"
echo "[bench_gate_compare] head worktree HEAD: $(git -C "$WT_HEAD" rev-parse HEAD)"

# ── One interleaved pass (base,head,base,head,…) + benchstat ─────────────────
# Pre-compiled binaries, alternating lead, NO mid-loop drop_caches — see
# bench_interleave.sh for why each of those is load-bearing (structural fix
# for the #502/#608/#611/#695 time-correlated-drift false-RED class).
#
# benchstat has TWO similar-looking flags that do different things:
#   -alpha α       (default 0.05)  → significance threshold for +/-/~ markers
#   -confidence c  (default 0.95)  → width of the displayed geomean ± CI
# Use `-alpha=0.01` to tighten significance (a previous version used
# `-confidence=0.99` thinking it did — it did NOT). The 5% magnitude floor
# that pairs with it lives in bench_verdict.py. [PR #437 catch.]
#
# METRIC SCOPE — `-filter '.unit:(sec/op OR B/op OR allocs/op)'` [#608]: the
# bench emits process-level custom metrics via b.ReportMetric (MB-sys,
# MB-heap-after-gc, goroutines, affected-tenants); MB-sys / MB-heap-after-gc
# are runtime/GC high-water readings that drift randomly BETWEEN processes and
# false-RED real Go PRs (root cause of #502). The gate sees per-op work only —
# benchstat normalizes ns/op → sec/op, hence the allowlist below. Excluded
# metrics remain visible in the nightly bench-record artifact, just not gated.
run_pass() {  # $1 = output dir; writes $1/benchstat.txt
  local dir="$1"
  mkdir -p "$dir"
  BASE_DIR="$WT_BASE/components/threshold-exporter/app" \
  PR_DIR="$WT_HEAD/components/threshold-exporter/app" \
  CANARY_DIR="$HARNESS_DIR/bench-canary" \
  BENCH_OUT_DIR="$dir" \
  ROUNDS="$ROUNDS" \
  BENCH_RE="$BENCH_RE" \
  BENCHTIME="$BENCHTIME" \
    bash "$HARNESS_DIR/bench_interleave.sh"
  benchstat -alpha=0.01 \
    -filter '.unit:(sec/op OR B/op OR allocs/op)' \
    "$dir/bench-base.txt" \
    "$dir/bench-pr.txt" \
    | tee "$dir/benchstat.txt"
}

# ── Verdict ──────────────────────────────────────────────────────────────────
# All reading of benchstat's text — regression rows per unit, the control
# canary, CPU headers, the #611 fail-loud shape assertions — lives in ONE
# place: scripts/tools/dx/bench_verdict.py. It runs from THIS checkout, not a
# stashed copy: it imports the repo's shared libs, and the checkout never
# changes between the two passes (only the worktrees are benched), so both
# passes are judged by the same version. Its exit 2 (unrecognised shape)
# propagates under `set -e`: a comparison nobody could parse must never read
# as "no regression".
#
# REPLICATION: a run whose only evidence is `sec/op` rows (B/op and allocs/op
# unchanged, canary stable) gets a SECOND independent interleaved pass, and a
# timing-only flag stands only for a benchmark both passes flag. Why, and why
# deterministic rows never need it, is in bench_verdict.py's header.
# REPLICATE_TIMING_ONLY=0 skips the second pass and reports the first as-is.
VERDICT="$PWD/scripts/tools/dx/bench_verdict.py"

run_pass "$OUT_DIR"
BENCHSTAT_TXT="$OUT_DIR/benchstat.txt"
replicate_args=()
if [ "${REPLICATE_TIMING_ONLY:-1}" = "1" ] \
   && [ "$(python3 "$VERDICT" needs-replication --benchstat "$BENCHSTAT_TXT")" = "yes" ]; then
  echo "[bench_gate_compare] only sec/op rows were flagged — running a replication pass"
  run_pass "$OUT_DIR/replicate"
  replicate_args=(--replicate "$OUT_DIR/replicate/benchstat.txt")
fi
python3 "$VERDICT" verdict --benchstat "$BENCHSTAT_TXT" \
  ${replicate_args[@]+"${replicate_args[@]}"} \
  --github-output "$STEP_OUT"
