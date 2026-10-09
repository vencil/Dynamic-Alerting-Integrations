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
#   This hook closes the gap by running mkdocs strict at pre-push time.
#
# Triggers (pre-push only) — read from the PUSHED REFSPEC (#1690): the tip of
#   every pushed branch is built. Deletions and non-branch refs are not.
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
#   pre-commit reads git's stdin before its hooks run, so a copy it runs sees
#   nothing being pushed and builds nothing.

set -uo pipefail

# ⛔ Pure parameter expansion, no `$(dirname …)` — sourcing _prepush_refs.sh
# puts this file under that helper's PATH-stripped contract. Rationale there.
_prepush_dir="${BASH_SOURCE[0]%/*}"
[ "$_prepush_dir" = "${BASH_SOURCE[0]}" ] && _prepush_dir="."

# --- Escape hatch ------------------------------------------------------------
if [ "${MKDOCS_STRICT_BYPASS:-0}" = "1" ]; then
    echo "[pre-push-mkdocs] MKDOCS_STRICT_BYPASS=1 set; skipping mkdocs strict check"
    exit 0
fi

# --- What is this push actually updating? (#1690) ----------------------------
# shellcheck source=scripts/ops/_prepush_refs.sh
if [ ! -r "$_prepush_dir/_prepush_refs.sh" ]; then
    echo "[pre-push-mkdocs] ⛔ _prepush_refs.sh is not next to ${BASH_SOURCE[0]}" >&2
    echo "  If that is in scripts/ops/, the helper is gone from your checkout. It is" >&2
    echo "  version-controlled: restore it from HEAD (this works when the deletion is" >&2
    echo "  staged too):" >&2
    echo "    git checkout HEAD -- scripts/ops/_prepush_refs.sh" >&2
    echo "  The installer cannot restore it." >&2
    exit 1
fi
. "$_prepush_dir/_prepush_refs.sh"

_refs="$(prepush_refs)"

_Z40="0000000000000000000000000000000000000000"

# ⛔ The tip of every pushed branch is built; there is no "did docs change?" test.
# Such a test needs a list of paths that matches everything the build reads —
# rule-packs/, the hooks under scripts/mkdocs/, the slug function, the strict
# check and its ledger — and a second list kept in step by hand drifts.
_build_shas=()
while read -r remote_ref local_sha; do
    [ -n "${remote_ref:-}" ] || continue
    # Deletions carry no tree to build, and only branches are built: a note
    # has no site at all.
    [ "${local_sha:-}" = "$_Z40" ] && continue
    case "$remote_ref" in refs/heads/*) ;; *) continue ;; esac
    _build_shas+=("$local_sha")
done <<< "$_refs"

if [ "${#_build_shas[@]}" -eq 0 ]; then
    exit 0
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
