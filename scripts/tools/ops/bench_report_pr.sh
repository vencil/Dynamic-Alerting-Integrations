#!/usr/bin/env bash
# bench_report_pr.sh — shared PR-side informational reporter (comment + label)
# for the bench workflows that target a PR:
#   .github/workflows/bench-gate-pr.yaml    (pull_request auto-run)
#   .github/workflows/bench-on-demand.yaml  (/bench comment)
# Extracted with bench_gate_compare.sh (two-stage C→B rework, 2026-07).
# bench-attrib-main.yaml does NOT use this — it files per-commit issues
# instead (its own inline step; different policy, deliberately not shared).
#
# Phase 2 governance: a real regression (significant ≥5% slowdown WHILE the
# control canary was stable) posts a PR comment + adds the `perf-regression`
# label, but NEVER blocks merge (exit 0). When the regression clears on a
# later run the label is removed. INCONCLUSIVE runs (canary drifted) are not
# regressions. Fork PRs get a read-only token so comment/label calls fail;
# degrade to a warning + step summary rather than reach for
# `pull_request_target` (pwn-request risk).
#
# Env: GH_TOKEN, REPO, PR_NUMBER, REGRESSION, INCONCLUSIVE, REGRESSIONS_LIST

set -uo pipefail

LABEL="perf-regression"
is_regression=false
if [ "${REGRESSION:-}" = "true" ] && [ "${INCONCLUSIVE:-}" != "true" ]; then
  is_regression=true
fi

# gh write helper: on a fork PR the token is read-only and these fail;
# don't let that fail the job — warn and fall back to the step summary.
gh_try() {
  if ! gh "$@" 2>/tmp/gh_err; then
    echo "::warning::gh ${1:-} failed (likely fork-PR read-only token): $(cat /tmp/gh_err)"
    return 1
  fi
}

if [ "$is_regression" = "true" ]; then
  # Ensure the label exists (idempotent), then apply it.
  gh label create "$LABEL" --repo "$REPO" --color "D93F0B" \
    --description "Bench gate flagged a perf regression (informational)" --force >/dev/null 2>&1 || true
  gh_try pr edit "$PR_NUMBER" --repo "$REPO" --add-label "$LABEL" || true
  {
    echo "### ⚠️ Bench gate flagged a perf regression (informational — does not block merge)"
    echo ""
    echo "Significant ≥5% slowdown vs merge-base with a stable control canary:"
    echo '```'
    echo "${REGRESSIONS_LIST:-}"
    echo '```'
    echo "Fix the perf and push again, or note the deliberate trade-off in the PR. The"
    echo "\`$LABEL\` label clears automatically once the regression is gone."
  } > /tmp/perf_comment.md
  gh_try pr comment "$PR_NUMBER" --repo "$REPO" --body-file /tmp/perf_comment.md || true
  echo "::notice::Perf regression reported (informational). Merge is NOT blocked."
else
  # No regression (clean or inconclusive) → remove a stale label if present.
  gh pr edit "$PR_NUMBER" --repo "$REPO" --remove-label "$LABEL" >/dev/null 2>&1 || true
fi
# Always succeed — the report is informational.
exit 0
