#!/usr/bin/env bash
# install_prepush_hook.sh — put this repo's pre-push guards on the push path.
#
# Usage:
#   bash scripts/ops/install_prepush_hook.sh
#
# ⛔ No `--check` here. "Are the guards on the push path?" is answered in ONE
#   place, `pr_preflight._prepush_guards_wired()`, in pure Python. It lived here
#   once and preflight shelled out; that gave WRONG verdicts, not loud ones —
#   `shutil.which("bash")` can resolve to WSL, and Git's `usr/bin/bash` under a
#   non-MSYS parent has no `grep`, which a `2>/dev/null` probe swallows. This
#   script is only ever run BY a shell, so it keeps to bash builtins plus
#   `mv`/`chmod`/`rm`/`mkdir` and says so, before changing anything, when one it
#   needs is missing.
#
# ⛔ NOTHING IS MOVED ASIDE TO BE RUN LATER (#2746). This repo has
#   `filter=lfs` paths and `git lfs install` is global, so a fresh clone
#   arrives with .git/hooks/pre-push owned by git-lfs. prepush_dispatch.sh runs
#   `git lfs pre-push` itself, so lfs's own hook is replaced, as pre-commit's
#   template is. Any other hook is refused, not moved: a slot for "whatever was
#   there" is where an edited shim, a pre-commit template and a directory each
#   landed, and each broke every push (#2745, #2746, #2702).
#
# ⛔ ONE WIRED STATE: the shim, byte for byte, at .git/hooks/pre-push.
#   `pre-commit install --hook-type pre-push` writes a template there and moves
#   what it found to pre-push.legacy. This repo's config declares no pre-push
#   stage, so that template does nothing but call pre-push.legacy: the
#   installer writes the shim over it and removes pre-push.legacy when it is
#   our shim, a guard copy, git-lfs's hook or a template; anything else there
#   is refused.
#
# ⛔ Every check runs before anything is changed, so a refusal leaves the hooks
#   directory as it was.
#
# ⚠️ It cannot stop a later `pre-commit install --hook-type pre-push` (with or
#   without -f) from taking the slot back. That is what preflight is for.
#
set -uo pipefail

say()  { printf '[install_prepush_hook] %s\n' "$1"; }
warn() { printf '[install_prepush_hook] %s\n' "$1" >&2; }

case "${1:-}" in
    "") ;;
    -h|--help)
        printf '%s\n' \
            "usage: bash scripts/ops/install_prepush_hook.sh" \
            "" \
            "Installs the pre-push guard shim over git-lfs's hook, pre-commit's template" \
            "or a copy of a guard; refuses any other hook already there, and refuses" \
            "while core.hooksPath is set." \
            "To ask whether the guards are wired, run: make pr-preflight" \
            "  (without make: python3 scripts/tools/dx/pr_preflight.py; on Windows, python)" >&2
        exit 0 ;;
    *)
        warn "unknown argument: $1 (there is no --check; use \`make pr-preflight\`, or without make \`python3 scripts/tools/dx/pr_preflight.py\` — on Windows, \`python\`)"
        exit 2 ;;
esac

root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    warn "⛔ not inside a git work tree"
    exit 2
}
# ⛔ Not while core.hooksPath is set, even to "" (#2696) — pre-commit refuses
# on the same test. It may name a directory many repositories share, where the
# shim would refuse every push of every one of them; "" makes git run no hook.
if hooks_path="$(git config --get core.hooksPath)"; then
    warn "⛔ refusing: core.hooksPath is set (to '$hooks_path'), so git does not look for"
    warn "   hooks in this repository's own hooks directory, where the guards go."
    warn "   Remove it (git config --show-origin --get core.hooksPath shows where it is"
    warn "   set; a global or system value is used by other repositories too, so think"
    warn "   of them first), then re-run."
    warn "   Nothing was changed."
    exit 1
fi
# ⛔ --git-path, not --git-dir: inside a worktree the git dir is
# .git/worktrees/<name> but the hooks live in the MAIN repo's .git/hooks.
hooks="$(git rev-parse --git-path hooks 2>/dev/null)" || {
    warn "⛔ cannot resolve the hooks directory"
    exit 2
}

hook="$hooks/pre-push"
legacy="$hooks/pre-push.legacy"
chained="$hooks/pre-push.chained"

# ⛔ `#!/usr/bin/env bash`, never an absolute interpreter path: when pre-commit
# owns the hook it resolves this shebang itself, and an absolute one is not a
# Windows path — measured, it aborts the whole push before ANY hook runs, which
# is exactly how git-lfs's `#!/bin/sh` hook breaks pushes under pre-commit.
#
# ⛔ The shim says something useful when the dispatcher is missing. It resolves
# it from the CURRENT work tree, and .git/hooks is shared by every worktree, so
# any tree checked out before #1689 lands has no dispatcher. Without the guard
# below the whole message is one line of `bash: …: No such file or directory`
# under three green `Passed` lines, and the three cheapest ways out of that
# picture each disarm the guards: --no-verify for that push, main included;
# deleting or hand-writing the hook for every worktree at once.
# ⛔ A quoted heredoc read into a variable by the `read` BUILTIN, then written
# with `printf`. Not `cat <<EOF` (needs `cat`, which is the dependency this file
# just removed) and not a printf with per-line escapes (the shim itself contains
# quotes, `$`, and continuations — building it out of escapes is how the escape
# gets eaten and the generated file becomes subtly wrong while still parsing).
IFS= read -r -d '' SHIM_BODY <<'VIBE_SHIM_EOF'
#!/usr/bin/env bash
# vibe-prepush-shim — generated by scripts/ops/install_prepush_hook.sh (#1689).
# ⛔ Do not edit here; edit scripts/ops/prepush_dispatch.sh. This file is
# rewritten by the installer and lives outside version control.
set -uo pipefail
_root="$(git rev-parse --show-toplevel 2>/dev/null)"
_dispatch="$_root/scripts/ops/prepush_dispatch.sh"
if [ ! -r "$_dispatch" ]; then
    {
        echo ""
        echo "[prepush] ⛔ No scripts/ops/prepush_dispatch.sh under the work tree git"
        echo "reports for this push, so the pre-push guards cannot run."
        echo "Work tree looked in: '$_root'"
        echo ""
        echo "If that is a checkout of this repo from before #1689: rebase / switch it"
        echo "to a commit that has the dispatcher. .git/hooks is shared by every"
        echo "worktree, so one old tree hits this while the others are fine."
        echo "If it is empty or not the top of a worktree (a bare repo, or"
        echo "git --git-dir=... run from elsewhere): push from inside a worktree of"
        echo "this repo, without --git-dir."
        echo ""
        echo "⛔ Re-running the installer does NOT fix this: the shim resolves the"
        echo "dispatcher from the tree you push FROM."
        echo ""
        echo "⛔ Do not reach for --no-verify: it skips every guard for that push,"
        echo "the direct-push-to-main one included. Do not delete .git/hooks/pre-push:"
        echo "that turns them off for EVERY worktree at once."
        echo ""
    } >&2
    exit 1
fi
exec bash "$_dispatch" "$@"
VIBE_SHIM_EOF

# ⛔ Identity by a WHOLE LINE equal to the generated header, never a substring:
# a user's hook that mentions the shim or pre-commit's `--hook-type=pre-push`
# in a comment is still a user's hook (#2617), and taken for ours or for
# pre-commit's it would be overwritten or deleted. Any line, not a fixed one,
# and a trailing CR dropped: on Windows pre-commit puts `#!/bin/sh` above its
# template and writes it CRLF. Bash builtins only — `read` is one, so these
# keep working when PATH carries nothing but the interpreter.
has_line() {   # $1 = file, $2 = the line
    [ -f "$1" ] || return 1
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        [ "${line%$'\r'}" = "$2" ] && return 0
    done < "$1"
    return 1
}
shim_header="${SHIM_BODY#*$'\n'}"
shim_header="${shim_header%%$'\n'*}"
is_ours()      { has_line "$1" "$shim_header"; }
is_precommit() { has_line "$1" "# File generated by pre-commit: https://pre-commit.com"; }
# git-lfs's own hook, recognised the way git-lfs recognises it: the whole file,
# surrounding whitespace trimmed, equal to one of the contents
# git-lfs writes or knows from its earlier versions. Copied from
# `hookBaseContent` and the pre-push upgradeables in git-lfs's lfs/hook.go; a
# version that writes something else is refused until it is added here. Exact
# contents, not a pattern: a hook that does anything more is someone's.
LFS_HOOKS=(
    $'#!/bin/sh\ncommand -v git-lfs >/dev/null 2>&1 || { printf >&2 "\\n%s\\n\\n" "This repository is configured for Git LFS but \'git-lfs\' was not found on your path. If you no longer wish to use Git LFS, remove this hook by deleting the \'pre-push\' file in the hooks directory (set by \'core.hookspath\'; usually \'.git/hooks\')."; exit 2; }\ngit lfs pre-push "$@"'
    $'#!/bin/sh\ngit lfs push --stdin $*'
    $'#!/bin/sh\ngit lfs push --stdin "$@"'
    $'#!/bin/sh\ngit lfs pre-push "$@"'
    $'#!/bin/sh\ncommand -v git-lfs >/dev/null 2>&1 || { echo >&2 "\\nThis repository has been set up with Git LFS but Git LFS is not installed.\\n"; exit 0; }\ngit lfs pre-push "$@"'
    $'#!/bin/sh\ncommand -v git-lfs >/dev/null 2>&1 || { echo >&2 "\\nThis repository has been set up with Git LFS but Git LFS is not installed.\\n"; exit 2; }\ngit lfs pre-push "$@"'
    $'#!/bin/sh\ncommand -v git-lfs >/dev/null 2>&1 || { echo >&2 "\\nThis repository is configured for Git LFS but \'git-lfs\' was not found on your path. If you no longer wish to use Git LFS, remove this hook by deleting the \'pre-push\' file in the hooks directory (set by \'core.hookspath\'; usually \'.git/hooks\').\\n"; exit 2; }\ngit lfs pre-push "$@"'
    $'#!/bin/sh\ncommand -v git-lfs >/dev/null 2>&1 || { echo >&2 "\\nThis repository is configured for Git LFS but \'git-lfs\' was not found on your path. If you no longer wish to use Git LFS, remove this hook by deleting \'.git/hooks/pre-push\'.\\n"; exit 2; }\ngit lfs pre-push "$@"'
    $'#!/bin/sh\ncommand -v git-lfs >/dev/null 2>&1 || { echo >&2 "\\nThis repository is configured for Git LFS but \'git-lfs\' was not found on your path. If you no longer wish to use Git LFS, remove this hook by deleting .git/hooks/pre-push.\\n"; exit 2; }\ngit lfs pre-push "$@"'
)
is_lfs() {
    [ -f "$1" ] || return 1
    local body known
    body="$(< "$1")" || return 1
    body="${body#"${body%%[![:space:]]*}"}"
    body="${body%"${body##*[![:space:]]}"}"
    for known in "${LFS_HOOKS[@]}"; do
        [ "$body" = "$known" ] && return 0
    done
    return 1
}
# ⛔ A hook byte-identical to a committed version of one of our guards is a
# single-file install from before #1689. It cannot run outside scripts/ops/,
# and the dispatcher already runs what it duplicates. Identity, not content: a
# hook of the user's own that happens to source the helper is still the
# user's. Removing an identical copy loses nothing, since git holds the same
# blob.
guard_blobs="$(git -C "$root" log --all --no-renames --format= --raw --no-abbrev -- \
    scripts/ops/protect_main_push.sh scripts/ops/require_preflight_pass.sh \
    scripts/ops/pre_push_mkdocs_strict.sh 2>/dev/null)"
is_guard_copy() {
    [ -f "$1" ] || return 1
    local id
    id="$(git hash-object -- "$1" 2>/dev/null)" || return 1
    case "$guard_blobs" in (*" $id "*) return 0 ;; esac
    return 1
}
# What the shim makes redundant, so it can go: our shim, a guard copy,
# git-lfs's hook (the dispatcher runs lfs itself) or a pre-commit template
# (this repo's config has no pre-push stage).
redundant() { is_ours "$1" || is_guard_copy "$1" || is_lfs "$1" || is_precommit "$1"; }
# Something that is there but is not a regular file: printed, rc 0. A symlink
# counts even when it points at a regular file, because writing the shim would
# go through it.
odd() {
    if [ -L "$1" ]; then
        if [ -e "$1" ]; then printf 'a symlink'; else printf 'a symlink to nothing'; fi
    elif [ -d "$1" ]; then printf 'a directory'
    elif [ -e "$1" ] && [ ! -f "$1" ]; then printf 'not a regular file'
    else return 1
    fi
}
refuse() {   # $1 = what is wrong; the rest, one line each, what to do
    warn "⛔ refusing: $1"
    shift
    local l
    for l in "$@"; do warn "   $l"; done
    warn "   Nothing was changed."
    exit 1
}
need() {   # $1 = tool, $2 = what it is for
    command -v "$1" >/dev/null 2>&1 ||
        refuse "\`$1\` is not on PATH, and it is needed to $2." "Put it on PATH, then re-run."
}

# --- Checks. Nothing below changes anything until they have all passed. ------

# Earlier versions of this installer moved the hook they found to
# pre-push.chained and the dispatcher ran it. It runs nothing now, and refuses
# every push while the file is there, so it has to go.
drop_chained=""
if what="$(odd "$chained")"; then
    refuse "$chained is $what." "Look at what it is and move it away, then re-run."
elif [ -e "$chained" ]; then
    redundant "$chained" ||
        refuse "$chained is a hook an earlier version of this installer moved there," \
            "and the dispatcher no longer runs it. Fold what it does into your own" \
            "workflow or delete it, then re-run."
    drop_chained=1
fi

mode=""          # new | refresh | overwrite | swap
replaced=""
drop_legacy=""
if is_guard_copy "$hook"; then
    # Before odd(): a symlink or hard link to a guard in scripts/ops is one.
    mode=swap
    replaced=" (replacing a copy of a guard that was there)"
elif what="$(odd "$hook")"; then
    refuse "$hook is $what." "Look at what it is and move it away, then re-run."
elif [ ! -e "$hook" ]; then
    mode=new
elif is_ours "$hook"; then
    mode=refresh
elif is_lfs "$hook"; then
    mode=overwrite
    replaced=" (replacing git-lfs's hook; the dispatcher runs git lfs pre-push itself)"
elif is_precommit "$hook"; then
    # pre-push.legacy is what the template calls. Once the template is gone
    # nothing calls it, so it may only be something the shim makes redundant.
    mode=overwrite
    replaced=" (replacing pre-commit's template)"
    if what="$(odd "$legacy")"; then
        refuse "$legacy, which pre-commit's template at $hook runs, is $what." \
            "Look at what it is and move it away, then re-run."
    elif [ -e "$legacy" ]; then
        redundant "$legacy" ||
            refuse "$legacy is a hook that pre-commit's template at $hook runs." \
                "Replacing the template would stop it running. Fold what it does into" \
                "your own workflow or delete it, then re-run."
        drop_legacy=1
    fi
else
    refuse "$hook is not the guard shim, git-lfs's hook or pre-commit's template, and no" \
        "version of a guard in this clone's history matches it." \
        "Installing the shim would stop it running: the dispatcher runs this" \
        "repo's guards and git-lfs, nothing else. Fold what it does into your own" \
        "workflow or delete it, then re-run."
fi

if [ -n "$drop_chained$drop_legacy" ]; then need rm "remove a hook the shim makes redundant"; fi
if [ ! -d "$hooks" ]; then need mkdir "create $hooks"; fi
# ⛔ git SILENTLY IGNORES a hook without the executable bit — it prints one
# `hint:` line that `advice.ignoredHook=false` turns off. A file written over an
# executable one keeps its bit; a new one needs `chmod`.
if [ "$mode" = new ] || [ "$mode" = swap ] || [ ! -x "$hook" ]; then
    need chmod "make the shim executable (git ignores a hook without the bit)"
fi
if [ "$mode" = swap ]; then need mv "replace the guard copy without writing through it"; fi

# --- Changes. ---------------------------------------------------------------

if [ -n "$drop_chained" ]; then
    rm -f "$chained" || { warn "⛔ could not remove $chained"; exit 1; }
    say "removed $chained: the shim makes it redundant, and the dispatcher no longer runs it"
fi

if [ ! -d "$hooks" ]; then
    mkdir -p "$hooks" || exit 1
fi

# ⛔ A guard copy is replaced by `mv`, never written into: a hook symlinked or
# hard-linked to a guard in scripts/ops is identical to it, and `>` would go
# through the link into the version-controlled guard. Until the `mv`, the old
# hook stays as it was.
out="$hook"
if [ "$mode" = swap ]; then out="$hook.vibe-new"; fi
printf '%s' "$SHIM_BODY" > "$out" || { warn "⛔ could not write $out"; exit 1; }

# ⛔ Not `|| true`: a swallowed failure leaves a hook file that looks installed
# and never runs. Measured: with the bit cleared, a direct push to main
# succeeded with the guard banner absent.
if [ ! -x "$out" ] && ! chmod +x "$out"; then
    warn "⛔ could not make $out executable. git ignores non-executable hooks"
    warn "   with only a hint, so the guards would look installed and never run."
    exit 1
fi
if [ "$out" != "$hook" ]; then
    mv -f "$out" "$hook" || {
        warn "⛔ could not move $out over $hook, a copy of a pre-push guard."
        warn "   $hook is unchanged. Delete it by hand (git holds the same content),"
        warn "   then re-run."
        exit 1
    }
fi

if [ -n "$drop_legacy" ]; then
    if rm -f "$legacy"; then
        say "removed $legacy: the shim makes it redundant, and nothing calls it now"
    else
        warn "could not remove $legacy; the shim makes it redundant, and nothing calls it now"
    fi
fi
# ⛔ Last line of stdout: session-init.py reports the guards as not installed
# unless it starts with "[install_prepush_hook] installed".
say "installed guard shim at $hook$replaced"
exit 0
