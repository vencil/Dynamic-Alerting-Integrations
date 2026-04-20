#!/usr/bin/env bash
# require_preflight_pass.sh — pre-push gate: verify `make pr-preflight`
# ran against the current HEAD before allowing the push.
#
# Purpose:
#   Prevent pushing pre-preflight commits that CI will likely reject. The
#   gate checks for `.git/.preflight-ok.<HEAD-sha>` — written by
#   scripts/tools/dx/pr_preflight.py on PASS, cleared on FAIL.
#
# Logic (pre-push stdin: <local_ref> <local_sha> <remote_ref> <remote_sha>):
#   1. If GIT_PREFLIGHT_BYPASS=1 in env → allow (escape hatch)
#   2. If target branch is main/master → allow (protect_main_push owns that)
#   3. If no commits being pushed (delete ref, tag push, etc.) → allow
#   4. Marker present for HEAD sha → allow
#   5. Otherwise → block with instruction to run `make pr-preflight`
#
# Installed via .pre-commit-config.yaml:
#     - id: require-preflight-pass
#       stages: [pre-push]
#       always_run: true
#       entry: bash scripts/ops/require_preflight_pass.sh
#
# Design notes:
#   * Uses `git rev-parse --git-dir` for worktree safety.
#   * Reads stdin to inspect refspecs — same protocol protect_main_push uses.
#   * Non-blocking on edge cases (tag push, delete-ref) to avoid disrupting
#     release flow.
set -euo pipefail

MARKER_PREFIX=".preflight-ok"

# Escape hatch — emergency bypass.
if [ "${GIT_PREFLIGHT_BYPASS:-0}" = "1" ]; then
    echo "[require_preflight_pass] BYPASSED via GIT_PREFLIGHT_BYPASS=1" >&2
    exit 0
fi

git_dir="$(git rev-parse --git-dir 2>/dev/null || echo .git)"
head_sha="$(git rev-parse HEAD 2>/dev/null || echo '')"
if [ -z "$head_sha" ]; then
    # Empty repo or broken state — don't block; other hooks will catch it.
    exit 0
fi

# Read pre-push stdin to detect refs being pushed.
# Protocol: <local_ref> <local_sha> <remote_ref> <remote_sha> per line.
pushing_to_protected=0
pushing_any_commit=0
zero="0000000000000000000000000000000000000000"

while read -r local_ref local_sha remote_ref remote_sha; do
    # Deleting a ref (local_sha = zeros) — not a commit push, skip.
    if [ "$local_sha" = "$zero" ]; then
        continue
    fi
    pushing_any_commit=1
    remote_branch="${remote_ref##refs/heads/}"
    if [ "$remote_branch" = "main" ] || [ "$remote_branch" = "master" ]; then
        pushing_to_protected=1
    fi
done

# Nothing being pushed (empty stdin or all deletes) — allow.
if [ "$pushing_any_commit" = "0" ]; then
    exit 0
fi

# Pushing to main/master: protect_main_push will block it; we don't add noise.
if [ "$pushing_to_protected" = "1" ]; then
    exit 0
fi

marker="$git_dir/$MARKER_PREFIX.$head_sha"
if [ -f "$marker" ]; then
    # Marker present — preflight passed for this SHA. Allow.
    exit 0
fi

# No marker — block with actionable instructions.
cat >&2 <<EOF

╔══════════════════════════════════════════════════════════════╗
║  ⛔ Push blocked — preflight not run on HEAD                 ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  HEAD: ${head_sha}
║  Missing marker: $(basename "$marker")
║                                                              ║
║  Run this before pushing:                                    ║
║      make pr-preflight                                       ║
║                                                              ║
║  Emergency bypass (use sparingly):                           ║
║      GIT_PREFLIGHT_BYPASS=1 git push ...                     ║
║                                                              ║
║  Why: pushing without preflight risks CI-visible failures    ║
║  that block PR merges. See dev-rules #12 + windows-mcp       ║
║  playbook §PR 收尾流程.                                       ║
╚══════════════════════════════════════════════════════════════╝

EOF
exit 1
