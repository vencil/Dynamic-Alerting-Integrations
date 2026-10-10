#!/usr/bin/env bash
# require_preflight_pass.sh — pre-push gate: verify pr-preflight
# (scripts/tools/dx/pr_preflight.py, which `make pr-preflight` runs) ran
# against the commits being PUSHED before allowing the push.
#
# Purpose:
#   Prevent pushing pre-preflight commits that CI will likely reject. The
#   gate checks for `.git/.preflight-ok.<sha>`, written by
#   scripts/tools/dx/pr_preflight.py. ⛔ When a marker appears or disappears is
#   that side's business and is NOT restated here — see `write_marker` /
#   `clear_marker` there. This file only ever asks whether the file is present.
#
#   ⛔ `<sha>` is each PUSHED commit, not HEAD, and not "any of them" — the
#   quantifier and the commit are both load-bearing, and both directions of
#   getting them wrong are pinned in tests/dx/test_preflight_pass_gate.py.
#
#   ⛔ The marker lives in the SHARED git dir. Parsing the refspec is
#   _prepush_refs.sh's job, not this file's.
set -euo pipefail

MARKER_PREFIX=".preflight-ok"

# Escape hatch — emergency bypass.
if [ "${GIT_PREFLIGHT_BYPASS:-0}" = "1" ]; then
    echo "[require_preflight_pass] BYPASSED via GIT_PREFLIGHT_BYPASS=1" >&2
    exit 0
fi

# ⛔ Refuse rather than allow when git itself is missing. The previous form
# swallowed that into "empty repo" and exited 0 with ZERO bytes of output, so
# "checked and fine" and "never ran" were the same picture — the #1664 shape.
if ! command -v git >/dev/null 2>&1; then
    echo "[require_preflight_pass] ⛔ git is not on PATH, so this gate cannot" >&2
    echo "  see what is being pushed. Refusing rather than allowing blind." >&2
    exit 1
fi

# ⛔ --git-common-dir, NOT --git-dir. A marker says "preflight passed on this
# COMMIT", which is not a property of the worktree you happened to run it in.
# With the per-worktree dir, a marker written in one worktree was invisible to
# every other one — and this repo runs many. pr_preflight.py writes to the same
# place; the two must not drift apart.
git_dir="$(git rev-parse --git-common-dir 2>/dev/null || echo .git)"
# --verify, so an unborn HEAD is empty rather than the literal string "HEAD".
head_sha="$(git rev-parse --verify --quiet HEAD 2>/dev/null || echo '')"
if [ -z "$head_sha" ]; then
    # No commits yet — there is nothing a preflight could have run against.
    exit 0
fi

# Which refs is this push updating? Parsed by _prepush_refs.sh.
# ⛔ Pure parameter expansion, no `$(dirname …)`. Rationale in _prepush_refs.sh.
_prepush_dir="${BASH_SOURCE[0]%/*}"
[ "$_prepush_dir" = "${BASH_SOURCE[0]}" ] && _prepush_dir="."
# ⛔ Say so when the helper is missing. `set -e` turns a failed `source` into a
# total abort — feature-branch pushes die too — under a bare "No such file or
# directory". The three cheapest ways out of that picture (--no-verify, delete
# the hook, copy the helper into .git/hooks and freeze it) all make things
# worse, so name the way back here instead.
if [ ! -r "$_prepush_dir/_prepush_refs.sh" ]; then
    printf '\n[require_preflight_pass] ⛔ _prepush_refs.sh is not next to %s,\nso the gate cannot tell what is being pushed.\n' "${BASH_SOURCE[0]}" >&2
    printf '%s\n' \
        '' \
        'If that is in scripts/ops/, the helper is gone from your checkout. It is' \
        'version-controlled: restore it from HEAD (this works when the deletion is' \
        'staged too):' \
        '    git checkout HEAD -- :/scripts/ops/_prepush_refs.sh' \
        'The installer cannot restore it.' \
        '' \
        '⛔ Do not hand-write a hook that runs only this script: that silently drops' \
        'protect_main_push and the mkdocs strict check while this one still looks fine.' \
        '⛔ Do not use `pre-commit install --hook-type pre-push` either: a hook run by' \
        'pre-commit sees no refspec at all (#1664).' \
        '' \
        '⛔ Do not reach for --no-verify and do not delete .git/hooks/pre-push: both' \
        'turn off the direct-push-to-main guard for good, which is what #1664 fixed.' \
        '' \
        >&2
    exit 1
fi
# shellcheck source=scripts/ops/_prepush_refs.sh
. "$_prepush_dir/_prepush_refs.sh"

_refs="$(prepush_refs)"

pushing_to_protected=0
pushing_any_commit=0
pushed_branches=()
pushed_shas=()
zero="0000000000000000000000000000000000000000"

# Each row: <remote_ref> <local_sha>
while read -r remote_ref local_sha; do
    [ -n "${remote_ref:-}" ] || continue
    # Deleting a ref (local_sha = zeros) — not a commit push, skip.
    if [ "$local_sha" = "$zero" ]; then
        continue
    fi
    # ⛔ Only branches have a PR to answer for. Anything else (tags, notes)
    # would survive the `refs/heads/` strip below intact and be judged as a
    # branch name. Same filter as pre_push_mkdocs_strict.sh.
    case "$remote_ref" in refs/heads/*) ;; *) continue ;; esac
    pushing_any_commit=1
    remote_branch="${remote_ref##refs/heads/}"
    # ⛔ Skip THIS ROW, do not exit. protect_main_push owns main, so adding
    # noise there is pointless — but a whole-push early exit made one main row
    # silence the marker check for every other branch in the same push, which
    # is the "any" this gate exists to not be.
    if [ "$remote_branch" = "main" ] || [ "$remote_branch" = "master" ]; then
        pushing_to_protected=1
        continue
    fi
    pushed_branches+=("$remote_branch")
    # ⛔ The marker belongs to the COMMIT being published, not to the tree the
    # pusher happens to be standing in — the same axis #1690 fixed in the
    # mkdocs guard.
    pushed_shas+=("$local_sha")
done <<< "$_refs"

# Nothing being pushed (empty stdin, all deletes, no branch) — allow.
if [ "$pushing_any_commit" = "0" ]; then
    exit 0
fi

# Only main/master was pushed: protect_main_push owns that verdict.
if [ "${#pushed_branches[@]}" -eq 0 ] && [ "$pushing_to_protected" = "1" ]; then
    exit 0
fi

# Conditional gate: only require marker when an OPEN PR exists for at least
# one of the pushed branches. Without a PR, this is WIP work and the user
# shouldn't be blocked. Once the PR is opened, CI cost matters again.
#
# STRICT mode overrides (always require marker, regardless of PR state):
#   GIT_PREFLIGHT_STRICT=1 git push ...
# ⛔ Track WHY the marker ends up required; the banner must not state one reason
# unconditionally. "Close the PR to push freely" reaches no green when `gh` is
# simply absent — which is the dev container's normal state.
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
            # ⛔ EMPTY is not "no PR". Measured: `--jq 'length'` prints `0`
            # for a branch with no PR and `1` when there is one — it is never
            # empty, so empty means the query produced nothing while still
            # exiting 0 (a wrapper, a pager, a stripped exporter). Folding it
            # in with `0` made unknown mean OK.
            case "$open_prs" in
                0)  ;;   # query succeeded, no open PR for this branch
                '') gh_available=0
                    marker_reason="the \`gh\` PR query returned nothing, so PR state is unknown (safe default)"
                    break ;;
                *)  has_open_pr=1; break ;;
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

# Every commit this push publishes needs its own marker. ⛔ Not HEAD: "standing
# on A while pushing B" is ordinary here, and keying the check to HEAD both
# let an unverified B through when A happened to be marked, and blocked a
# verified B when A was not.
_missing_sha=""
_missing_branch=""
for _i in "${!pushed_shas[@]}"; do
    if [ ! -f "$git_dir/$MARKER_PREFIX.${pushed_shas[$_i]}" ]; then
        _missing_sha="${pushed_shas[$_i]}"
        _missing_branch="${pushed_branches[$_i]}"
        break
    fi
done

if [ -z "$_missing_sha" ]; then
    # Every pushed commit has a marker — preflight passed for all of them.
    exit 0
fi

marker="$git_dir/$MARKER_PREFIX.$_missing_sha"
# Where to run preflight. It marks HEAD and checks the working tree, so the
# tree you push from qualifies only when its HEAD IS the pushed commit and its
# tracked files are clean. Anything else gets a throwaway worktree: at that
# commit, clean, and nobody else's (#1952).
# ⛔ One line, run in a subshell, absolute paths: the instruction must not move
# the shell you push from — a relative refspec (`git push origin HEAD~1:x`) is
# re-read from wherever that shell stands.
# ⛔ A `status` that fails is not "clean": unknown must not pick the tree.
# The instruction must run where it is pasted (#1920). `make pr-preflight` is
# only `python3 scripts/tools/dx/pr_preflight.py`, and neither make (Git Bash
# on a Windows host) nor a working python3 (there, a Store stub that exits
# non-zero) can be assumed: ask the PATH this hook inherited which interpreter
# actually starts.
_preflight_cmd=""
for _py in python3 python; do
    # -S: no site, so no .pth hooks — under a test run's coverage one hung here
    # (cwd with a newline). Not -I: it would also ignore a broken PYTHONHOME,
    # and the printed command, run without it, would then fail.
    if "$_py" -S -c '' >/dev/null 2>&1; then
        _preflight_cmd="$_py scripts/tools/dx/pr_preflight.py"
        break
    fi
done
_py_note=""
if [ -z "$_preflight_cmd" ]; then
    _preflight_cmd="python3 scripts/tools/dx/pr_preflight.py"
    _py_note="║  (neither python3 nor python starts from this PATH — install one first)"
fi
_here=""
if [ "$_missing_sha" = "$head_sha" ] \
    && _dirty="$(git --no-optional-locks status --porcelain --untracked-files=no 2>/dev/null)" \
    && [ -z "$_dirty" ]; then
    _here="$(git rev-parse --show-toplevel 2>/dev/null)" || _here=""
fi
if [ -n "$_here" ]; then
    printf -v _here_q '%q' "$_here"
    _checkout_hint="    (cd ${_here_q} && ${_preflight_cmd})"
else
    # ⛔ `&&` before the trap: a failed `add` (path already there) must not
    # remove what is there. `$$` keeps two pushes' paths apart.
    # ⛔ An EXIT trap, not a clean-up after the command: Ctrl-C or SIGTERM mid
    # preflight skips a following command, and the throwaway is left behind
    # (#2038). The subshell's status is preflight's; the trap does not change it.
    # ⛔ `cd` in a nested subshell: the trap's shell must not stand in the tree
    # it removes (Windows refuses to delete a process's cwd). ⚠️ NOT GUARDED:
    # Linux deletes a cwd without complaint, so no test here can see this.
    # ⚠️ Relies on bash running an EXIT trap on a signal; dash does not always.
    _common="$(CDPATH='' cd "$git_dir" && pwd)"
    _tmp_root="$(CDPATH='' cd "${TMPDIR:-/tmp}" 2>/dev/null && pwd)" || _tmp_root="/tmp"
    printf -v _common_q '%q' "$_common"
    printf -v _tmp_wt_q '%q' "${_tmp_root}/preflight-${_missing_sha:0:12}-$$"
    # Quoted twice: the shell you paste into reads it once, the trap once more.
    printf -v _remove_q '%q' "git -C ${_common_q} worktree remove --force ${_tmp_wt_q}"
    _checkout_hint="    (git -C ${_common_q} worktree add --detach ${_tmp_wt_q} ${_missing_sha} && trap ${_remove_q} EXIT && (cd ${_tmp_wt_q} && ${_preflight_cmd}))"
fi

# No marker — block with actionable instructions.
printf '%s\n' \
    "" \
    "╔══════════════════════════════════════════════════════════════╗" \
    "║  ⛔ Push blocked — no preflight PASS for this commit         ║" \
    "╠══════════════════════════════════════════════════════════════╣" \
    "║                                                              ║" \
    "║  Pushing: ${_missing_branch} -> ${_missing_sha}" \
    "║  HEAD:    ${head_sha}" \
    "║  Missing marker: ${marker##*/}" \
    "║                                                              ║" \
    "║  No marker means one of: preflight never ran on this commit," \
    "║  it did not pass, or a later FAIL on this commit removed it —" \
    "║  from ANY worktree standing on it, since markers are shared." \
    "║                                                              ║" \
    "║  ⛔ The marker names a COMMIT, not \"now\". If HEAD above is a" \
    "║  different commit, running preflight where you stand writes" \
    "║  the marker for THAT commit and this push stays blocked." \
    "║                                                              ║" \
    "║  Run this in bash (on Windows: Git Bash) before pushing:" \
    "${_checkout_hint}" \
    ${_py_note:+"$_py_note"} \
    "║                                                              ║" \
    "║  Emergency bypass (use sparingly):                           ║" \
    "║      GIT_PREFLIGHT_BYPASS=1 git push ...                     ║" \
    "║                                                              ║" \
    "║  Why the marker is required for THIS push:" \
    "║      ${marker_reason}" \
    "║                                                              ║" \
    "║  ⛔ If the reason above is about \`gh\`, there is no PR to close" \
    "║  and no WIP branch to switch to — run the preflight above, or" \
    "║  use the bypass. Force strict mode anywhere:" \
    "║      GIT_PREFLIGHT_STRICT=1 git push ...                     ║" \
    "║                                                              ║" \
    "║  Why: pushing without preflight risks CI-visible failures    ║" \
    "║  that block PR merges. See dev-rules #12.                    ║" \
    "╚══════════════════════════════════════════════════════════════╝" \
    "" \
    >&2
exit 1
