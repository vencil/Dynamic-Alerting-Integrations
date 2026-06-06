"""Version SSOT readers for the dx doc-generation tools.

`generate_doc_map.py` and `generate_tool_map.py`
all stamp a `version:` field into the frontmatter of the docs they emit.
Hardcoding that string in the generator source means a release bump has to
patch the Python source by hand — `bump_docs.py --check` cannot see it
because the version lives in a runtime-generated string, not a checked-in
frontmatter field (this bit the v2.8.0 -> v2.8.1 release commit, PR #503).

Centralising the lookup here lets every generator derive the version from the
same source of truth that `bump_docs.py` already maintains, so a
`bump_docs.py --platform` run propagates automatically.
"""
from __future__ import annotations

import re
from pathlib import Path

# scripts/tools/_lib_versions.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

# Last-resort fallbacks, used only when the SSOT file is missing or
# unparseable. The live path is the read below; bump_docs.py keeps the
# SSOT current on every release.
_PLATFORM_VERSION_FALLBACK = "v2.8.1"
_DA_TOOLS_VERSION_FALLBACK = "1.11.0"

# Anchor must stay in sync with bump_docs.py's platform write rule
# (_build_platform_rules) and read regex (read_current_versions): since
# v2.6.0 the version lives in the bold lead-in line under "## 專案概覽",
# not in the heading itself.
_PLATFORM_VERSION_RE = re.compile(
    r"Multi-Tenant Dynamic Alerting 平台 \(v([0-9]+\.[0-9]+[^)]*)\)"
)


def read_platform_version(default: str = _PLATFORM_VERSION_FALLBACK) -> str:
    """Return the platform version (e.g. ``v2.8.1``) from CLAUDE.md.

    Reads the ``## 專案概覽`` lead-in line. Returns ``default`` if CLAUDE.md
    is missing or the anchor cannot be found.
    """
    claude_md = REPO_ROOT / "CLAUDE.md"
    if claude_md.exists():
        m = _PLATFORM_VERSION_RE.search(claude_md.read_text(encoding="utf-8"))
        if m:
            return "v" + m.group(1)
    return default


def read_da_tools_version(default: str = _DA_TOOLS_VERSION_FALLBACK) -> str:
    """Return the da-tools version (e.g. ``1.11.0``) from its VERSION file."""
    ver_file = REPO_ROOT / "components" / "da-tools" / "app" / "VERSION"
    if ver_file.exists():
        tv = ver_file.read_text(encoding="utf-8").strip()
        if re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", tv):
            return tv
    return default
