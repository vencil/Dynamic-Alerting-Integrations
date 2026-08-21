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

# Two spellings of the platform version are matched, both as *precise*
# phrases — never as an anchor plus a scanning window.
#
# ⛔ Three invariants, each of which was violated by an earlier draft and is
# pinned by a test. The full account lives in issue #1480; do not re-add it
# here.
#   1. The search starts at the `## 專案概覽` HEADING and never at the top of
#      the file (an unanchored search reads a quoted release note above it).
#   2. The anchor must be a heading line (a table-of-contents entry that
#      merely mentions 專案概覽 must not move it).
#   3. Whitespace between the heading text and `(v` is horizontal-only:
#      `\s` matches newlines, so `\s*` there silently re-creates the
#      unbounded window these patterns replaced.
# There is deliberately NO distance limit — the phrases decide what counts,
# so how far the version sits from the heading stops mattering.
_VERSION_TOKEN = r"([0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[-+][0-9A-Za-z.-]+)?)"
# The heading that opens the section the version lives in. Must be a
# heading line: a bare mention of 專案概覽 in prose or a table of
# contents cannot move where the search starts.
_SECTION_HEADING_RE = re.compile(r"(?m)^#{1,6}[^\S\n]*專案概覽")

_PLATFORM_VERSION_PATTERNS = (
    # Current: the bold lead-in line under the heading.
    re.compile(r"Multi-Tenant Dynamic Alerting 平台 \(v" + _VERSION_TOKEN),
    # Historical (<= v2.6.0): inline in the heading itself.
    #
    # ⛔ The whitespace class is horizontal-only. `\s` matches newlines, so
    # writing `\s*` between the heading text and `(v` re-creates the very
    # loose window this pattern was written to replace: it spans any number
    # of blank lines and reads a bare `(v9.9.9)` further down the file as the
    # platform version. Measured on `## 專案概覽` + 10 blank lines +
    # `(v9.9.9)`: the newline-swallowing class returns 9.9.9, this one
    # returns nothing, and the real inline spelling still matches.
    #
    # Found only because #1480 collapsed `check_frontmatter_versions` onto
    # this reader and *its* existing test covered that shape. ⚠️ Not "this
    # module's tests missed it" — `_lib_versions` has no test file at all,
    # which is the more useful thing to know about the reader four tools
    # now share.
    re.compile(r"(?m)^#{1,6}[^\S\n]*專案概覽[^\S\n]*\(v" + _VERSION_TOKEN),
)


def read_platform_version(
    default: str = _PLATFORM_VERSION_FALLBACK,
    claude_md: Path | None = None,
) -> str:
    """Return the platform version (e.g. ``v2.8.1``) from CLAUDE.md.

    Returns *default* when CLAUDE.md is missing or neither spelling matches.

    Args:
        default: value returned when the version cannot be read. Pass ``""``
            when the caller needs to *detect* that failure rather than paper
            over it with a stale release string.
        claude_md: override the file to read. Exists so callers that keep
            their own SSOT-path constant can inject it in tests instead of
            reaching the real repository file.
    """
    path = Path(claude_md) if claude_md is not None else REPO_ROOT / "CLAUDE.md"
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8")

    # ⛔ The search starts at the `## 專案概覽` heading, never at the top of
    # the file.
    #
    # An earlier revision searched the whole document, on the reasoning that
    # the product-name phrase is specific enough that "an unrelated mention
    # cannot satisfy it". Blind review measured that away: put a line naming
    # the product with a version ABOVE the heading — a changelog snippet, a
    # quoted release note — and the unanchored search returns that one. The
    # reader this replaced *was* anchored, so dropping the anchor was a
    # regression, and the reasoning was the exact "it happens to be correct
    # on today's file" argument this module exists to stop making. The phrase
    # being unique today is a property of today's CLAUDE.md, not of the
    # pattern.
    #
    # ⚠️ Anchoring is NOT the six-line window that was rejected above. There
    # is no distance limit: the anchor only says where to start reading, and
    # the precise phrases still say what counts. The anchor itself must be a
    # heading line, so a table-of-contents entry or a cross-reference that
    # merely mentions 專案概覽 cannot move it.
    anchor = _SECTION_HEADING_RE.search(text)
    if anchor is None:
        return default
    section = text[anchor.start():]

    for pattern in _PLATFORM_VERSION_PATTERNS:
        m = pattern.search(section)
        if m:
            return "v" + m.group(1)
    return default


class PlatformVersionUnreadable(RuntimeError):
    """The platform version could not be read from CLAUDE.md.

    Raised by :func:`require_platform_version` so that callers which *stamp*
    the version into generated artefacts can fail instead of writing a stale
    one. Callers should translate this into their own exit-code contract
    rather than letting it surface as a traceback.
    """


def require_platform_version(claude_md: "Path | None" = None) -> str:
    """Return the platform version, or raise if it cannot be read.

    ⛔ Use this instead of ``read_platform_version()`` whenever the value is
    written into a file. The default fallback exists for read-only callers
    that can tolerate an approximation; for a generator it is the worst
    possible behaviour — `_PLATFORM_VERSION_FALLBACK` is a hard-coded release
    string that nothing updates, so a broken anchor silently stamps a version
    that was current at some point in the past. #1480: `generate_doc_map` and
    `generate_tool_map` both did exactly that, writing `version:` into the
    frontmatter of `doc-map.md` / `tool-map.md` — the very field the revived
    `check_platform_version` validates.
    """
    version = read_platform_version(default="", claude_md=claude_md)
    if not version:
        raise PlatformVersionUnreadable(
            "could not read the platform version from CLAUDE.md; refusing to "
            "stamp the stale fallback %r into generated output. Fix the "
            "anchor in _lib_versions.read_platform_version."
            % _PLATFORM_VERSION_FALLBACK)
    return version


def read_da_tools_version(default: str = _DA_TOOLS_VERSION_FALLBACK) -> str:
    """Return the da-tools version (e.g. ``1.11.0``) from its VERSION file."""
    ver_file = REPO_ROOT / "components" / "da-tools" / "app" / "VERSION"
    if ver_file.exists():
        tv = ver_file.read_text(encoding="utf-8").strip()
        if re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", tv):
            return tv
    return default
