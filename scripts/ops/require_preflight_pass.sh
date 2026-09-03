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
#   4. If GIT_PREFLIGHT_STRICT=1 → require the marker regardless of PR state.
#      Otherwise, if `gh` confirms that none of the pushed branches has an
#      OPEN PR → skip the marker requirement. The idea: WIP/feature branches
#      without a PR yet are being iterated on; the marker requirement kicks
#      in once a PR exists (i.e. the work is ready to be reviewed, so CI
#      noise matters).
#      If `gh` is missing, or the PR query itself fails (unauthenticated,
#      API/network error) → require the marker (safe default).
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
#   * The refspecs come from scripts/ops/_prepush_refs.sh, not from stdin
#     directly: when this hook is installed through pre-commit, pre-commit has
#     already consumed the stdin git wrote, so reading it here yields nothing
#     and the `pushing_any_commit=0` branch below allowed EVERY push (#1664).
#     That helper's header carries the measurements and the known residual.
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

# Which refs is this push updating? One implementation, two channels.
# ⛔ Pure parameter expansion, NOT `$(dirname …)`: test_gh_missing_* strips PATH
# down to bash/git/basename/sh/cat to prove this gate still works without `gh`,
# and `dirname` is not in that set. Measured: it fails there with
# `dirname: command not found` and takes the whole gate down with it — on Linux
# only, so a Windows run reports the sourcing as fine.
_prepush_dir="${BASH_SOURCE[0]%/*}"
[ "$_prepush_dir" = "${BASH_SOURCE[0]}" ] && _prepush_dir="."
# ⛔ Say so when the helper is missing. `set -e` turns a failed `source` into a
# total abort — feature-branch pushes die too — under a bare "No such file or
# directory". The three cheapest ways out of that picture (--no-verify, delete
# the hook, copy the helper into .git/hooks and freeze it) all make things
# worse, so name the reinstall here instead.
if [ ! -r "$_prepush_dir/_prepush_refs.sh" ]; then
    cat >&2 <<'PREPUSH_MISSING'

[require_preflight_pass] ⛔ _prepush_refs.sh is not next to this script, so the
gate cannot tell what is being pushed.

Most likely cause: the hook was installed by copying this file alone (the
pre-#1664 recipe). It now needs its sibling helper.

Reinstall, either way:
  1. pre-commit (this repo's default path):
       pre-commit install --hook-type pre-push
  2. native hook, keeping the script inside the repo so the helper resolves:
       printf '%s\n%s\n' '#!/usr/bin/env bash' \
         'exec bash "$(git rev-parse --show-toplevel)/scripts/ops/require_preflight_pass.sh" "$@"' \
         > .git/hooks/pre-push && chmod +x .git/hooks/pre-push

⛔ Do not reach for --no-verify and do not delete .git/hooks/pre-push: both
turn off the direct-push-to-main guard for good, which is what #1664 fixed.

PREPUSH_MISSING
    exit 1
fi
# shellcheck source=scripts/ops/_prepush_refs.sh
. "$_prepush_dir/_prepush_refs.sh"

if ! _refs="$(prepush_refs)"; then
    prepush_refs_unavailable_message >&2
    exit 1
fi

pushing_to_protected=0
pushing_any_commit=0
pushed_branches=()
zero="0000000000000000000000000000000000000000"

# Each row: <remote_ref> <local_sha>. remote_ref comes FIRST on purpose:
# local_sha is legitimately empty on the first push of a branch to an empty
# remote (pre-commit exports no PRE_COMMIT_TO_REF there), and a leading empty
# field is collapsed by default-IFS `read`, which used to leave remote_ref
# empty and drop the row entirely — a silent allow. See the helper's header.
while read -r remote_ref local_sha; do
    [ -n "${remote_ref:-}" ] || continue
    # Deleting a ref (local_sha = zeros) — not a commit push, skip.
    if [ "$local_sha" = "$zero" ]; then
        continue
    fi
    # ⛔ Tag pushes: this file's header has promised "tag push → allow" since it
    # was written, and the code never did it. `${remote_ref##refs/heads/}` only
    # strips a heads/ prefix, so `refs/tags/v1.2.3` stayed intact and was
    # treated as a branch name — measured: a tag push was BLOCKED whenever `gh`
    # could not answer, which is the state the dev container is in (no `gh`
    # there), i.e. exactly the six-line release tag push. Harmless while the
    # guard was inert; #1664 made it live, so the promise has to be kept.
    case "$remote_ref" in
        refs/tags/*) continue ;;
    esac
    pushing_any_commit=1
    remote_branch="${remote_ref##refs/heads/}"
    if [ "$remote_branch" = "main" ] || [ "$remote_branch" = "master" ]; then
        pushing_to_protected=1
    fi
    pushed_branches+=("$remote_branch")
done <<< "$_refs"

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
# ⛔ Track WHY the marker ends up required. The banner used to state one reason
# unconditionally ("gate only triggers when branch has an OPEN PR — close the
# PR to push freely"), and that sentence is false in the `gh`-unavailable case,
# which is not exotic: the dev container this repo calls the 主路徑 has no `gh`
# at all. Following the false note reaches no green — there is no PR to close.
marker_reason="branch has an OPEN PR (CI cost matters once it is reviewable)"
if [ "${GIT_PREFLIGHT_STRICT:-0}" = "1" ]; then
    marker_reason="GIT_PREFLIGHT_STRICT=1 is set"
fi
if [ "${GIT_PREFLIGHT_STRICT:-0}" != "1" ]; then
    has_open_pr=0
    gh_available=0
    if command -v gh >/dev/null 2>&1; then
        gh_available=1
    else
        marker_reason="\`gh\` is not on PATH, so PR state is unknown (safe default)"
    fi
    if [ "$gh_available" = "1" ]; then
        for b in "${pushed_branches[@]}"; do
            # Query with `gh pr list --json`, not `gh pr view`: `pr view`
            # exits non-zero when the branch simply has no PR (NotFoundError),
            # so its exit code cannot separate "no PR" from "the query
            # failed". `pr list` with an exporter set prints `[]` and exits 0
            # when nothing matches, so here a non-zero exit means the query
            # itself failed — not authenticated, API or network error — and
            # that must fall back to requiring the marker rather than being
            # read as "this branch has no PR".
            if ! open_prs="$(gh pr list --head "$b" --state open \
                --json number --jq 'length' 2>/dev/null)"; then
                gh_available=0
                marker_reason="the \`gh\` PR query failed (not authenticated, or API/network), so PR state is unknown (safe default)"
                break
            fi
            case "$open_prs" in
                ''|0) ;;   # query succeeded, no open PR for this branch
                *)    has_open_pr=1; break ;;
            esac
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
║  Why the marker is required for THIS push:
║      ${marker_reason}
║                                                              ║
║  ⛔ If the reason above is about \`gh\`, there is no PR to close
║  and no WIP branch to switch to — run the preflight above, or
║  use the bypass. Force strict mode anywhere:
║      GIT_PREFLIGHT_STRICT=1 git push ...                     ║
║                                                              ║
║  Why: pushing without preflight risks CI-visible failures    ║
║  that block PR merges. See dev-rules #12 + windows-mcp       ║
║  playbook §PR 收尾流程.                                       ║
╚══════════════════════════════════════════════════════════════╝

EOF
exit 1
