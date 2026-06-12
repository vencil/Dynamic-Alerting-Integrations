#!/usr/bin/env python3
"""PreToolUse hook: block patterns we already know cause damage in this repo.

Currently enforced (audit playbook-audit-2026-04 §H1, §H2):

  1. Bash tool — `sed -i` (or `sed --in-place`) targeting mounted paths.
     Reason: dev-rules #11. FUSE mount truncates files lacking EOF newline
     and may inject NUL bytes. The repair layer (`fix_file_hygiene.py`)
     and detection layer (`detect_sed_damage.py`) already exist; this is
     the prevention layer at the harness. The shell-function override
     `vibe-sed-guard.sh` does not fire from Claude Code's Bash tool
     (no .bashrc sourcing), so we attach here instead.

  2. Write tool — ad-hoc `_*.bat` / `_*.ps1` / `_*.cmd` outside the
     whitelisted dirs (scripts/ops/, scripts/tools/, tools/).
     Reason: trap #54 (session-after-session reinventing escape-hatch
     wrappers). The pre-commit hook `check_ad_hoc_git_scripts.py` already
     blocks these at commit time; this stops them at write time so we
     don't burn tokens producing the file.

Failure policy:
  - Hook never blocks "normal" tool calls. Parse failures, regex bugs,
    unhandled exceptions all exit 0 with a stderr warning.
  - Only documented patterns trigger exit 2 with a remediation message.

Exit codes:
  0 — allow (default)
  2 — block; stderr is fed back to the model so it can self-correct.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo root resolution
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[2]  # scripts/session-guards/preflight_bash.py → repo

# UTF-8 stdout/stderr guard (#824) — 本檔訊息目前全 ASCII，但與同目錄其他
# guard 一致套用，避免未來訊息加非 ASCII 字元時重演 cp950 靜默失效。
sys.path.insert(0, str(_THIS.parents[1] / "tools"))
try:
    from _lib_compat import try_utf8_stdout
except Exception:  # pragma: no cover — standalone fallback, never block
    def try_utf8_stdout() -> None:  # type: ignore
        return None

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# `sed -i` or `sed --in-place`. Covers:
#   sed -i '...'
#   sed -i.bak '...'
#   sed -i'' '...'      (zero-length suffix)
#   sed -i"" '...'      (double-quoted zero-length)
#   sed -isuffix        (glued)
#   sed --in-place
#   gsed -i             (GNU sed alias on macOS / some Cowork images)
# Word boundary on the left of `sed` so we don't match `pushed`, `parsed`, etc.
_SED_INPLACE_RE = re.compile(
    r"(?<![A-Za-z0-9_/])g?sed\s+(?:[^|;&]*\s)?(?:-[A-Za-z]*i|--in-place)",
    re.IGNORECASE,
)

# Mount-path patterns that FUSE / Docker bind-mount paths fall under in this
# repo. We intentionally cast a wide net — false positives only show a clear
# remediation message; false negatives let damage through.
_MOUNT_PATH_RE = re.compile(
    r"(?:"
    r"/sessions/[^\s/]+/mnt/"             # Cowork sandbox legacy
    r"|/workspaces/vibe-k8s-lab/"         # Dev container mount
    r"|/c/Users/[^\s/]+/vibe-k8s-lab/"    # Git Bash POSIX path
    r"|[Cc]:[\\/]+Users[\\/]+[^\s\\/]+[\\/]+vibe-k8s-lab[\\/]+"  # Windows native
    r")"
)

# Ad-hoc script names. Pre-commit hook already enforces this at commit; we
# add the harness layer to stop the WRITE before tokens are spent.
# Whitelist mirrors `scripts/tools/lint/check_ad_hoc_git_scripts.py`.
_AD_HOC_SCRIPT_RE = re.compile(r".*[\\/]_[^\\/]+\.(bat|ps1|cmd)$", re.IGNORECASE)
_AD_HOC_WHITELIST_DIRS = (
    "scripts/ops/",
    "scripts/tools/",
    "tools/",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_path(p: str) -> str:
    """Normalize Windows backslashes to forward slashes for matching."""
    return p.replace("\\", "/")


def _is_in_whitelist(file_path: str) -> bool:
    norm = _normalize_path(file_path)
    return any(d in norm for d in _AD_HOC_WHITELIST_DIRS)


def _check_bash(command: str) -> tuple[int, str]:
    """Return (exit_code, stderr_message). exit_code 0 = allow, 2 = block."""
    if not command:
        return 0, ""
    sed_match = _SED_INPLACE_RE.search(command)
    if not sed_match:
        return 0, ""
    if not _MOUNT_PATH_RE.search(command):
        # `sed -i` is fine on /tmp/, on host paths, anywhere outside our
        # mounted workspace. Don't get in the way.
        return 0, ""
    msg = (
        "[preflight] BLOCKED: dev-rules #11 -- `sed -i` on mounted path.\n"
        "FUSE truncates files lacking EOF newline + may inject NUL bytes.\n"
        "\n"
        "Replace this command with one of:\n"
        "  - Read + Edit       (preferred for single-file changes)\n"
        "  - Read + Write      (full-file rewrites)\n"
        "  - Pipe out-of-place: git show HEAD:<f> | sed '...' > <f>\n"
        "                      (read from HEAD avoids FUSE stale state)\n"
        "  - Python + atomic_write: scripts/tools/dx/_atomic_write.py\n"
        "\n"
        "See docs/internal/dev-rules.md (rule 11). The regex only fires\n"
        "when both `sed -i` AND a mount-path token appear on the same\n"
        "Bash invocation -- legitimate sed -i outside the mount is fine.\n"
        f"Detected command fragment: {sed_match.group(0)}"
    )
    return 2, msg


def _check_write(file_path: str) -> tuple[int, str]:
    if not file_path:
        return 0, ""
    norm = _normalize_path(file_path)
    if not _AD_HOC_SCRIPT_RE.match(norm):
        return 0, ""
    if _is_in_whitelist(norm):
        return 0, ""
    msg = (
        "[preflight] BLOCKED: ad-hoc `_*.bat` / `_*.ps1` / `_*.cmd` outside\n"
        "the whitelisted dirs (scripts/ops/, scripts/tools/, tools/).\n"
        "\n"
        "This repo already has standard Windows escape-hatch wrappers.\n"
        "Don't reinvent them -- extend or call them instead:\n"
        "  - scripts/ops/win_git_escape.bat  (status / add / commit-file /\n"
        "                                     push / tag / branch / log /\n"
        "                                     diff / preflight / pr-preflight\n"
        "                                     / fix-hooks)\n"
        "  - scripts/ops/win_gh.bat          (pr-checks / pr-view / pr-create\n"
        "                                     / run-view / run-log / raw)\n"
        "  - scripts/ops/win_async_exec.ps1  (fire-and-forget for >60s ops)\n"
        "  - scripts/ops/win_read_fresh.ps1  (FUSE dentry cache bypass)\n"
        "  - make win-commit / fuse-commit / recover-index / fuse-locks\n"
        "\n"
        "Both wrappers expose `raw <args>` for one-off needs. If you truly\n"
        "need a new subcommand, extend the wrapper in scripts/ops/ and\n"
        "submit it (the `bat-ascii-purity-check` hook will validate).\n"
        "See docs/internal/windows-mcp-playbook.md (section: 'Repair layer C',\n"
        "trap #54). Run `make help-escape` for a quick reference.\n"
        f"Attempted path: {file_path}"
    )
    return 2, msg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    try_utf8_stdout()
    # Read JSON payload from stdin per Claude Code PreToolUse hook contract.
    raw = sys.stdin.read()
    if not raw.strip():
        # Manual invocation with no payload — nothing to check.
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[preflight] warning: invalid JSON ({exc}); allowing", file=sys.stderr)
        return 0

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}

    try:
        if tool_name == "Bash":
            command = tool_input.get("command") or ""
            code, msg = _check_bash(command)
        elif tool_name in ("Write",):
            # Edit / MultiEdit don't create new ad-hoc scripts (they require an
            # existing file). Limit to Write.
            file_path = tool_input.get("file_path") or ""
            code, msg = _check_write(file_path)
        else:
            return 0
    except Exception as exc:  # noqa: BLE001 — never block on hook bug
        print(f"[preflight] warning: hook crashed ({exc}); allowing", file=sys.stderr)
        return 0

    if code == 0:
        return 0
    print(msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    # Keep PYTHONUTF8 implicit; we only print ASCII through stderr.
    sys.exit(main())
