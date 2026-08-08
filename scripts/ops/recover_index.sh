#!/bin/bash
# recover_index.sh — rebuild a corrupted .git/index from HEAD via temp-index plumbing
#
# Problem
# -------
# Under Cowork's FUSE mount, the Git index can become corrupted mid-operation:
#
#   fatal: index file corrupt
#   error: index uses ??�� extension, which we do not understand
#
# After this happens, NOTHING git-related works: no status, no add, no commit.
# The fix that always works is: rebuild the index from HEAD into a fresh file,
# then replace .git/index.
#
# Usage
# -----
#   bash scripts/ops/recover_index.sh           # diagnose + rebuild
#   bash scripts/ops/recover_index.sh --check   # diagnose only (exit 0 = clean,
#                                               # exit 2 = corruption detected)
#   bash scripts/ops/recover_index.sh --help    # this text (also -h)
#
# Rebuild is reachable ONLY by passing no argument — it has no spelling, so a
# typo cannot request it. Anything unrecognised exits 64 without touching
# anything. Via make: `make recover-index` rebuilds; mentioning CHECK at ANY
# value (`make recover-index CHECK=1`, `CHECK=`, …) diagnoses only.
#
# Exit codes
# ----------
#   0 — index clean (--check) OR rebuilt successfully
#   1 — could not proceed (see stderr). EITHER diagnosis found a git failure with
#       no known index-corruption signature — no rebuild was attempted, in either
#       mode — OR a rebuild was attempted and failed. Do not read 1 as "a repair
#       ran": on the first path nothing was touched.
#   2 — --check: corruption detected (no rebuild)
#  64 — bad usage (unknown or extra argument). Nothing was read or written:
#       unrecognised input is rejected, never treated as a rebuild request.
#
# Works from a linked worktree as well as the main checkout.
#
# Why not just rm .git/index? Because under FUSE phantom-locking, `rm` often
# returns EPERM on .git/index. The temp-index + cp pattern sidesteps this.
#
# The index path is taken from `git rev-parse --git-path index`, which is what
# makes the linked-worktree case work (there `.git` is a file and the real index
# lives under `.git/worktrees/<name>/`) and what keeps diagnosis and repair from
# resolving different indexes under GIT_DIR — see the block above the derivation.

set -euo pipefail

# ⛔ Arity FIRST: the per-spelling case below exits 0 for --help, so checking
# arity after it let `--help foo bar` succeed while the header promises 64.
if [ "$#" -gt 1 ]; then
    echo "❌ Too many arguments (expected at most one)." >&2
    echo "   ⛔ Refusing to fall through to the rebuild path." >&2
    exit 64
fi

# ⛔ Anything that is not exactly `--check` used to fall through to the REPAIR
# path, because the only test was `[ "$MODE" = "--check" ]`. Measured on a corrupt
# index: `--help`, `-check`, `--CHECK` and `--check=1` each rebuilt .git/index and
# printed "✅ Index recovered." — i.e. asking this tool for HELP destroyed the
# operator's staged work, during the incident it exists for. Same shape as the
# backtick defect in the advice line: a command the operator believes is read-only
# performs a destructive write. Unknown input must never resolve to the
# destructive default, so reject it instead of guessing.
#
# ⛔ REPAIR is reachable only by passing NO argument — there is deliberately no
# spelling for it. An earlier fix used `MODE="${1:-run}"` plus a `run)` branch,
# which was wrong twice: `${1:-…}` substitutes on unset OR NULL, so an EMPTY
# argument became `run` and rebuilt the index (measured rc=0, index rewritten —
# exactly what `bash recover_index.sh "$MODE"` emits when a wrapper's MODE is
# unset); and `run` itself was an accepted, destructive, undocumented spelling.
#
# ⛔ This case is the SINGLE place that maps a spelling to an action. It used to
# be two independent lists — an accept-list here and a separate
# `[ "$MODE" = "--check" ]` further down — so adding a spelling to the accept-list
# alone (e.g. an `-c` alias) silently made it REPAIR. Assigning ACTION here means
# a new spelling cannot be added without choosing what it does.
if [ "$#" -eq 0 ]; then
    ACTION=repair
else
    case "$1" in
        -h|--help)
            # Delimited by MARKERS, not line numbers. A hardcoded `15,29p` was
            # already cutting the last exit-code entry mid-sentence, and any later
            # header edit shifts it further — silently, since --help still prints
            # *something*.
            sed -n '/^# Usage$/,/^# Why not just rm/p' "$0" \
                | sed '$d' | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        --check) ACTION=diagnose ;;
        *)
            echo "❌ Unknown argument: ${1:-(empty)}" >&2
            echo "   Usage: bash scripts/ops/recover_index.sh [--check]   (--help for detail)" >&2
            echo "   ⛔ Refusing to fall through to the rebuild path — it would discard" >&2
            echo "      whatever is staged, and you did not ask for it." >&2
            exit 64
            ;;
    esac
fi

# ⛔ DIAGNOSE AND REPAIR MUST RESOLVE THE SAME INDEX. They did not: `git status`
# honours the git environment, while the rebuild target was the hardcoded
# `$REPO_ROOT/.git/index`. Both directions were measured, both destructive:
#   - GIT_INDEX_FILE=<corrupt> in a healthy repo → `--check` reported corruption
#     (rc=2), then the repair destroyed the HEALTHY index (`A s.txt` → `?? s.txt`).
#   - GIT_DIR=<other repo> → same shape; and the REVERSE is a false all-clear —
#     a genuinely corrupt repo with GIT_DIR pointing at a healthy one printed
#     "✅ Index is healthy." rc=0, which is the one thing `--check` exists to
#     prevent. Git exports these to every hook, so this is reachable, not exotic.
#
# Unsetting one variable at a time chases instances. Instead: drop the override
# that has no path form (GIT_INDEX_FILE), then ASK GIT for the index it will
# actually use. `--git-path index` honours GIT_DIR and resolves a linked
# worktree's real index under `.git/worktrees/<name>/`, so the two can no longer
# disagree — and the worktree limitation this script carried goes with it.
unset GIT_INDEX_FILE

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
INDEX="$(git rev-parse --git-path index 2>/dev/null || echo "$REPO_ROOT/.git/index")"
case "$INDEX" in
    /*|[A-Za-z]:[/\\]*) ;;                    # already absolute
    *) INDEX="$(pwd)/$INDEX" ;;               # --git-path can return a relative path
esac

# ---------- Diagnosis ------------------------------------------------------
# `git status` is the canonical corruption probe; it walks the index.
if git -C "$REPO_ROOT" status --short >/dev/null 2>&1; then
    CORRUPT=0
else
    # Capture exact failure for the report
    ERR="$(git -C "$REPO_ROOT" status --short 2>&1 || true)"
    # Heuristics: known corruption signatures
    #   - "index file corrupt"                    (classic)
    #   - "index uses X extension"                (FUSE-observed: garbled extension)
    #   - "index file smaller than expected"      (truncation)
    #   - "bad index file signature"              (header clobber)
    #   - "bad index file sha1 signature"         (trailer clobber)
    #   - ".git/index: ..."                       (any .git/index-rooted failure)
    case "$ERR" in
        *"index file corrupt"* \
        | *"index uses "*"extension"* \
        | *"index file smaller than expected"* \
        | *"bad index file signature"* \
        | *"bad index file sha1"* \
        | *".git/index:"* )
            CORRUPT=2
            ;;
        *)
            # Some other failure (missing HEAD, detached weirdness, etc.) —
            # not our department. Report and exit 1.
            echo "⚠️  git status failed but not with a known index-corruption signature:" >&2
            echo "$ERR" >&2
            exit 1
            ;;
    esac
fi

# ---------- --check mode ---------------------------------------------------
# Branch on ACTION, not on the spelling: re-testing `$MODE` here is what made the
# accept-list and the dispatch two independent lists (see the case above).
if [ "$ACTION" = "diagnose" ]; then
    if [ "$CORRUPT" = "0" ]; then
        echo "✅ Index is healthy."
        exit 0
    fi
    echo "🔴 Index corruption detected."
    echo ""
    echo "Signature:"
    echo "$ERR" | sed 's/^/    /'
    echo ""
    # ⛔ Backticks ESCAPED on purpose. Unescaped, bash runs `make recover-index`
    # right here — and that target with CHECK unset is the REPAIR path, so the
    # documented diagnose-only mode rebuilt .git/index and discarded whatever the
    # operator had staged, then still exited 2 (which the header above documents
    # as "corruption detected (no rebuild)"). The repair banner does get printed,
    # spliced mid-sentence into this very line — but nothing says the staging area
    # was just thrown away, and the exit code says the opposite.
    echo "To rebuild: bash scripts/ops/recover_index.sh   (or \`make recover-index\`)"
    exit 2
fi

# ---------- Repair ---------------------------------------------------------
if [ "$CORRUPT" = "0" ]; then
    echo "✅ Index is healthy — nothing to recover."
    exit 0
fi

echo "🔧 Rebuilding .git/index from HEAD via temp-index plumbing..."
TMP_IDX="$(mktemp /tmp/recover_idx_XXXXXX)"
# Stage path on the SAME filesystem as .git/index so `mv` is atomic (rename(2)).
# /tmp is usually a different filesystem; cross-FS mv falls back to copy+delete,
# which is NOT atomic. Keep the staging file next to the real index.
STAGE_IDX="$INDEX.recover.$$"
trap 'rm -f "$TMP_IDX" "$STAGE_IDX"' EXIT

# read-tree with a separate GIT_INDEX_FILE avoids touching the corrupted
# .git/index (and avoids any .git/index.lock handshake).
if ! GIT_INDEX_FILE="$TMP_IDX" git -C "$REPO_ROOT" read-tree HEAD; then
    echo "❌ Failed to read-tree HEAD into temp index." >&2
    exit 1
fi

# Replace the corrupted index atomically: cp to a sibling path (same FS as
# .git/index) then mv, which is rename(2) and atomic on POSIX. A bare `cp`
# onto .git/index is NOT atomic — a reader could see a partially written file.
if ! cp "$TMP_IDX" "$STAGE_IDX"; then
    echo "❌ Failed to stage .git/index replacement (is the FS full?)." >&2
    exit 1
fi
if ! mv "$STAGE_IDX" "$INDEX"; then
    echo "❌ Failed to rename staged index onto .git/index (is it locked? check \`make fuse-locks\`)." >&2
    echo "   Fallback: use \`make fuse-commit\` for plumbing commits that" >&2
    echo "   don't require a healthy .git/index." >&2
    exit 1
fi

# Verify repair worked
if git -C "$REPO_ROOT" status --short >/dev/null 2>&1; then
    echo "✅ Index recovered. \`git status\` is back."
    exit 0
fi
echo "❌ Rebuild ran but index still unreadable." >&2
echo "   Raw error from git status:" >&2
# ⛔ Capture first, THEN print. Piping git straight into sed under
# `set -euo pipefail` let git's 128 propagate through the pipeline and abort the
# script — so this branch exited 128, never reaching the `exit 1` the header
# documents. `|| true` keeps the failure expected (it is the whole point here).
VERIFY_ERR="$(git -C "$REPO_ROOT" status --short 2>&1 || true)"
printf '%s\n' "$VERIFY_ERR" | sed 's/^/    /' >&2
exit 1
