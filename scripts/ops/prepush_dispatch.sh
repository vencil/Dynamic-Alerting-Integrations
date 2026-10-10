#!/usr/bin/env bash
# prepush_dispatch.sh — the single pre-push entry point, and the only thing in
# this repo that reads git's pre-push stdin. #1689 has the measurements.
#
# ⛔ Not run directly. Reached through the shim install_prepush_hook.sh writes
#   to .git/hooks/pre-push.
#
# ⛔ SHEBANG: `#!/usr/bin/env bash`, never an absolute one. When pre-commit owns
#   the hook it resolves this shebang itself, and `/bin/sh` is not a Windows
#   path — measured: the push dies before ANY guard runs.

set -uo pipefail

# Earlier installers moved the hook they found to pre-push.chained and this
# file ran it. Nothing runs it now (git-lfs is run directly, below), so a hook
# left there would stop running without a word: refuse until the installer has
# dealt with it.
_hooks_dir="$(git rev-parse --git-path hooks 2>/dev/null)"
if [ -n "$_hooks_dir" ] && { [ -e "$_hooks_dir/pre-push.chained" ] || [ -L "$_hooks_dir/pre-push.chained" ]; }; then
    printf '%s\n' "" \
        "[prepush_dispatch] ⛔ $_hooks_dir/pre-push.chained is still there. An earlier" \
        "installer moved a hook to it, and nothing runs it any more. Re-run:" \
        "    bash scripts/ops/install_prepush_hook.sh" \
        "It removes what the shim makes redundant (git-lfs's hook, a copy of ours," \
        "pre-commit's template) and says what to do with anything else." \
        "" >&2
    exit 1
fi

_dispatch_dir="${BASH_SOURCE[0]%/*}"
[ "$_dispatch_dir" = "${BASH_SOURCE[0]}" ] && _dispatch_dir="."

# ⛔ Pure parameter expansion above, no `$(dirname …)`. Rationale in
# _prepush_refs.sh.

GUARDS=(
    protect_main_push.sh
    require_preflight_pass.sh
    pre_push_mkdocs_strict.sh
)

# Guards that run only when the push carries commits. ⛔ Do NOT add the other
# two: `git push origin :main` must stay judged (#1691). mkdocs belongs here
# because a deletion carries no tree to build — and since #1690 it drops
# deletion rows itself, so this entry is a cheap short-circuit, not the
# mechanism.
GUARDS_NEEDING_COMMITS=(
    pre_push_mkdocs_strict.sh
)

_Z40="0000000000000000000000000000000000000000"

# ⛔ Read stdin ONCE, then hand every guard its own copy. Each guard reads the
# refspec itself, so chaining them lets the first drain the pipe and leaves
# every later guard with EOF — the #1664 picture, relocated.
# ⛔ The `read` builtin, not `$(cat)`: without `cat` on PATH that read nothing,
# every guard saw zero rows and allowed the push (#2765). Trailing newlines are
# dropped, as `$(cat)` dropped them, so the guards get the same bytes.
IFS= read -r -d '' _refs || true
_refs="${_refs%"${_refs##*[!$'\n']}"}"

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
        printf '%s\n' "" \
            "[prepush_dispatch] ⛔ $_guard is missing from $_dispatch_dir, so one of the" \
            "pre-push guards cannot run. Stopping here rather than running the rest." \
            "" \
            "It is version-controlled: restore it from HEAD (the deletion may already be" \
            "staged)." \
            "" \
            "⛔ Not the installer: it writes .git/hooks, never scripts/ops, so it exits 0" \
            "and changes nothing here. And do not reach for --no-verify or delete" \
            ".git/hooks/pre-push — both turn off the direct-push-to-main guard for good," \
            "which is what #1664 fixed." \
            "" >&2
        exit 1
    fi
    bash "$_path" "$@" < <(_feed)
    _guard_rc=$?
    if [ "$_guard_rc" -ne 0 ]; then
        _rc="$_guard_rc"
    fi
done

# git-lfs: on a fresh clone of this repo its own pre-push hook held the slot
# the installer took (the repo has `filter=lfs` paths and `git lfs install` is
# global), so its work is done here. ⛔ Keyed on LFS being configured, not on
# git-lfs being on PATH: skipping when the binary is missing reports a pushed
# branch whose LFS objects never left the machine — lfs's own hook refuses
# then, and so does this.
if git config --get-regexp '^filter\.lfs\.' >/dev/null 2>&1; then
    if command -v git-lfs >/dev/null 2>&1; then
        git lfs pre-push "$@" < <(_feed)
        _lfs_rc=$?
        if [ "$_lfs_rc" -ne 0 ]; then
            _rc="$_lfs_rc"
        fi
    else
        printf '%s\n' "" \
            "[prepush_dispatch] ⛔ Git LFS is configured (filter.lfs.* in git config) but" \
            "git-lfs is not on PATH, so LFS objects in this push would not be uploaded." \
            "Install git-lfs. If you no longer use it, remove the filter.lfs section from" \
            "every config that sets it; this lists them:" \
            "    git config --show-origin --get-regexp '^filter\\.lfs\\.'" \
            "" >&2
        _rc=1
    fi
fi

exit "$_rc"
