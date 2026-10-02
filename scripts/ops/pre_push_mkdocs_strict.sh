#!/usr/bin/env bash
# pre_push_mkdocs_strict.sh — Pre-push gate for mkdocs strict-build validation.
#
# Purpose:
#   Pre-commit `check_doc_links.py` validates links via filesystem semantics
#   (resolves `../foo.md` relative to the .md file's parent dir on disk).
#   MkDocs strict mode validates via SITE-ROOT semantics (`docs/` is root;
#   `../X.md` from `docs/internal/Y.md` jumps OUT of site → strict fails).
#
#   The two validators have different (both correct) semantic models. Only
#   one runs locally during pre-commit; the other fires only in CI. Issue
#   #412 documents the pattern and its recurrence.
#
#   This hook closes the gap by running mkdocs strict at pre-push time
#   when docs changed.
#
# Triggers (pre-push only) — read from the PUSHED REFSPEC (#1690). ⛔ Which
#   paths count as docs is `DOC_RE` below and nowhere else: a second spelling
#   of it in prose drifts. A ref whose base cannot be determined is built
#   regardless; tags and deletions never are.
#
# Tiered execution:
#   Tier 1 — Native `mkdocs` on PATH: run directly
#   Tier 2 — Not available: WARN + don't block (CI is backstop; we don't
#            punish devs who lack mkdocs locally for legitimate reasons)
#
# Why no docker-exec / dev container fallback:
#   Considered but rejected. Dev containers mount a stale main repo clone
#   (`/workspaces/vibe-k8s-lab` is updated on container start, not on every
#   command). Running mkdocs strict against that stale state gives false
#   positives from pre-existing warnings the current worktree may have
#   already fixed. Cleaner to require local pip install and use Tier 2
#   warn-only path otherwise.
#
# Escape hatch:
#   MKDOCS_STRICT_BYPASS=1 git push  (skips this hook entirely)
#
# Configuration:
#   ⛔ NOT a pre-commit hook (#1689). It is run by
#   scripts/ops/prepush_dispatch.sh; install with
#       bash scripts/ops/install_prepush_hook.sh
#   ⛔ Do not put a `stages: [pre-push]` entry back in .pre-commit-config.yaml.
#   A hook that pre-commit runs is handed at most ONE refspec, so a push of
#   several refs would be judged on one of them.

set -uo pipefail

# ⛔ Pure parameter expansion, no `$(dirname …)` — sourcing _prepush_refs.sh
# puts this file under that helper's PATH-stripped contract. Rationale there.
_prepush_dir="${BASH_SOURCE[0]%/*}"
[ "$_prepush_dir" = "${BASH_SOURCE[0]}" ] && _prepush_dir="."
# ⛔ Absolute BEFORE the cd below, or a relative invocation from a subdirectory
# reports the helper missing while it is sitting right there.
case "$_prepush_dir" in /* | ?:[/\\]*) ;; *) _prepush_dir="$PWD/$_prepush_dir" ;; esac
# ⛔ Keep REPO_ROOT on its own line, without naming `_prepush_dir`: the
# sourcing-form test refuses a `$(` on any line that mentions that variable.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" || exit 1

# --- Escape hatch ------------------------------------------------------------
if [ "${MKDOCS_STRICT_BYPASS:-0}" = "1" ]; then
    echo "[pre-push-mkdocs] MKDOCS_STRICT_BYPASS=1 set; skipping mkdocs strict check"
    exit 0
fi

# --- What is this push actually updating? (#1690) ----------------------------
# ⛔ BOTH the trigger and the subject come from the pushed refspec. Changing
# only the trigger is worse than changing neither: the guard then fires more
# often and still builds the tree you are standing in.
# shellcheck source=scripts/ops/_prepush_refs.sh
if [ ! -r "$_prepush_dir/_prepush_refs.sh" ]; then
    echo "[pre-push-mkdocs] ⛔ _prepush_refs.sh is not next to ${BASH_SOURCE[0]}" >&2
    echo "  If that is in scripts/ops/, the helper is gone from your checkout. It is" >&2
    echo "  version-controlled: restore it from HEAD (the deletion may already be staged)." >&2
    echo "  Anywhere else it is a stale copy of this guard, and the installer replaces it:" >&2
    echo "    bash scripts/ops/install_prepush_hook.sh" >&2
    exit 1
fi
. "$_prepush_dir/_prepush_refs.sh"

if ! _refs="$(prepush_refs_full)"; then
    prepush_refs_unavailable_message >&2
    exit 1
fi

_Z40="0000000000000000000000000000000000000000"
DOC_RE='^(docs/.*\.(md|jsx|html)$|mkdocs\.yml$|README\.md$|README\.en\.md$|CHANGELOG\.md$)'

# git hands a pre-push hook the remote's name as $1. The dispatcher passes it
# through, so a clone whose remote is not called `origin` still gets a base.
_remote_name="${1:-}"

# Rows: <remote_ref> <local_sha> <remote_sha>; `-` means "not known".
_build_shas=()
_doc_changes=""
_unknown_base=""
while read -r remote_ref local_sha remote_sha; do
    [ -n "${remote_ref:-}" ] || continue
    # Deletions carry no tree to build.
    [ "$local_sha" = "$_Z40" ] && continue
    case "$remote_ref" in refs/tags/*) continue ;; esac
    # ⛔ `-` is an unknown commit, not "nothing to push" — and `git worktree
    # add` reads `-` as the previous branch, so building it would validate the
    # wrong tree.
    if [ "$local_sha" = "-" ]; then
        echo "[pre-push-mkdocs] ⛔ cannot tell which commit ${remote_ref} pushes; refusing." >&2
        exit 1
    fi

    # Base for "what does THIS push introduce?".
    # ⛔ Unknown must mean BUILD, never skip.
    _base=""
    if [ "$remote_sha" != "-" ] && [ "$remote_sha" != "$_Z40" ] \
       && git cat-file -e "${remote_sha}^{commit}" 2>/dev/null; then
        _base="$remote_sha"
    else
        for _cand in ${_remote_name:+"$_remote_name/main"} origin/main; do
            if git rev-parse --verify --quiet "${_cand}^{commit}" >/dev/null 2>&1; then
                _base="$_cand"
                break
            fi
        done
    fi

    # ⛔ Diff from the MERGE BASE, never `git diff A B` — that is two-way, so a
    # branch merely BEHIND the base reports the base's own files as changed by
    # this push. With no merge base at all (orphan branch)
    # this is the unknown case, which must build.
    _mb=""
    [ -n "$_base" ] && _mb=$(git merge-base "$_base" "$local_sha" 2>/dev/null || true)

    # ⛔ No --diff-filter: every change status counts, deletions included.
    # Deleting a doc is precisely what breaks mkdocs strict (dangling nav
    # entries, cross-refs to the gone file), and an enumerated list silently
    # drops whatever it forgets (#2195).
    # ⛔ -z: without it git C-quotes a non-ASCII path ("docs/\346…") and the
    # leading quote defeats DOC_RE (#2195). A newline inside a file name
    # still splits.
    # ⛔ A failed diff is the unknown case, never "no doc changes" (#2195).
    # It reaches the else branch only through `set -o pipefail` above;
    # without it the pipeline's status is tr's, and the failure is lost.
    if [ -n "$_mb" ] && _changed=$(git diff --name-only -z "$_mb" "$local_sha" | tr '\0' '\n'); then
        _hit=$(printf '%s\n' "$_changed" | grep -E "$DOC_RE" || true)
        if [ -n "$_hit" ]; then
            _doc_changes="${_doc_changes}${_hit}"$'\n'
            _build_shas+=("$local_sha")
        fi
    else
        # ⛔ Fail-safe, not fail-open: with no base, or a diff that failed, we
        # cannot tell, so we build.
        # ⛔ Reported separately: filing it under "doc changes detected" tells a
        # contributor pushing pure code that they changed docs.
        _unknown_base="${_unknown_base}${remote_ref}"$'\n'
        _build_shas+=("$local_sha")
    fi
done <<< "$_refs"

if [ "${#_build_shas[@]}" -eq 0 ]; then
    # Non-doc push; nothing to check.
    exit 0
fi

if [ -n "$_doc_changes" ]; then
    echo "[pre-push-mkdocs] Doc changes detected in this push:"
    printf '%s\n' "$_doc_changes" | grep -v '^[[:space:]]*$' | sed 's/^/  • /'
    echo ""
fi
if [ -n "$_unknown_base" ]; then
    echo "[pre-push-mkdocs] Cannot tell what these refs introduce:"
    printf '%s\n' "$_unknown_base" | grep -v '^[[:space:]]*$' | sed 's/^/  • /'
    echo ""
fi

# --- Build the PUSHED tree, not the working tree (#1690) ---------------------
# ⛔ `git worktree add`, NOT `git archive | tar -x`. A worktree checkout obeys
# core.symlinks, so this repo's mode-120000 aliases (docs/CHANGELOG.md, …)
# materialise exactly as they do in the contributor's checkout and in CI.
# `tar -x` does not read core.symlinks, so on a Windows checkout — where they
# are path stubs — the extracted tree matches neither: the platform dependence
# these aliases exist to remove, reintroduced by the build step.
# ⛔ The clean-up is a trap, because one that runs only after the build is
# skipped on Ctrl-C or SIGTERM (#2169); and an EXIT trap, not INT/TERM alone,
# because a closed terminal (SIGHUP) ends the guard too.
# ⛔ `add` and the build run in the background, and the trap waits for the
# running one before removing the tree: a signal to this bash alone reaches
# neither. An `add` still checking out holds the tree locked, so `remove`
# refuses and the `add` finishes afterwards, registered; mkdocs (a grandchild)
# writes site/ back into .git, unregistered (#2211). Killing their pids does
# not reach mkdocs. Signals are ignored while it waits: an impatient second
# Ctrl-C would otherwise end the trap before the tree is removed.
_live_wt=""
_bg_pid=""
trap 'trap "" INT TERM HUP
wait "$_bg_pid" 2>/dev/null
git worktree remove --force "$_live_wt" >/dev/null 2>&1' EXIT
_build_one() {
    local _sha="$1" _wt _rc
    _wt="$(git rev-parse --git-path "mkdocs-strict-$$-${_sha:0:8}")"
    _live_wt="$_wt"
    # ⛔ git's own stderr is the diagnosis; no guessed causes (#2210).
    # A bare `&` is enough here, unlike the build below: git sets its own
    # SIGINT handler, so Ctrl-C still stops the checkout.
    # ⛔ The user's hooks stay out of this tree: CI runs none, and a failing
    # post-checkout hook would be reported as a failed checkout.
    git -c core.hooksPath=/dev/null worktree add --detach --quiet "$_wt" "$_sha" &
    _bg_pid=$!
    if ! wait "$_bg_pid"; then
        # ⛔ FAIL CLOSED. The obvious fallback — build the working tree instead —
        # is EXACTLY the #1690 defect this guard exists to remove: it returns
        # the wrong tree's exit status as the verdict, so a clean working tree
        # turns a broken pushed commit green.
        cat >&2 <<WORKTREE_FAILED

[pre-push-mkdocs] ⛔ could not check out $_sha to validate it.

This guard builds the commit you are PUSHING, not the tree you are standing
in, so it cannot fall back to the working tree — that would report on the
wrong commit. Refusing instead.

To push anyway (the docs build then runs only in CI):
    MKDOCS_STRICT_BYPASS=1 git push ...

WORKTREE_FAILED
        return 1
    fi
    # ⛔ A subshell, not a bare command: bash starts a bare `cmd &` with SIGINT
    # ignored, and Ctrl-C would then wait out the whole build.
    ( cd "$_wt" && bash scripts/tools/lint/mkdocs_strict_check.sh ) &
    _bg_pid=$!
    wait "$_bg_pid"
    _rc=$?
    git worktree remove --force "$_wt" >/dev/null 2>&1
    return "$_rc"
}

# --- Tiered execution --------------------------------------------------------
# Tier 1: native mkdocs
if command -v mkdocs >/dev/null 2>&1; then
    echo "[pre-push-mkdocs] Using native mkdocs ($(mkdocs --version 2>&1 | head -1))"
    # ⛔ Every non-zero lands here — broken links, a failed checkout, an aborted
    # build — so this line names the commit, gives no advice, and stops (#2210).
    for _sha in "${_build_shas[@]}"; do
        echo "[pre-push-mkdocs] validating pushed commit ${_sha:0:8}"
        if ! _build_one "$_sha"; then
            echo ""
            echo "::error::mkdocs strict did not pass for ${_sha:0:8}"
            exit 1
        fi
    done
    echo "[pre-push-mkdocs] ✅ mkdocs strict PASS"
    exit 0
fi

# Tier 2: no native mkdocs; soft fail
echo "[pre-push-mkdocs] ⚠️  mkdocs not on PATH; cannot validate locally."
echo ""
echo "  To enable local pre-push validation (one-time install):"
echo "    pip install --user mkdocs-material mkdocs-static-i18n pymdown-extensions"
echo ""
echo "  Continuing push; CI will catch any mkdocs strict failures."
exit 0
