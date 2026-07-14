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
#
# Requires: cwd = repo root of a FULL-HISTORY checkout (fetch-depth: 0 — both
# SHAs must resolve to commits), `go` on PATH. Installs benchstat if absent.
#
# Outputs ($GITHUB_OUTPUT keys)
#   regression=true|false      regressions        (multiline benchstat rows)
#   inconclusive=true|false    inconclusive_reason
#   inconclusive_cpus / inconclusive_canary       (multiline, may be absent)
#   benchstat_txt=<path>       full benchstat output, for summary rendering
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
# specific version if a future release ever breaks either the
# regression-detection grep/awk OR the `-filter` benchfilter unit syntax
# `.unit:(sec/op OR B/op OR allocs/op)` (#608) — the #611 shape assertions
# below fail loud and point here. golang.org/x/perf has NO semver tags (only
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

# ── Interleaved benchmark (base,head,base,head,…) ────────────────────────────
# Pre-compiled binaries, alternating lead, NO mid-loop drop_caches — see
# bench_interleave.sh for why each of those is load-bearing (structural fix
# for the #502/#608/#611/#695 time-correlated-drift false-RED class).
BASE_DIR="$WT_BASE/components/threshold-exporter/app" \
PR_DIR="$WT_HEAD/components/threshold-exporter/app" \
CANARY_DIR="$HARNESS_DIR/bench-canary" \
BENCH_OUT_DIR="$OUT_DIR" \
ROUNDS="$ROUNDS" \
BENCH_RE="$BENCH_RE" \
BENCHTIME="$BENCHTIME" \
  bash "$HARNESS_DIR/bench_interleave.sh"

# ── benchstat compare ────────────────────────────────────────────────────────
# IMPORTANT — benchstat has TWO similar-looking flags that do different things:
#   -alpha α       (default 0.05)  → significance threshold for +/-/~ markers
#   -confidence c  (default 0.95)  → width of the displayed geomean ± CI
# Use `-alpha=0.01` to tighten significance (a previous version used
# `-confidence=0.99` thinking it did — it did NOT). Plus a 5% magnitude floor
# (industry-standard dual-threshold) to filter sub-noise FPs. [PR #437 catch.]
#
# METRIC SCOPE — `-filter '.unit:(sec/op OR B/op OR allocs/op)'` [#608]: the
# detector greps the WHOLE benchstat output for `+N% (p<α)` rows, so it must
# never see non-deterministic metrics. The bench emits process-level custom
# metrics via b.ReportMetric (MB-sys, MB-heap-after-gc, goroutines,
# affected-tenants); MB-sys / MB-heap-after-gc are runtime/GC high-water
# readings that drift randomly BETWEEN processes and false-RED real Go PRs
# (root cause of #502). Restrict the gate to DETERMINISTIC per-op work only —
# benchstat normalizes ns/op → sec/op, hence the allowlist below. Excluded
# metrics remain visible in the nightly bench-record artifact, just not gated.
BENCHSTAT_TXT="$OUT_DIR/benchstat.txt"
benchstat -alpha=0.01 \
  -filter '.unit:(sec/op OR B/op OR allocs/op)' \
  "$OUT_DIR/bench-base.txt" \
  "$OUT_DIR/bench-pr.txt" \
  | tee "$BENCHSTAT_TXT"

# Fail-loud shape assertions (#611): the detector below greps for `+N% (p=…)`
# rows AND relies on the `-filter` unit names. benchstat is @latest — a future
# release that renames a unit or changes the row format would make every grep
# miss → regression=false → a GREEN gate silently masking ALL regressions.
# Require the filtered comparison to have produced recognizable output.
if ! grep -Eq 'sec/op|B/op|allocs/op' "$BENCHSTAT_TXT"; then
  echo "::error::benchstat output shape changed — no metric-section header (sec/op / B/op / allocs/op) in the filtered comparison. The regression detector may be silently passing; pin benchstat to a known-good version (see the benchstat install note in this script)."
  exit 1
fi
if ! grep -Eq '±[[:space:]]*[0-9]+%' "$BENCHSTAT_TXT"; then
  echo "::error::benchstat output shape changed — no per-bench result row (matching '± N%') in the filtered comparison. The regression detector may be silently passing; pin benchstat to a known-good version (see the benchstat install note in this script)."
  exit 1
fi

# ── Two-tier regression detection ────────────────────────────────────────────
# (metric scope already applied above via benchstat -filter)
# (1) benchstat filters by α=0.01: only `+` rows are statistically
#     significant slowdowns.
# (2) Additionally require |Δ| ≥ MAGNITUDE_FLOOR_PCT to filter noise-floor
#     false positives (free-tier within-run noise is ~1-3%).
# POSIX awk for portability. `|| true` tolerates "no matches" (the happy
# path — zero regressions) so pipefail doesn't crash the script.
# `grep -v ControlCanary` excludes BOTH control canaries — they are
# environment probes, not product benchmarks, and are handled by the
# INCONCLUSIVE check below. Without this a drifting canary could itself be
# counted as a "regression".
MAGNITUDE_FLOOR_PCT=5.0
regressions=$(grep -E '[[:space:]]\+[0-9]+\.[0-9]+%[[:space:]]*\(p=' \
                "$BENCHSTAT_TXT" \
              | grep -v geomean \
              | grep -v ControlCanary \
              | awk -v floor="$MAGNITUDE_FLOOR_PCT" '{
                  if (match($0, /\+[0-9]+\.[0-9]+%/)) {
                    num = substr($0, RSTART + 1, RLENGTH - 2) + 0
                    if (num >= floor) print
                  }
                }' || true)

# ── INCONCLUSIVE — control-canary drift detection ────────────────────────────
# The CPU canary (BenchmarkControlCanaryCPU) runs byte-identical work on both
# sides, so a statistically-significant base-vs-head delta on it means THIS
# run's runner was drifting (thermal / frequency / noisy neighbour) — the
# whole comparison is untrustworthy → INCONCLUSIVE (re-run), NOT a regression.
# The Sleep canary is INFORMATIONAL ONLY (µs-scale scheduler jitter would flap
# the gate). Sign-agnostic: a -N% canary drift (head unfairly favoured) is
# just as disqualifying as +N%.
CPU_CANARY_FLOOR_PCT=4.0
# Fail-safe: the canary is stashed and run EVERY time, independent of the
# base/head trees, so it must always appear. If it doesn't, the
# environment-stability check silently lost its teeth — treat the run as
# INCONCLUSIVE rather than gating on data we can't vouch for.
canary_missing=false
if ! grep -q 'ControlCanaryCPU' "$BENCHSTAT_TXT"; then
  canary_missing=true
  echo "::warning::CPU control canary row absent from benchstat output — cannot verify runner stability this run."
fi
canary_drift=$(grep -E 'ControlCanaryCPU' "$BENCHSTAT_TXT" \
               | grep -E '[-+][0-9]+\.[0-9]+%[[:space:]]*\(p=' \
               | awk -v floor="$CPU_CANARY_FLOOR_PCT" '{
                   if (match($0, /[-+][0-9]+\.[0-9]+%/)) {
                     pct = substr($0, RSTART, RLENGTH - 1) + 0
                     if (pct < 0) pct = -pct
                     if (pct >= floor) print
                   }
                 }' || true)

# Defense-in-depth: heterogeneous CPU headers should be impossible on a single
# interleaved runner, but if runner provisioning ever changes this still
# surfaces it as INCONCLUSIVE rather than a silent FN.
cpu_lines=$(grep -E '^cpu:' "$BENCHSTAT_TXT" 2>/dev/null | sort -u || true)

inconclusive=false
inconclusive_reason=""
if [ "$canary_missing" = "true" ]; then
  inconclusive=true
  inconclusive_reason="Control canary absent from output — cannot verify runner stability this run. Re-run."
  echo "::warning::$inconclusive_reason"
fi
if [ -n "$canary_drift" ]; then
  inconclusive=true
  inconclusive_reason="CPU control canary drifted ≥ ${CPU_CANARY_FLOOR_PCT}% — this run's runner was unstable (thermal/frequency/neighbour). Comparison untrustworthy; re-run."
  echo "::warning::$inconclusive_reason"
  {
    echo "inconclusive_canary<<__CANEOF__"
    grep -E 'ControlCanary' "$BENCHSTAT_TXT" || true
    echo "__CANEOF__"
  } >> "$STEP_OUT"
fi
if [ -n "$cpu_lines" ] && [ "$(echo "$cpu_lines" | wc -l)" -gt 1 ]; then
  inconclusive=true
  inconclusive_reason="Heterogeneous CPUs in a single interleaved job — investigate runner provisioning."
  cpu_count=$(echo "$cpu_lines" | wc -l)
  echo "::error::Heterogeneous CPUs detected ($cpu_count distinct) IN A SINGLE INTERLEAVED JOB. This shouldn't be possible; investigate runner provisioning. CPUs observed:"
  echo "$cpu_lines"
  {
    echo "inconclusive_cpus<<__CPUEOF__"
    echo "$cpu_lines"
    echo "__CPUEOF__"
  } >> "$STEP_OUT"
fi

{
  echo "inconclusive=$inconclusive"
  echo "inconclusive_reason<<__REOF__"
  echo "$inconclusive_reason"
  echo "__REOF__"
  echo "benchstat_txt=$BENCHSTAT_TXT"
} >> "$STEP_OUT"

if [ -n "$regressions" ]; then
  {
    echo "regression=true"
    echo "regressions<<__EOF__"
    echo "$regressions"
    echo "__EOF__"
  } >> "$STEP_OUT"
else
  echo "regression=false" >> "$STEP_OUT"
fi

echo "[bench_gate_compare] verdict: regression=$([ -n "$regressions" ] && echo true || echo false) inconclusive=$inconclusive"
