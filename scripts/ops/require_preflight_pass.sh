#!/usr/bin/env bash
# require_preflight_pass.sh — pre-push gate: verify `make pr-preflight`
# ran against the commits being PUSHED before allowing the push.
#
# Purpose:
#   Prevent pushing pre-preflight commits that CI will likely reject. The
#   gate checks for `.git/.preflight-ok.<sha>` — written by
#   scripts/tools/dx/pr_preflight.py on PASS, cleared on FAIL.
#
#   ⛔ `<sha>` is each PUSHED commit, not HEAD, and not "any of them" — the
#   quantifier and the commit are both load-bearing, and both directions of
#   getting them wrong are pinned in tests/dx/test_preflight_pass_gate.py.
#
#   ⛔ The marker lives in the SHARED git dir. Which channel carries the
#   refspec is _prepush_refs.sh's problem, not this file's; its header has the
#   measurements and the known residuals.
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

# Which refs is this push updating? One implementation, two channels.
# ⛔ Pure parameter expansion, no `$(dirname …)`. Rationale in _prepush_refs.sh.
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
pre-#1664 recipe). It needs its sibling helper.

Reinstall — since #1689 there is exactly one way:
    bash scripts/ops/install_prepush_hook.sh

⛔ Do not hand-write a hook that runs only this script: that silently drops
protect_main_push and the mkdocs strict check while this one still looks fine.
⛔ Do not use `pre-commit install --hook-type pre-push` either: a hook run by
pre-commit sees only one refspec (#1689).

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
pushed_shas=()
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
    # ⛔ Tags before the `refs/heads/` strip below, which would leave
    # `refs/tags/v1.2.3` intact and judge it as a branch name — that blocks the
    # release tag push wherever `gh` cannot answer, e.g. the dev container.
    case "$remote_ref" in
        refs/tags/*) continue ;;
    esac
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
    # mkdocs guard. `local_sha` is legitimately EMPTY on the first push of a
    # branch to an empty remote (the env channel exports no TO_REF there), and
    # no marker can ever exist for "" — so that row falls back to HEAD, which
    # is the best answer available and is what this gate has always done.
    pushed_shas+=("${local_sha:-$head_sha}")
done <<< "$_refs"

# Nothing being pushed (empty stdin, all deletes, all tags) — allow.
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
# ⛔ A separate flag, not `-z "$_missing_sha"`. An empty sha is a real row
# shape here (see the fallback above), and the empty-string sentinel made it
# indistinguishable from "nothing is missing" — so a row whose sha we could
# not determine would have been silently ALLOWED. Unknown must not mean OK.
_missing_found=0
_missing_sha=""
_missing_branch=""
for _i in "${!pushed_shas[@]}"; do
    if [ ! -f "$git_dir/$MARKER_PREFIX.${pushed_shas[$_i]}" ]; then
        _missing_found=1
        _missing_sha="${pushed_shas[$_i]}"
        _missing_branch="${pushed_branches[$_i]}"
        break
    fi
done

if [ "$_missing_found" = "0" ]; then
    # Every pushed commit has a marker — preflight passed for all of them.
    exit 0
fi

marker="$git_dir/$MARKER_PREFIX.$_missing_sha"
# ⛔ `git checkout <branch>` exits 128 when that branch is checked out in
# another worktree, which is the normal state here — an instruction that
# cannot reach green is a dead end, not a hint. Point at that worktree instead.
_other_wt=""
while read -r _wt_path _wt_rest; do
    case "$_wt_rest" in
        *"[$_missing_branch]"*) _other_wt="$_wt_path"; break ;;
    esac
done <<< "$(git worktree list 2>/dev/null)"

if [ "$_missing_sha" = "$head_sha" ]; then
    _checkout_hint="    make pr-preflight"
elif [ -n "$_other_wt" ]; then
    _checkout_hint="    cd ${_other_wt} && make pr-preflight"
else
    # ⛔ By SHA, not by branch name. `git push <old-sha>:refs/heads/x` and
    # `git push HEAD:refs/heads/other-name` both name a remote branch that
    # either does not exist locally or does not point at the commit being
    # pushed — so `git checkout <branch>` marks the wrong commit, or fails.
    _checkout_hint="    git checkout --detach ${_missing_sha} && make pr-preflight"
fi

# No marker — block with actionable instructions.
cat >&2 <<EOF

╔══════════════════════════════════════════════════════════════╗
║  ⛔ Push blocked — preflight not run on the pushed commit    ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Pushing: ${_missing_branch} -> ${_missing_sha}
║  HEAD:    ${head_sha}
║  Missing marker: $(basename "$marker")
║                                                              ║
║  ⛔ The marker names a COMMIT, not "now". If HEAD above is a
║  different commit, running preflight where you stand writes
║  the marker for THAT commit and this push stays blocked.
║                                                              ║
║  Run this before pushing:                                    ║
${_checkout_hint}
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
║  that block PR merges. See dev-rules #12.                    ║
╚══════════════════════════════════════════════════════════════╝

EOF
exit 1
