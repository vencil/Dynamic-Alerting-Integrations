#!/usr/bin/env bash
# run-hooks.sh — session-guard launcher: resolve a WORKING python, then exec
# the guard script (#824 root-cure).
#
# Why this exists
# ---------------
# The PreToolUse hook commands used bare `python`, which on Windows hosts
# resolves to the MS-Store App-Execution-Alias stub ("Python was not found",
# exit 49). Both session guards died silently for 7 weeks — hook failures
# are non-blocking by protocol, and only exit 2 feeds stderr to the model.
#
# Why bash (not python) does the probing: you cannot use python to find
# python (bootstrapping). Git Bash ships with the repo's required tooling
# on Windows; dev container / CI are Linux.
#
# ⛔ Probe rule: FUNCTIONAL probe only. `command -v python3` SUCCEEDS on the
# Store stub (it is a real executable on PATH) — the exact failure mode that
# burned us. A candidate counts only if it can actually run `import sys`.
#
# Failure policy (fail-loud, NOT fail-closed — #824 外審 reframe):
#   No working interpreter → emit PreToolUse JSON additionalContext on
#   stdout (exit 0) so the model learns the guards are down, plus a stderr
#   line for the transcript. Blocking every tool call here would brick the
#   session (the agent could no longer even fix the environment).
#
# Usage:
#   run-hooks.sh <guard-script.py> [args...]   # hook entry (stdin passes through)
#   run-hooks.sh --probe                        # liveness check (pre-commit gate)
set -u

GUARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Functional probe — never consume the hook's stdin payload (</dev/null).
_works() { "$@" -c "import sys" </dev/null >/dev/null 2>&1; }

INTERP=()
if _works py -3; then
  INTERP=(py -3)
elif _works python3; then
  INTERP=(python3)
elif _works python; then
  INTERP=(python)
fi

if [ "${#INTERP[@]}" -eq 0 ]; then
  if [ "${1:-}" = "--probe" ]; then
    echo "[run-hooks] FAIL: no working python (py -3 / python3 / python all failed functional probe)" >&2
    exit 1
  fi
  # Fail-loud without blocking: additionalContext reaches the model as a
  # system reminder; stderr line lands in the transcript. Exit 0 by design.
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"[session-guards] CRITICAL: no working Python interpreter found (py/python3/python all failed a functional probe). Session guards (VS Code Git toggle, sed -i interception) are NOT active. Apply dev-rules #11 manually and fix the interpreter — see issue #824."}}'
  echo "[run-hooks] CRITICAL: no working python for session guards (see issue #824)" >&2
  exit 0
fi

if [ "${1:-}" = "--probe" ]; then
  echo "[run-hooks] interpreter ok: ${INTERP[*]}"
  exit 0
fi

if [ "$#" -lt 1 ]; then
  echo "[run-hooks] usage: run-hooks.sh <guard-script.py> [args...] | --probe" >&2
  exit 0  # never block tool calls on launcher misuse
fi

SCRIPT_NAME="$1"
shift
if [ ! -f "$GUARD_DIR/$SCRIPT_NAME" ]; then
  echo "[run-hooks] WARNING: guard script not found: $GUARD_DIR/$SCRIPT_NAME" >&2
  exit 0  # never block
fi

# exec preserves stdin (PreToolUse JSON payload) and exit code (2 = block).
exec "${INTERP[@]}" "$GUARD_DIR/$SCRIPT_NAME" "$@"
