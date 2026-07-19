#!/usr/bin/env python3
"""check_design_token_usage.py — JSX 設計 token 使用完整性 lint

掃描 JSX 工具檔案, 檢測:
  (a) hardcoded hex 色碼（應使用 var(--da-color-*) token）
  (b) hardcoded px 數值在 style object 中（應使用 --da-space-* 或 --da-font-size-* token）
  (c) 飽和語意 error/warning token 當「前景文字色」（--da-color-error/-warning）
      兩種形態：(c1) Tailwind className `text-[color:…]`；(c2) inline style /
      colors-map 前景鍵（color: / text: / textColor:）吃裸 token — 含
      ternary/conditional 值（如 `color: cond ? 'var(--da-color-error)' : …`），
      值範圍 bound 在該屬性內（遇 `,`/`;`/`}`/換行 即止）以免誤扣鄰屬性；
      變數間接（`color: c`）屬 #1075 範圍，line-based lint 不涵蓋。
      — 淺色主題對比 3.76:1 / 2.15:1 < AA 4.5:1（WCAG 1.4.3）；應改 AA 版
      `-error-text` / `-warning-text`。#885/#904 的 stroke-as-text 類，codify 防復發。

例外規則:
  - 行末註解 /* token-exempt */ 豁免整行
  - // 單行註解中的 hex 不檢查
  - #fff 和 #000 過於常見，不檢查
  - 0px, 1px, 2px（邊框 / hairline）不檢查
  - design-tokens.css 本身不掃描（定義端）

Lint class & scope (lint-policy.md §3)
--------------------------------------
Class **(b)** — negative pattern + token-exempt allowlist.
Default scope: **diff-only** — only lines ADDED in current diff vs base
emit findings. Override with --full-scan for periodic manual audit.

Bypass (per lint-policy.md §4):
    Add to PR description body:
        bypass-lint: design-token-usage
        reason: <≥30 words explaining why this case is legitimate>

用法:
    # Diff-only (default; CI sets LINT_DIFF_BASE / GITHUB_BASE_REF)
    python3 scripts/tools/lint/check_design_token_usage.py [--ci]

    # Full-scan (manual audit)
    python3 scripts/tools/lint/check_design_token_usage.py --full-scan [--ci]
"""

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

# Make stdout tolerate non-ASCII on Windows shells (cp950 / cp1252) — output
# uses ✓ + Chinese strings which crash on cp-default consoles otherwise.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# Helpers from this lint family
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lint_helpers import (  # noqa: E402
    DiffBaseMissingError,
    get_diff_added_lines,
    parse_bypass_tag,
    resolve_diff_base,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
# TRK-242 monorepo restructure: portal source moved from docs/ to
# tools/portal/src/. These dirs were left stale at docs/interactive/tools +
# docs/getting-started — neither holds .jsx anymore, so the lint silently
# scanned ZERO files and always passed (#444 Phase 0 keystone fix). Aligns with
# check_tool_registry_jsx_parity.py's JSX_ROOT = tools/portal/src.
JSX_TOOLS_DIR = REPO_ROOT / "tools" / "portal" / "src" / "interactive" / "tools"
WIZARD_DIR = REPO_ROOT / "tools" / "portal" / "src" / "getting-started"
DESIGN_TOKENS = REPO_ROOT / "docs" / "assets" / "design-tokens.css"


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------

# A line is exempt if it carries /* token-exempt */ OR the reasoned form
# /* token-exempt: <reason> */. Requiring the bare string only (the old
# behaviour) silently dropped reasoned markers — every "/* token-exempt:
# category colors */" was ignored and the finding still fired. Accepting the
# reasoned form *encourages* authors to document WHY a value is exempt.
_EXEMPT_RE = re.compile(r"/\*\s*token-exempt\b")


def _is_exempt(line: str) -> bool:
    return bool(_EXEMPT_RE.search(line))


def _strip_comments(content: str) -> List[str]:
    """Return per-line code with // line comments and /* */ block comments
    blanked out (replaced by spaces, preserving column positions and line
    count). This stops false positives where a hex-shaped token sits inside a
    comment — most commonly issue/PR references like `(extracted in PR #153)`
    inside a multi-line /* ... */ doc block, which the old per-line
    startswith("*") check could not see (continuation lines don't start with
    '*'). A /* token-exempt */ marker is preserved verbatim so the exempt
    check still sees it.

    Blanking (not deleting) keeps each line's other code intact, so a real
    violation on the same line as a trailing comment is still caught, e.g.
    ``color: '#abcdef' /* note */`` still reports #abcdef.

    Leading YAML frontmatter (a ``---`` fence at the very top of the file
    through the closing ``---``) is also blanked: jsx-loader tools carry a
    metadata block whose prose routinely contains issue refs like ``PR #158``
    that are hex-shaped but obviously not colors.
    """
    raw_lines = content.splitlines()

    # Blank a leading YAML frontmatter block (--- ... ---).
    fm_end = -1
    if raw_lines and raw_lines[0].strip() == "---":
        for idx in range(1, len(raw_lines)):
            if raw_lines[idx].strip() == "---":
                fm_end = idx
                break

    out: List[str] = []
    in_block = False
    for li, line in enumerate(raw_lines):
        if fm_end != -1 and li <= fm_end:
            out.append(" " * len(line))
            continue
        buf = []
        i = 0
        n = len(line)
        while i < n:
            if in_block:
                end = line.find("*/", i)
                if end == -1:
                    buf.append(" " * (n - i))
                    i = n
                else:
                    buf.append(" " * (end + 2 - i))
                    i = end + 2
                    in_block = False
                continue
            # not in a block comment
            # token-exempt marker: keep the rest of the line verbatim so
            # _is_exempt() still matches it.
            if line.startswith("/* token-exempt", i) or line.startswith(
                "/*token-exempt", i
            ):
                buf.append(line[i:])
                i = n
                continue
            star = line.find("/*", i)
            # Find a // that starts a line comment, skipping :// (URL schemes
            # like https://) so we don't mistake a URL for a comment and blank
            # out the rest of the line (which would hide real violations after
            # it). A bare // not preceded by ':' is treated as a comment.
            slash = -1
            probe = i
            while True:
                cand = line.find("//", probe)
                if cand == -1:
                    break
                if cand > 0 and line[cand - 1] == ":":
                    probe = cand + 2
                    continue
                slash = cand
                break
            # whichever comment opener comes first
            if slash != -1 and (star == -1 or slash < star):
                buf.append(line[i:slash])
                buf.append(" " * (n - slash))
                i = n
            elif star != -1:
                buf.append(line[i:star])
                buf.append("  ")
                i = star + 2
                in_block = True
            else:
                buf.append(line[i:])
                i = n
        out.append("".join(buf))
    return out


def check_hardcoded_hex_colors(content: str, filename: str) -> List[Dict]:
    """Scan for hardcoded hex colors that should use --da-color-* tokens.

    Returns list of {line_num, hex, context}.
    """
    issues = []

    # Hex color pattern: exactly 3, 4, 6, or 8 hex digits after #
    # Use negative lookbehind for & (HTML entities like &#8987;)
    # Use word boundary or end-of-token to avoid matching #dba-alerts
    hex_pattern = re.compile(r'(?<!&)#([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})(?![0-9a-fA-F\w-])')

    lines = content.splitlines()
    code_lines = _strip_comments(content)
    for i, line in enumerate(lines, 1):
        # Skip lines with exempt marker (bare or reasoned)
        if _is_exempt(line):
            continue

        # Scan the comment-stripped code (// and /* */ blanked) so hex-shaped
        # tokens inside comments — e.g. issue refs like (PR #153) — are ignored.
        code_part = code_lines[i - 1]

        # Find hex patterns
        for m in hex_pattern.finditer(code_part):
            hex_val = "#" + m.group(1)

            # Exceptions: #fff, #000, #ffffff, #000000
            if hex_val.lower() in ("#fff", "#000", "#ffffff", "#000000"):
                continue

            issues.append({
                "line": i,
                "hex": hex_val,
                "context": line.strip()[:80],
            })

    return issues


def check_hardcoded_px_values(content: str, filename: str) -> List[Dict]:
    """Scan for hardcoded px values in style objects.

    Looks for patterns like: fontSize: '14px', padding: '12px', etc.
    Skips: 0px, 1px, 2px (borders/hairlines), className strings.

    Returns list of {line_num, px, context}.
    """
    issues = []

    lines = content.splitlines()
    code_lines = _strip_comments(content)
    for i, line in enumerate(lines, 1):
        # Skip lines with exempt marker (bare or reasoned)
        if _is_exempt(line):
            continue

        # Comment-stripped code (// and /* */ blanked) for this line.
        code_part = code_lines[i - 1]

        # Only check inside style object context (rough heuristic)
        # Look for style={ or style=` patterns
        if "style=" not in code_part and "style =" not in code_part:
            continue

        # Find style property patterns
        # Match patterns like: fontSize: '14px', padding: 12, margin: "8px"
        # Group 3 captures the trailing unit (if any) so percentage/viewport/
        # em values are NOT misread as px. A bare number (no unit) is treated
        # as px because React maps unitless numeric style values to px.
        prop_pattern = re.compile(
            r'(\w+)\s*:\s*[\'"]?(\d+)(px|%|vh|vw|em|rem|fr|s|ms)?[\'"]?'
        )

        for m in prop_pattern.finditer(code_part):
            prop_name = m.group(1)
            px_value = m.group(2)
            unit = m.group(3)

            # Only px (or unitless → React defaults to px) is a violation.
            # %, vh/vw, em/rem, fr, s/ms are legitimately not px (e.g.
            # width: '100%' must not be flagged as 100px).
            if unit not in (None, "px"):
                continue

            # Exceptions: 0, 1, 2 (borders/hairlines)
            if int(px_value) in (0, 1, 2):
                continue

            issues.append({
                "line": i,
                "px": f"{px_value}px",
                "property": prop_name,
                "context": line.strip()[:80],
            })

    return issues


# Saturated semantic STROKE tokens used as a TEXT color. --da-color-error
# (#ef4444 = 3.76:1) and --da-color-warning (#f59e0b = 2.15:1) are below the
# WCAG 1.4.3 AA floor (4.5:1) as text on the light-theme page background; the
# design system ships AA-verified `-text` variants (#991b1b / #92400e) for
# exactly this. Only error/warning are flagged: --da-color-info (#2563eb ~5.2:1)
# and -success (#047857 ~5.5:1) already pass as text and have NO `-text` variant.
# The trailing `\)` anchors the BARE token, so `-error-text` / `-error-soft` are
# not matched; the `text-\[` prefix means border-/bg- usages are never flagged
# (saturated is correct for strokes/borders). This is the #885/#904 class.
_SATURATED_TEXT_RE = re.compile(r"text-\[color:var\(--da-color-(error|warning)\)\]")

# (c2) inline / style-object FOREGROUND form — the className regex's blind spot.
# _SATURATED_TEXT_RE only sees the Tailwind `text-[…]` utility; it never catches
# the SAME saturated token fed to a foreground COLOR via an inline style or a
# colors-map literal — e.g. `color: 'var(--da-color-error)'`, or a `text:` map
# entry (the TIER_COLORS / GROUP_COLORS convention) later applied as
# `color: colors.text`. Same WCAG 1.4.3 failure on a light / `-soft` background,
# same fix (`-text` variant), so it feeds the SAME token_issues stream.
#
# TERNARY / CONDITIONAL coverage (the widening — #1074 blind spot). The prior form
# required a quote IMMEDIATELY after `color:`, so it caught only the literal
# `color: 'var(--da-color-error)'` and MISSED the token when it sits inside a
# conditional VALUE, e.g.
#     color: isOutlier ? 'var(--da-color-error)' : 'var(--da-color-tag-fg)'
#     color: over ? 'var(--da-color-error)' : near ? 'var(--da-color-warning)' : '…success'
# The value scan now matches a bare `var(--da-color-(error|warning))` appearing
# ANYWHERE in the foreground property's value — including any branch of a
# ternary/conditional. NOTE (out of scope, tracked in #1075): variable / prop
# INDIRECTION such as `const c = cond ? … ; …; color: c` stays uncaught — this is a
# deliberately line-based regex lint, NOT an AST analysis.
#
# VALUE-BOUNDING (critical false-positive guard, trap 1). The scanned value is
# bounded to the foreground property itself via the `[^,;}\n]` char class: the scan
# stops at the first `,` / `;` / `}` / newline that separates one property from the
# next. So a saturated token on a SIBLING non-foreground property on the SAME line —
# `{ color: 'var(--da-color-tag-fg)', background: 'var(--da-color-error)' }` — is NOT
# mis-attributed to `color:` (the `background` token lives past the comma). A ternary
# value has no top-level comma, so it stays wholly within bounds.
#
# Foreground-key ALLOW-LIST (conservative — NOT "any key"): color / text / textColor.
#   - color     — the canonical CSS/React inline foreground-color property.
#   - text      — the established colors-map foreground key (TIER_COLORS/GROUP_COLORS),
#                 applied downstream as `color: colors.text`.
#   - textColor — common camelCase alias for the same foreground slot.
# DELIBERATELY EXCLUDED because a saturated hue there is CORRECT (a stroke / paint /
# background is not body text on a light bg): background / backgroundColor / bg /
# border / borderColor / stroke, AND `fill`. `fill` is an SVG paint used mostly for
# decorative shapes / status marks where the vivid hue is intended; line-by-line we
# cannot tell an SVG <text> fill from a shape fill, so flagging it would false-
# positive on every status dot / chart mark — it stays out. Key match is case-
# sensitive and prefixed by `(?<![\w-])`, so the `color`/`text` alternatives cannot
# begin mid-identifier: `backgroundColor:` / `borderColor:` (capital C) AND the
# hyphenated CSS forms `background-color:` / `border-color:` / `--brand-color:` (the
# `color` there is preceded by `-`) all fail to match — bare OR inside a ternary.
#
# Anchoring / no-double-flag guards:
#   - trailing `\)` binds the BARE token so `-error-text` / `-error-soft` never match;
#     a MIXED ternary `cond ? 'var(--da-color-error)' : 'var(--da-color-error-text)'`
#     still flags on its bare true-branch. error/warning only: info / success already
#     pass AA as text and ship no `-text` variant.
#   - `(?<!color:)` before `var\(` is the SINGLE no-double-flag guard. It defers to
#     (c1) any `var(` that is glued directly to `color:` — which is exactly the
#     `text-[color:var(--da-color-error)]` Tailwind shape, whether it appears as a raw
#     className OR as a colors-MAP entry whose value is a class STRING
#     (`text: 'text-[color:var(--da-color-error)]'` / `color: 'text-[…] bg-…'`,
#     config-lint SEVERITY_COLORS / summary chips). A genuine inline/ternary value's
#     `var(` is preceded by a quote or space (not `color:`), so it is unaffected.
#     (A front `(?!var\()` was tried but is fully redundant: `\s*` backtracks the space
#     into the value scan, so this lookbehind alone already dedups every className form
#     — mutation-tested, its removal breaks no test.)
_SATURATED_INLINE_TEXT_RE = re.compile(
    r"""(?<![\w-])(?:color|text|textColor)\s*:\s*[^,;}\n]*?(?<!color:)var\(--da-color-(error|warning)\)"""
)


def check_saturated_token_as_text(content: str, filename: str) -> List[Dict]:
    """Scan for a saturated error/warning token used as a FOREGROUND text color.

    Two forms, one WCAG 1.4.3 failure, one fix (the AA-verified ``-text`` variant),
    reported into a single stream:

    (c1) Tailwind className ``text-[color:var(--da-color-error)]`` / ``…-warning)]``.
    (c2) inline style / colors-map foreground literal — a bare
         ``var(--da-color-(error|warning))`` appearing ANYWHERE in the value of a
         foreground key (``color:`` / ``text:`` / ``textColor:``), e.g.
         ``color: 'var(--da-color-error)'``, the ``text:`` entry of a
         TIER_COLORS/GROUP_COLORS map later applied as ``color: c.text``, OR a
         ternary/conditional branch such as
         ``color: isOutlier ? 'var(--da-color-error)' : 'var(--da-color-tag-fg)'``.
         The value is bounded to the property itself (scan stops at the next
         ``,``/``;``/``}``/newline) so a saturated token on a SIBLING non-foreground
         property on the same line is not mis-attributed.

    NOT flagged (a saturated hue is correct there): ``border``/``borderColor``,
    ``background``/``backgroundColor``/``bg``, ``stroke``, ``fill`` (SVG paint), the
    ``-soft`` background and ``-error-text``/``-warning-text`` variants (the fix, even
    as a ternary branch); and the (c1) className is never double-counted by (c2) —
    both the raw ``text-[color:var(…)]`` utility and a colors-map value that is itself
    a class STRING (``text: 'text-[color:var(…)]'``) stay counted once by (c1).
    Variable/prop indirection (``color: c``) is out of scope (#1075) — this is a
    line-based regex lint, not an AST analysis. Honors the same
    ``/* token-exempt */`` marker and comment-stripping as the other checks; only
    error/warning are in scope (info/success already pass AA as text and ship no
    ``-text`` variant).

    Returns list of {line, token, suggestion, context}.
    """
    issues = []
    lines = content.splitlines()
    code_lines = _strip_comments(content)
    for i, line in enumerate(lines, 1):
        if _is_exempt(line):
            continue
        # Scan comment-stripped code so the bad pattern inside a doc comment
        # (e.g. this very docstring quoted elsewhere) is not flagged. Both the
        # className (c1) and inline/foreground (c2) forms feed one stream.
        code_part = code_lines[i - 1]
        for pattern in (_SATURATED_TEXT_RE, _SATURATED_INLINE_TEXT_RE):
            for m in pattern.finditer(code_part):
                token = m.group(1)  # 'error' | 'warning'
                issues.append({
                    "line": i,
                    "token": f"--da-color-{token}",
                    "suggestion": f"--da-color-{token}-text",
                    "context": line.strip()[:80],
                })
    return issues


# (d) Raw Tailwind `slate-*` color classes (dev-rules.md S1). A raw `slate-*`
# utility is hardcoded and does NOT flip in dark mode — `text-slate-600` stays
# slate-600 under `[data-theme="dark"]`, breaking the portal's light/dark
# theming — whereas the `--da-color-*` tokens flip (e.g. --da-color-muted
# #475569 → #94a3b8). The point is "use a token that re-themes", not avoiding
# the slate hue itself (--da-color-muted IS slate-600). A leading utility prefix
# (bg-/text-/border-/…, incl. responsive/state prefixes like `md:`/`hover:` via
# the `(?<![\w-])` boundary) is required so bare words like "translate" or
# "context" never match. Case-sensitive. Shade captured to honor the S1 Waiver.
_SLATE_CLASS_RE = re.compile(
    r"(?<![\w-])(?:bg|text|border|ring|ring-offset|from|to|via|divide|placeholder|"
    r"outline|decoration|accent|caret|fill|stroke)-slate-(\d{2,3})(?![\w-])"
)
# S1 Waiver: the dark IDE / code-preview surface pair (`bg-slate-900` +
# `text-slate-100`) is allowed — those extreme shades ARE the dark chrome and
# don't participate in the light/dark neutral flip. Any other shade on a
# non-exempt line is a finding.
_SLATE_WAIVER_SHADES = {"900", "100"}


def check_hardcoded_slate_classes(content: str, filename: str) -> List[Dict]:
    """Scan for raw Tailwind ``slate-*`` color classes (dev-rules.md S1).

    ``slate-*`` utilities are hardcoded and do not re-theme in dark mode; portal
    neutrals must use theme-aware ``--da-color-*`` tokens (or a ``gray-*`` shade).
    Diff-only by default (see ``scan_jsx_files``), so the pre-existing ~500
    occurrences are grandfathered — this only stops NEW additions ("codified
    beats documented"; S1 previously had no enforcement). The S1 Waiver shades
    ``slate-900`` / ``slate-100`` (dark code-preview chrome) are allowed, as is
    any line carrying ``/* token-exempt */``. Honors the same comment-stripping
    as the other checks (a slate class inside a comment is not flagged).

    Returns list of {line, cls, context}.
    """
    issues = []
    lines = content.splitlines()
    code_lines = _strip_comments(content)
    for i, line in enumerate(lines, 1):
        if _is_exempt(line):
            continue
        code_part = code_lines[i - 1]
        for m in _SLATE_CLASS_RE.finditer(code_part):
            if m.group(1) in _SLATE_WAIVER_SHADES:
                continue
            issues.append({
                "line": i,
                "cls": m.group(0),
                "context": line.strip()[:80],
            })
    return issues


def scan_jsx_files(diff_base: str | None = None) -> Tuple[Dict[str, List[Dict]], Dict[str, List[Dict]], Dict[str, List[Dict]], Dict[str, List[Dict]]]:
    """Scan JSX files for design token violations.

    If ``diff_base`` is None: full-scan all JSX files in JSX_TOOLS_DIR + WIZARD_DIR.
    If ``diff_base`` is a ref: only flag findings on lines ADDED in the current
    diff vs that base. Existing pre-existing violations are not re-emitted.

    Returns (hex_issues, px_issues, token_issues, slate_issues) by file.
    """
    hex_issues = defaultdict(list)
    px_issues = defaultdict(list)
    token_issues = defaultdict(list)
    slate_issues = defaultdict(list)

    jsx_dirs = [JSX_TOOLS_DIR, WIZARD_DIR]

    for jsx_dir in jsx_dirs:
        if not jsx_dir.is_dir():
            continue

        for jsx_file in sorted(jsx_dir.glob("**/*.jsx")):
            # Skip design-tokens.css itself
            if jsx_file.name == "design-tokens.css":
                continue

            try:
                content = jsx_file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

            rel_path = str(jsx_file.relative_to(REPO_ROOT))

            # Run full scan to get all findings
            hex_found = check_hardcoded_hex_colors(content, jsx_file.name)
            px_found = check_hardcoded_px_values(content, jsx_file.name)
            token_found = check_saturated_token_as_text(content, jsx_file.name)
            slate_found = check_hardcoded_slate_classes(content, jsx_file.name)

            # If diff-only, filter findings to lines actually added in current diff
            if diff_base is not None:
                try:
                    added_lines = {ln for ln, _ in get_diff_added_lines(jsx_file, diff_base)}
                except subprocess.CalledProcessError:
                    # Git error — keep all findings (safer than silent suppress)
                    added_lines = None
                if added_lines is not None:
                    hex_found = [h for h in hex_found if h["line"] in added_lines]
                    px_found = [h for h in px_found if h["line"] in added_lines]
                    token_found = [h for h in token_found if h["line"] in added_lines]
                    slate_found = [h for h in slate_found if h["line"] in added_lines]

            if hex_found:
                hex_issues[rel_path] = hex_found
            if px_found:
                px_issues[rel_path] = px_found
            if token_found:
                token_issues[rel_path] = token_found
            if slate_found:
                slate_issues[rel_path] = slate_found

    return dict(hex_issues), dict(px_issues), dict(token_issues), dict(slate_issues)


def _read_pr_body(pr_body_file: str | None) -> str | None:
    """Read PR body from --pr-body-file or $PR_BODY env var."""
    if pr_body_file:
        try:
            return Path(pr_body_file).read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError) as e:
            print(f"WARN: cannot read --pr-body-file {pr_body_file}: {e}", file=sys.stderr)
    return os.environ.get("PR_BODY") or None


# ---------------------------------------------------------------------------
# Main check logic
# ---------------------------------------------------------------------------

def main():
    """Run all design token usage checks."""
    parser = argparse.ArgumentParser(
        description="JSX 設計 token 使用完整性 lint"
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI 模式: 有違規時 exit 1"
    )
    parser.add_argument(
        "--full-scan", action="store_true",
        help="Scan ALL existing violations (default: diff-only — only added lines).",
    )
    parser.add_argument(
        "--diff-base", default=None,
        help="Override diff base (default: $LINT_DIFF_BASE / $GITHUB_BASE_REF / origin/main).",
    )
    parser.add_argument(
        "--pr-body-file", default=None,
        help="Path to file containing PR body for bypass tag check.",
    )
    args = parser.parse_args()

    # Resolve scan mode
    if args.full_scan:
        scan_mode = "full-scan"
        diff_base = None
    else:
        try:
            diff_base = args.diff_base or resolve_diff_base()
        except DiffBaseMissingError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        scan_mode = f"diff vs {diff_base}"

    hex_issues, px_issues, token_issues, slate_issues = scan_jsx_files(diff_base=diff_base)

    all_files = set(hex_issues.keys()) | set(px_issues.keys()) | set(token_issues.keys()) | set(slate_issues.keys())
    total_violations = 0

    if not all_files:
        print(f"✓ 設計 token 使用檢查通過 (mode={scan_mode})。")
        sys.exit(EXIT_OK)

    # Print results grouped by file
    for filename in sorted(all_files):
        print(f"[{filename}]")

        # Hex color violations
        if filename in hex_issues:
            for issue in hex_issues[filename]:
                print(f"  L{issue['line']}: hardcoded hex {issue['hex']} "
                      f"(use --da-color-* token)")
                total_violations += 1

        # PX value violations
        if filename in px_issues:
            for issue in px_issues[filename]:
                print(f"  L{issue['line']}: hardcoded px '{issue['px']}' "
                      f"in {issue['property']} (use --da-space-* or --da-font-size-*)")
                total_violations += 1

        # Saturated stroke token used as a text color (WCAG 1.4.3, #885/#904)
        if filename in token_issues:
            for issue in token_issues[filename]:
                print(f"  L{issue['line']}: saturated var({issue['token']}) used as text color "
                      f"— fails WCAG AA contrast in light theme; use var({issue['suggestion']})")
                total_violations += 1

        # Raw slate-* Tailwind class — not theme-aware (dev-rules S1)
        if filename in slate_issues:
            for issue in slate_issues[filename]:
                print(f"  L{issue['line']}: raw Tailwind '{issue['cls']}' is not theme-aware "
                      f"(dev-rules S1) — use a --da-color-* token (or gray-* shade)")
                total_violations += 1

        print()

    # Summary
    print(f"TOTAL: {total_violations} violation(s) in {len(all_files)} file(s) (mode={scan_mode})")

    # Bypass check (lint-policy.md §4)
    pr_body = _read_pr_body(args.pr_body_file)
    bypass_reason = parse_bypass_tag(pr_body, "design-token-usage")
    if bypass_reason:
        print(
            f"\n⚠️  BYPASSED via PR body: {bypass_reason}\n"
            f"   {total_violations} finding(s) above are author-acknowledged.\n"
            f"   Reviewer must confirm bypass is justified."
        )
        sys.exit(EXIT_OK)

    # Exit with appropriate code
    if args.ci and total_violations > 0:
        print(
            "\nFix: replace hardcoded values with --da-* tokens; for a saturated\n"
            "  error/warning token used as text, switch to its -text variant\n"
            "  (--da-color-error-text / --da-color-warning-text); for a raw\n"
            "  slate-* class use a --da-color-* token (or gray-* shade) so it\n"
            "  re-themes in dark mode (dev-rules S1). OR add /* token-exempt */\n"
            "  on the line if intentional (e.g. on a dark bg).\n"
            "Or add to PR description (per lint-policy.md §4):\n"
            "  bypass-lint: design-token-usage\n"
            "  reason: <≥30 words explaining why this is legitimate>",
        )
        sys.exit(EXIT_VIOLATION)
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
