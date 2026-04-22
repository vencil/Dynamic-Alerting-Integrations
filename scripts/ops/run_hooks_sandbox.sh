#!/usr/bin/env bash
# run_hooks_sandbox.sh — sandbox-side pre-commit runner for the Windows
# escape hatch.
#
# Purpose:
#   make win-commit must call git from the Windows side (trap #47 / #36 /
#   FUSE index corruption), which means `win_git_escape.bat commit-file`
#   uses `--no-verify` internally and pre-commit hooks get skipped.
#   This wrapper closes the gap: it runs pre-commit's auto-stage hooks
#   against the user-supplied FILES list BEFORE the Windows-side git
#   commit. If any hook fails, make aborts and the user fixes the issue
#   before the commit reaches git.exe.
#
# Why sandbox-side:
#   1. Cowork VM has a clean native ext4 filesystem — no FUSE cache
#      staleness or dentry-lock traps.
#   2. All 31 auto-stage hooks in .pre-commit-config.yaml are pure Python
#      (+ pyyaml). None require docker, Go, or Helm, so the sandbox is a
#      complete execution environment for them.
#   3. `pre-commit run --files <list>` bypasses pre-commit's stash logic,
#      which would otherwise trip on FUSE-side `.git/index` corruption.
#
# Usage:
#   scripts/ops/run_hooks_sandbox.sh FILE1 [FILE2 ...]
#
# Environment variables:
#   SKIP        — passed through to pre-commit (CSV of hook IDs to skip)
#   PRECOMMIT_LOG — override log destination (default: _sandbox_hooks.log)
#
# Concurrency note:
#   The default log path (_sandbox_hooks.log) is a single shared file in
#   the repo root. If two `make win-commit` invocations run in parallel
#   they will overwrite each other's log and misreport status. Do NOT
#   run concurrent win-commit / run_hooks_sandbox invocations — either
#   serialize them, or override PRECOMMIT_LOG per caller:
#     PRECOMMIT_LOG=_sandbox_hooks.$$.log scripts/ops/run_hooks_sandbox.sh ...
#
# Exit codes:
#   0   All hooks passed
#   1   At least one hook failed (see log for details)
#   2   Invalid arguments or environment
#
# Output (grep-friendly, last line):
#   HOOKS STATUS=PASS FILES=<n> DURATION=<s>s
#   HOOKS STATUS=FAIL FILES=<n> DURATION=<s>s LOG=<path>

set -euo pipefail

# --- Argument check ----------------------------------------------------------
if [ "$#" -lt 1 ]; then
    echo "Usage: $0 FILE1 [FILE2 ...]" >&2
    echo "HOOKS STATUS=FAIL FILES=0 DURATION=0s REASON=no-files-given" >&2
    exit 2
fi

# --- Environment check -------------------------------------------------------
if ! python3 -m pre_commit --version >/dev/null 2>&1; then
    echo "[run_hooks_sandbox] pre-commit not installed in this Python. Try: pip install pre-commit" >&2
    echo "HOOKS STATUS=FAIL FILES=0 DURATION=0s REASON=precommit-missing" >&2
    exit 2
fi

# Find repo root (look for .pre-commit-config.yaml upward from script dir)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
if [ ! -f "$REPO_ROOT/.pre-commit-config.yaml" ]; then
    echo "[run_hooks_sandbox] .pre-commit-config.yaml not found at $REPO_ROOT" >&2
    echo "HOOKS STATUS=FAIL FILES=0 DURATION=0s REASON=config-missing" >&2
    exit 2
fi

cd "$REPO_ROOT"

# --- Normalize file paths (relative to repo root) ----------------------------
# Users may pass absolute or relative paths; pre-commit --files expects
# paths relative to the repo root.
FILES=()
for f in "$@"; do
    # Strip leading repo root if absolute
    case "$f" in
        "$REPO_ROOT/"*) FILES+=("${f#$REPO_ROOT/}") ;;
        /*)             FILES+=("$f") ;;  # absolute but outside repo — pre-commit will reject
        *)              FILES+=("$f") ;;
    esac
done

LOG_FILE="${PRECOMMIT_LOG:-_sandbox_hooks.log}"
: > "$LOG_FILE"

# --- Run hooks ---------------------------------------------------------------
START=$(date +%s)
FILE_COUNT="${#FILES[@]}"

echo "[run_hooks_sandbox] Running pre-commit on $FILE_COUNT file(s)..." >&2
echo "[run_hooks_sandbox] Log: $LOG_FILE" >&2

set +e
python3 -m pre_commit run \
    --hook-stage pre-commit \
    --files "${FILES[@]}" 2>&1 | tee "$LOG_FILE"
RC=${PIPESTATUS[0]}
set -e

END=$(date +%s)
DURATION=$((END - START))

# --- Emit summary ------------------------------------------------------------
if [ "$RC" -eq 0 ]; then
    echo "HOOKS STATUS=PASS FILES=$FILE_COUNT DURATION=${DURATION}s"
    exit 0
else
    echo "HOOKS STATUS=FAIL FILES=$FILE_COUNT DURATION=${DURATION}s LOG=$LOG_FILE" >&2
    exit 1
fi
