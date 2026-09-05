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
#   `mv`/`chmod` and says so when those are missing.
#
# ⛔ IT CHAINS WHAT WAS ALREADY THERE — NOT A CONVENIENCE. This repo has
#   `filter=lfs` paths and `git lfs install` is global, so EVERY FRESH CLONE
#   arrives with .git/hooks/pre-push owned by git-lfs. Refusing it made the
#   shipped remedy a dead end for every new clone; `pre-commit install
#   --hook-type pre-push` instead migrates lfs's `#!/bin/sh` hook and then every
#   push dies on `/bin/sh not found`. The foreign hook is moved to
#   pre-push.chained and run by prepush_dispatch.sh with the same argv and stdin.
#
# ⛔ MIGRATION-AWARE: pre-commit writes this file too, and BOTH orders happen.
#   Installer first -> pre-commit migrates our shim to pre-push.legacy and calls
#   it with the full stdin. pre-commit first -> the shim goes to pre-push.legacy
#   and we do NOT touch pre-commit's file (that would drop every pre-commit-stage
#   hook). Never blindly overwrite: an unconditional write was measured faithful
#   in only one of the two orders.
#
# ⚠️ It cannot stop `pre-commit install -f --hook-type pre-push`, which DELETES
#   pre-push.legacy silently (rc=0, no mention). That is what preflight is for.
#
# Measurements behind all of the above: #1689 and this commit's message.

set -uo pipefail

MARKER="vibe-prepush-shim"
CHAINED_NAME="pre-push.chained"

say()  { printf '[install_prepush_hook] %s\n' "$1"; }
warn() { printf '[install_prepush_hook] %s\n' "$1" >&2; }

case "${1:-}" in
    "") ;;
    -h|--help)
        printf '%s\n' \
            "usage: bash scripts/ops/install_prepush_hook.sh" \
            "" \
            "Installs the pre-push guard shim, chaining any hook already there." \
            "To ask whether the guards are wired, run: make pr-preflight" >&2
        exit 0 ;;
    *)
        warn "unknown argument: $1 (there is no --check; use \`make pr-preflight\`)"
        exit 2 ;;
esac

root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    warn "⛔ not inside a git work tree"
    exit 2
}
# ⛔ --git-path, not --git-dir: inside a worktree the git dir is
# .git/worktrees/<name> but the hooks live in the MAIN repo's .git/hooks. It
# also follows core.hooksPath, which this repo sets.
hooks="$(git rev-parse --git-path hooks 2>/dev/null)" || {
    warn "⛔ cannot resolve the hooks directory"
    exit 2
}

hook="$hooks/pre-push"
legacy="$hooks/pre-push.legacy"
chained="$hooks/$CHAINED_NAME"

# ⛔ Bash builtins only — no `grep`. `$(<file)` is a redirection, not a command,
# so these keep working when PATH carries nothing but the interpreter.
contains() {   # $1 = file, $2 = needle
    [ -f "$1" ] || return 1
    local body
    body="$(<"$1")" || return 1
    case "$body" in (*"$2"*) return 0 ;; esac
    return 1
}
is_ours()      { contains "$1" "$MARKER"; }
is_precommit() { contains "$1" "--hook-type=pre-push"; }

# Move a foreign hook into the chained slot. Fails loudly: a silent failure here
# means either that hook stops running or ours never installs.
stash_foreign() {   # $1 = path to the foreign hook
    if [ -e "$chained" ]; then
        warn "⛔ refusing: $1 is not ours and $chained is already occupied."
        warn "   Two different hooks want the chained slot. Resolve by hand:"
        warn "   inspect both, keep one at $chained, then re-run."
        return 1
    fi
    command -v mv >/dev/null 2>&1 || {
        warn "⛔ \`mv\` is not on PATH, so the existing hook at $1 cannot be"
        warn "   moved aside. Refusing rather than deleting it."
        return 1
    }
    mv "$1" "$chained" || {
        warn "⛔ could not move $1 to $chained"
        return 1
    }
    say "moved the existing pre-push hook to $CHAINED_NAME; the dispatcher will keep running it"
    return 0
}

target="$hook"
if is_ours "$hook"; then
    target="$hook"                       # refresh in place
elif is_precommit "$hook"; then
    # pre-commit keeps the hook file; we take the slot it calls with the full
    # stdin. If something else is already in that slot it is a real hook that
    # pre-commit migrated — chain it rather than destroy it.
    if [ -e "$legacy" ] && ! is_ours "$legacy"; then
        stash_foreign "$legacy" || exit 1
    fi
    target="$legacy"
elif [ -e "$hook" ]; then
    stash_foreign "$hook" || exit 1
fi

if [ ! -d "$hooks" ]; then
    command -v mkdir >/dev/null 2>&1 || { warn "⛔ no \`mkdir\` and $hooks does not exist"; exit 1; }
    mkdir -p "$hooks" || exit 1
fi

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
# picture (--no-verify, delete the hook, hand-write one) each disarm the guards
# for every worktree at once.
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
        echo "[prepush] ⛔ This checkout has no scripts/ops/prepush_dispatch.sh, so the"
        echo "pre-push guards cannot run. .git/hooks is shared by every worktree, so one"
        echo "tree older than #1689 hits this while the others are fine."
        echo ""
        echo "This tree is checked out before #1689 landed."
        echo "Fix: rebase / switch it to a commit that has the dispatcher."
        echo ""
        echo "⛔ Re-running the installer does NOT fix this, and it exits 0,"
        echo "which reads like success: the shim it rewrites is byte-identical,"
        echo "and the shim resolves the dispatcher from the tree you push FROM."
        echo ""
        echo "⛔ Do not reach for --no-verify and do not delete .git/hooks/pre-push:"
        echo "both turn off the direct-push-to-main guard for EVERY worktree at once."
        echo ""
    } >&2
    exit 1
fi
exec bash "$_dispatch" "$@"
VIBE_SHIM_EOF

printf '%s' "$SHIM_BODY" > "$target" || { warn "⛔ could not write $target"; exit 1; }

# ⛔ Not `|| true`. git SILENTLY IGNORES a hook without the executable bit — it
# prints one `hint:` line that `advice.ignoredHook=false` turns off — so a
# swallowed chmod failure leaves a hook file that looks installed and never
# runs. Measured: with the bit cleared, a direct push to main succeeded with the
# guard banner absent.
if ! chmod +x "$target"; then
    warn "⛔ could not make $target executable. git ignores non-executable hooks"
    warn "   with only a hint, so the guards would look installed and never run."
    exit 1
fi

if [ "$target" = "$legacy" ]; then
    say "installed guard shim at $target (pre-commit owns $hook and calls it with the full refspec)"
else
    say "installed guard shim at $target"
fi
exit 0
