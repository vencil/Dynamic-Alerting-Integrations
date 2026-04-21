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
#
# Exit codes
# ----------
#   0 — index clean (--check) OR rebuilt successfully
#   1 — rebuild attempted but failed (see stderr)
#   2 — --check: corruption detected (no rebuild)
#
# Why not just rm .git/index? Because under FUSE phantom-locking, `rm` often
# returns EPERM on .git/index. The temp-index + cp pattern sidesteps this.

set -euo pipefail

MODE="${1:-run}"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
INDEX="$REPO_ROOT/.git/index"

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
if [ "$MODE" = "--check" ]; then
    if [ "$CORRUPT" = "0" ]; then
        echo "✅ Index is healthy."
        exit 0
    fi
    echo "🔴 Index corruption detected."
    echo ""
    echo "Signature:"
    echo "$ERR" | sed 's/^/    /'
    echo ""
    echo "To rebuild: bash scripts/ops/recover_index.sh   (or `make recover-index`)"
    exit 2
fi

# ---------- Repair ---------------------------------------------------------
if [ "$CORRUPT" = "0" ]; then
    echo "✅ Index is healthy — nothing to recover."
    exit 0
fi

echo "🔧 Rebuilding .git/index from HEAD via temp-index plumbing..."
TMP_IDX="$(mktemp /tmp/recover_idx_XXXXXX)"
trap 'rm -f "$TMP_IDX"' EXIT

# read-tree with a separate GIT_INDEX_FILE avoids touching the corrupted
# .git/index (and avoids any .git/index.lock handshake).
if ! GIT_INDEX_FILE="$TMP_IDX" git -C "$REPO_ROOT" read-tree HEAD; then
    echo "❌ Failed to read-tree HEAD into temp index." >&2
    exit 1
fi

# Replace the corrupted index. cp (not mv) because mv would fail across
# filesystem boundaries (/tmp → FUSE) and we want the atomic write behavior.
if ! cp "$TMP_IDX" "$INDEX"; then
    echo "❌ Failed to write .git/index (is it locked? check \`make fuse-locks\`)." >&2
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
git -C "$REPO_ROOT" status --short 2>&1 | sed 's/^/    /' >&2
exit 1
