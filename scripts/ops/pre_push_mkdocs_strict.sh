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
#   one runs locally during pre-commit; the other fires only in CI. This
#   gap has bitten 5+ PRs in close succession (#391/#375/#400/#406/#411).
#   Issue #412 documents the pattern + recurrence.
#
#   This hook closes the gap by running mkdocs strict at pre-push time
#   when docs changed.
#
# Triggers (pre-push only):
#   Any push that includes changes to:
#     - docs/**/*.md
#     - mkdocs.yml
#     - README.md / README.en.md
#     - CHANGELOG.md (rendered via docs/ symlink)
#
# Tiered execution:
#   Tier 1 — Native `mkdocs` on PATH: run directly (fastest, ~25s)
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
#   Wired in .pre-commit-config.yaml under `stages: [pre-push]`.
#
# See:
#   - scripts/tools/lint/mkdocs_strict_check.sh — the actual strict check
#   - issue #412 — the recurrence pattern that motivated this hook
#   - dev-rules.md #4 — mkdocs strict semantic gap context

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

# --- Escape hatch ------------------------------------------------------------
if [ "${MKDOCS_STRICT_BYPASS:-0}" = "1" ]; then
    echo "[pre-push-mkdocs] MKDOCS_STRICT_BYPASS=1 set; skipping mkdocs strict check"
    exit 0
fi

# --- Detect doc changes in this push ----------------------------------------
# Reference: compare HEAD against upstream tracking branch (or origin/main
# fallback). pre-commit pre-push doesn't expose remote refs cleanly to the
# subprocess; use git's own upstream resolution.
UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo "")
if [ -z "$UPSTREAM" ]; then
    # No upstream — diff against origin/main as fallback
    if git rev-parse --verify --quiet 'origin/main' >/dev/null; then
        UPSTREAM='origin/main'
    else
        # No origin/main either (fresh clone? detached?). Skip; we can't
        # determine what's being pushed.
        echo "[pre-push-mkdocs] no upstream / origin/main; cannot determine push scope; skip"
        exit 0
    fi
fi

# Use --diff-filter=ACMR to include Added/Copied/Modified/Renamed (skip Deleted)
CHANGED=$(git diff --name-only --diff-filter=ACMR "$UPSTREAM"...HEAD 2>/dev/null || echo "")

# Filter to doc-affecting files
DOC_CHANGES=$(echo "$CHANGED" | grep -E '^(docs/.*\.(md|jsx|html)$|mkdocs\.yml$|README\.md$|README\.en\.md$|CHANGELOG\.md$)' || true)

if [ -z "$DOC_CHANGES" ]; then
    # Non-doc push; nothing to check
    exit 0
fi

echo "[pre-push-mkdocs] Doc changes detected in this push:"
echo "$DOC_CHANGES" | sed 's/^/  • /'
echo ""

# --- Tiered execution --------------------------------------------------------
# Tier 1: native mkdocs
if command -v mkdocs >/dev/null 2>&1; then
    echo "[pre-push-mkdocs] Using native mkdocs ($(mkdocs --version 2>&1 | head -1))"
    if bash "$REPO_ROOT/scripts/tools/lint/mkdocs_strict_check.sh"; then
        echo "[pre-push-mkdocs] ✅ mkdocs strict PASS"
        exit 0
    else
        echo ""
        echo "::error::mkdocs strict check failed. See output above."
        echo ""
        echo "Common fixes for the recurring site-root path gotcha:"
        echo "  • ../../foo.md from docs/X/Y.md → use absolute GitHub URL"
        echo "    https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/foo.md"
        echo "  • #anchor-with--double-dash → single-dash (mkdocs normalizes consecutive dashes)"
        echo "  • Missing file in nav → add to mkdocs.yml or remove the link"
        echo ""
        echo "Bypass (emergency only): MKDOCS_STRICT_BYPASS=1 git push"
        exit 1
    fi
fi

# Tier 2: no native mkdocs; soft fail
echo "[pre-push-mkdocs] ⚠️  mkdocs not on PATH; cannot validate locally."
echo "                  Doc changes will be validated by CI."
echo ""
echo "  To enable local pre-push validation (one-time, ~30s install):"
echo "    pip install --user mkdocs-material mkdocs-static-i18n pymdown-extensions"
echo ""
echo "  Continuing push; CI will catch any mkdocs strict failures."
exit 0
