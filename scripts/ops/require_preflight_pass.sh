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
#   4. If GIT_PREFLIGHT_STRICT=1 or none of the pushed branches has an OPEN
#      PR (checked via `gh pr view`) → skip the marker requirement. The idea:
#      WIP/feature branches without a PR yet are being iterated on; the
#      marker requirement kicks in once a PR exists (i.e. the work is ready
#      to be reviewed, so CI noise matters).
#      If `gh` is missing / unauthenticated / errors → fall back to "require
#      marker" (safe default, preserves prior behavior).
#   5. Marker present for HEAD sha → allow
#   6. Otherwise → block with instruction to run `make pr-preflight`
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
pushed_branches=()
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
    pushed_branches+=("$remote_branch")
done

# Nothing being pushed (empty stdin or all deletes) — allow.
if [ "$pushing_any_commit" = "0" ]; then
    exit 0
fi

# Pushing to main/master: protect_main_push will block it; we don't add noise.
if [ "$pushing_to_protected" = "1" ]; then
    exit 0
fi

# Conditional gate: only require marker when an OPEN PR exists for at least
# one of the pushed branches. Without a PR, this is WIP work and the user
# shouldn't be blocked. Once the PR is opened, CI cost matters again.
#
# STRICT mode overrides (always require marker, regardless of PR state):
#   GIT_PREFLIGHT_STRICT=1 git push ...
if [ "${GIT_PREFLIGHT_STRICT:-0}" != "1" ]; then
    has_open_pr=0
    gh_available=0
    if command -v gh >/dev/null 2>&1; then
        gh_available=1
    fi
    if [ "$gh_available" = "1" ]; then
        for b in "${pushed_branches[@]}"; do
            # `gh pr view <branch>` resolves the PR whose head matches this
            # branch on the current repo. Non-zero exit = no PR, not
            # authenticated, or API error; treat all as "no open PR" (the
            # gh_available=1 + has_open_pr=0 block below then allows the push).
            state="$(gh pr view "$b" --json state --jq '.state' 2>/dev/null || true)"
            if [ "$state" = "OPEN" ]; then
                has_open_pr=1
                break
            fi
        done
    fi

    # If gh is unavailable, be conservative: require marker (old behavior).
    # If gh confirmed no open PR, skip the marker requirement (new behavior).
    if [ "$gh_available" = "1" ] && [ "$has_open_pr" = "0" ]; then
        # WIP branch, no PR yet — let it through.
        exit 0
    fi
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
║  Note: gate only triggers when branch has an OPEN PR.        ║
║  Close the PR (or switch to a WIP branch) to push freely.    ║
║  Force strict mode: GIT_PREFLIGHT_STRICT=1 git push ...      ║
║                                                              ║
║  Why: pushing without preflight risks CI-visible failures    ║
║  that block PR merges. See dev-rules #12 + windows-mcp       ║
║  playbook §PR 收尾流程.                                       ║
╚══════════════════════════════════════════════════════════════╝

EOF
exit 1
