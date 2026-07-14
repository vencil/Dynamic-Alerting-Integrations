#!/usr/bin/env bash
# bench_render_summary.sh — shared GITHUB_STEP_SUMMARY renderer for the three
# bench workflows (bench-gate-pr / bench-attrib-main / bench-on-demand).
# Extracted with bench_gate_compare.sh (two-stage C→B rework, 2026-07).
#
# Verdict inputs are bench_gate_compare.sh step outputs, bound via env:
#   REGRESSION, REGRESSIONS_LIST, INCONCLUSIVE, INCONCLUSIVE_REASON,
#   INCONCLUSIVE_CPUS, INCONCLUSIVE_CANARY, BENCHSTAT_TXT
# Consumer-specific texts, also via env:
#   COMPARE_DESC     markdown describing WHAT was compared (per workflow)
#   REGRESSION_NOTE  markdown appended under "Regressions flagged" (policy blurb)
#
# Writes to $GITHUB_STEP_SUMMARY (falls back to stdout for local runs).

set -euo pipefail
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/stdout}"

{
  if [ ! -f "${BENCHSTAT_TXT:-/nonexistent}" ]; then
    # The run died before producing a verdict (compile failure, harness error,
    # hard-kill). Say so explicitly — an empty-verdict render must NOT look
    # like a green result (fail-visible, not fail-open).
    echo "## 💥 Bench — no verdict produced (run failed before benchstat; see the job log)"
    exit 0
  fi
  # INCONCLUSIVE takes precedence over REGRESSION: if the runner was drifting,
  # the main-bench "regression" is itself untrustworthy.
  if [ "${INCONCLUSIVE:-}" = "true" ]; then
    echo "## ⚠️ Bench — INCONCLUSIVE (runner drift, re-run)"
  elif [ "${REGRESSION:-}" = "true" ]; then
    echo "## ❌ Bench — REGRESSION DETECTED"
  else
    echo "## ✅ Bench — no statistically significant regression"
  fi
  echo ""
  echo "${COMPARE_DESC:-}"
  echo "Gate threshold: **p < 0.01 AND |Δ| ≥ 5%** (statistical significance via \`benchstat -alpha=0.01\` + magnitude floor for free-tier-runner noise resistance)."
  echo "Metric scope: **deterministic per-op metrics only** (\`sec/op\`, \`B/op\`, \`allocs/op\`) via \`benchstat -filter\`. Non-deterministic process-level metrics (\`MB-sys\`, \`MB-heap-after-gc\`, \`goroutines\`) are excluded — they are GC/runtime high-water noise, not per-op work (#608)."
  echo "Topology: **single-runner INTERLEAVED** (base,head,base,head,… pre-compiled binaries, no mid-loop drop_caches) + **control canary**. Interleaving cancels time-correlated runner drift so benchstat's independent-samples assumption holds; the canary detects residual drift and judges the run INCONCLUSIVE rather than emitting a false regression (root-cause fix for #502/#608/#611/#695)."
  echo ""
  if [ "${INCONCLUSIVE:-}" = "true" ]; then
    echo "### ⚠️ Inconclusive — re-run recommended"
    echo ""
    echo "${INCONCLUSIVE_REASON:-}"
    echo ""
    echo "This run is **not** counted as a regression — re-run the job to get a clean comparison."
    if [ -n "${INCONCLUSIVE_CANARY:-}" ]; then
      echo ""
      echo "Control canary rows this run:"
      echo '```'
      echo "$INCONCLUSIVE_CANARY"
      echo '```'
    fi
    if [ -n "${INCONCLUSIVE_CPUS:-}" ]; then
      echo ""
      echo "Heterogeneous CPUs observed (unexpected on one runner):"
      echo '```'
      echo "$INCONCLUSIVE_CPUS"
      echo '```'
    fi
    echo ""
  fi
  echo "### Full benchstat output"
  echo "_\`BenchmarkControlCanaryCPU\` is the GATING drift probe; \`BenchmarkControlCanarySleep\` is informational only (scheduler jitter)._"
  echo '```'
  cat "$BENCHSTAT_TXT"
  echo '```'
  if [ "${INCONCLUSIVE:-}" != "true" ] && [ "${REGRESSION:-}" = "true" ]; then
    echo ""
    echo "### Regressions flagged"
    echo '```'
    echo "${REGRESSIONS_LIST:-}"
    echo '```'
    echo ""
    echo "${REGRESSION_NOTE:-}"
  fi
} >> "$SUMMARY"
