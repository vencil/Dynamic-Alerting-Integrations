#!/usr/bin/env bash
# prepush_dispatch.sh — the single pre-push entry point, and the only thing in
# this repo that reads git's pre-push stdin. #1689 has the measurements.
#
# ⛔ Not run directly. Reached through .git/hooks/pre-push (or pre-push.legacy
#   when pre-commit owns that file); both are written by install_prepush_hook.sh.
#
# ⛔ SHEBANG: `#!/usr/bin/env bash`, never an absolute one. When pre-commit owns
#   the hook it resolves this shebang itself, and `/bin/sh` is not a Windows
#   path — measured: the push dies before ANY guard runs.

set -uo pipefail

_dispatch_dir="${BASH_SOURCE[0]%/*}"
[ "$_dispatch_dir" = "${BASH_SOURCE[0]}" ] && _dispatch_dir="."

# ⛔ Pure parameter expansion, no `$(dirname …)`. require_preflight_pass.sh's
# test_gh_missing_* strips PATH down to bash/git/basename/sh/cat to prove the
# gate still works without `gh`, and `dirname` is not in that set.

# ⛔ A PIN, NOT A SCAN: "this repo's pre-push guards are exactly these three".
# Do not replace it with a glob of scripts/ops/*.sh.
GUARDS=(
    protect_main_push.sh
    require_preflight_pass.sh
    pre_push_mkdocs_strict.sh
)

# Guards that run only when the push carries commits. ⛔ Do NOT add the other
# two: `git push origin :main` must stay judged (#1691). mkdocs belongs here
# because it does not read the refspec at all (it diffs `@{u}...HEAD`, #1690),
# so on a deletion it judges something the push is not doing. This restores the
# pre-#1689 behaviour — `_pre_push_ns` returns None for deletions and for
# up-to-date pushes, and pre-commit then ran no pre-push hooks at all.
GUARDS_NEEDING_COMMITS=(
    pre_push_mkdocs_strict.sh
)

_Z40="0000000000000000000000000000000000000000"

# Whatever owned .git/hooks/pre-push before the installer took the slot. On a
# fresh clone of this repo that is git-lfs's hook (the repo has `filter=lfs`
# paths and `git lfs install` is global), and it has real work to do.
_CHAINED_NAME="pre-push.chained"

# ⛔ Read stdin ONCE, then hand every guard its own copy. Each guard reads the
# refspec itself, so chaining them lets the first drain the pipe and leaves
# every later guard with EOF — the #1664 picture, relocated.
_refs="$(cat)"

_feed() {
    if [ -n "$_refs" ]; then
        printf '%s\n' "$_refs"
    fi
}

# Does this push carry any commits, or is it only deletions / nothing at all?
# Rows are git's own protocol: <local_ref> <local_sha> <remote_ref> <remote_sha>.
_pushes_commits=0
while read -r _lref _lsha _rref _rsha; do
    [ -n "${_rref:-}" ] || continue
    [ "$_lsha" = "$_Z40" ] && continue
    _pushes_commits=1
    break
done < <(_feed)

_needs_commits() {
    local _g
    for _g in "${GUARDS_NEEDING_COMMITS[@]}"; do
        [ "$_g" = "$1" ] && return 0
    done
    return 1
}

# ⛔ Run every guard; do not short-circuit on the first failure, and do not put
# a guard on the right-hand side of a pipe — process substitution keeps the exit
# status unambiguously the guard's own.
_rc=0
for _guard in "${GUARDS[@]}"; do
    if _needs_commits "$_guard" && [ "$_pushes_commits" = "0" ]; then
        continue
    fi
    _path="$_dispatch_dir/$_guard"
    if [ ! -r "$_path" ]; then
        cat >&2 <<GUARD_MISSING

[prepush_dispatch] ⛔ $_guard is missing next to this script, so one of the
pre-push guards cannot run. Stopping here rather than running the rest.

The guards are version-controlled files, so restore them from git:
    git checkout -- scripts/ops/

⛔ Not the installer: it writes .git/hooks, never scripts/ops, so it exits 0
and changes nothing here. And do not reach for --no-verify or delete
.git/hooks/pre-push — both turn off the direct-push-to-main guard for good,
which is what #1664 fixed.

GUARD_MISSING
        exit 1
    fi
    bash "$_path" "$@" < <(_feed)
    _guard_rc=$?
    if [ "$_guard_rc" -ne 0 ]; then
        _rc="$_guard_rc"
    fi
done

# ⛔ Invoke the chained hook DIRECTLY — never `bash "$hook"`. It is not
# necessarily a shell script, and bash ignores its shebang: measured, a python
# hook gives `import: command not found`, rc=2.
_hooks_dir="$(git rev-parse --git-path hooks 2>/dev/null)"
if [ -n "${_hooks_dir:-}" ] && [ -x "$_hooks_dir/$_CHAINED_NAME" ]; then
    "$_hooks_dir/$_CHAINED_NAME" "$@" < <(_feed)
    _chained_rc=$?
    if [ "$_chained_rc" -ne 0 ]; then
        _rc="$_chained_rc"
    fi
fi

exit "$_rc"
