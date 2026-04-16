#!/usr/bin/env python3
"""
Axe-lite: static WCAG heuristics for JSX files (Phase .a0 Day 5 verification).

Not a replacement for @axe-core/playwright — axe-core needs a real browser,
and the Cowork VM network allowlist blocks Chromium download. These static
heuristics are a stopgap that can run in pre-commit / CI without a browser.
Full axe-core is still gated behind CI (documented in planning §19 Day 5).

Checks performed (all conservative — only flag near-certain violations):

  A) WCAG 1.4.1 "Use of Color": Unicode status symbols (✓ ⚠ ❌ ✗ ⓘ) inside
     JSX expressions MUST be either:
       * wrapped in an element with aria-hidden (visual-only decoration), OR
       * the only child of an element with aria-label (described textually)

  B) WCAG 4.1.2 "Name, Role, Value": <button> elements with neither text
     content, aria-label, aria-labelledby, nor title.

  C) WCAG 3.3.2 "Labels or Instructions": <input type="text"|"email"|"number">
     and <textarea> with no label/aria-label/aria-labelledby/placeholder.

  D) WCAG 1.4.1 complementary: class strings that encode severity ONLY via
     color tokens (e.g. `text-[color:var(--da-color-error)]`) without also
     using a non-color channel (border-*, underline, Unicode symbol, bold,
     or aria-describedby) on the same element.

Exit 0 = clean; 1 = violations found.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


UNICODE_STATUS = "✓✔⚠❌✗ⓘ"

# Color-only severity tokens that the WCAG 1.4.1 check looks for.
SEVERITY_COLOR_TOKENS = (
    "var(--da-color-error)",
    "var(--da-color-warning)",
    "var(--da-color-success)",
)


def strip_frontmatter(src: str) -> str:
    if src.startswith("---"):
        end = src.find("\n---", 3)
        if end != -1:
            return src[end + 4 :]
    return src


def scan_unicode_status(src: str) -> list[tuple[int, str]]:
    """Walk every element immediately enclosing a Unicode status symbol and
    require the IMMEDIATE enclosing tag (or any ancestor inside the same line
    window) to have aria-hidden or aria-label.

    This is the most important WCAG 1.4.1 check for the colorblind hotfix.
    We walk backward from each symbol occurrence to find the nearest `<tag`
    open and check its attributes up to the matching `>`.
    """
    out: list[tuple[int, str]] = []
    for i, ch in enumerate(src):
        if ch not in UNICODE_STATUS:
            continue
        # Walk backwards to find the nearest `<tag` open
        k = i
        while k > 0:
            k -= 1
            if src[k] == "<" and (
                src[k + 1 : k + 2].isalpha()
                or src[k + 1 : k + 2] in ("_",)
            ):
                break
            if src[k] == ">":
                # passed a close; give up on backward scan
                k = -1
                break
        if k <= 0:
            continue
        # Skip if this symbol is inside a comment/string — approximate by
        # checking the opening `<` is at col 0 of text content (not inside
        # attribute value). A cheap way: require the intervening `>` exists
        # before the symbol.
        open_end = _find_opening_tag(src, k)
        if not open_end:
            continue
        close_gt, attrs = open_end
        if close_gt > i:
            # symbol is inside the opening tag attributes — ignore
            continue
        if "aria-hidden" in attrs or "aria-label" in attrs or "aria-labelledby" in attrs:
            continue
        # Also allow if immediate parent sibling has it (the symbol might be
        # a direct child of a labeled container).
        parent_start = max(0, k - 500)
        parent_window = src[parent_start : k + 1]
        if re.search(
            r"aria-label\s*=|aria-labelledby\s*=|aria-hidden",
            parent_window[-400:],
        ):
            continue
        line = src.count("\n", 0, i) + 1
        out.append(
            (line, f"status symbol {ch!r} not in aria-hidden/aria-label scope")
        )
    return out


def scan_buttons_without_name(src: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    # Find `<button` tokens; use balanced-tag parser to get the opening tag
    # attributes and the tag body.
    for m in re.finditer(r"<button\b", src):
        open_end = _find_opening_tag(src, m.start())
        if not open_end:
            continue
        open_idx_end, attrs = open_end
        close_idx = src.find("</button>", open_idx_end)
        if close_idx == -1:
            continue
        body = src[open_idx_end:close_idx]
        if (
            "aria-label" in attrs
            or "aria-labelledby" in attrs
            or "title=" in attrs
        ):
            continue
        # Accept children with aria-hidden stripped; also accept any text coming
        # from a JSX expression (e.g. {t('Add', 'Add')}) as potential a11y name.
        text_like = re.sub(
            r"<(span|i|img|svg|strong|em)\b[^>]*aria-hidden[^>]*>.*?</\1>",
            "",
            body,
            flags=re.DOTALL,
        )
        # If body contains ANY JSX expression {...} treat as accessible
        # (runtime string). This reduces false positives for i18n patterns.
        if re.search(r"\{[^{}]+\}", text_like):
            continue
        plain = re.sub(r"<[^>]+>", "", text_like).strip()
        if plain:
            continue
        line = src.count("\n", 0, m.start()) + 1
        out.append((line, "button has no accessible name"))
    return out


def _find_opening_tag(src: str, start: int) -> tuple[int, str] | None:
    """Find the end of a JSX opening tag starting at `start` (which is `<`).

    Returns (end_index_exclusive, attrs_text). Respects `{...}` expressions
    (which can contain `>` inside arrow functions) and string literals.
    """
    i = start + 1
    n = len(src)
    # Skip tag name
    while i < n and (src[i].isalnum() or src[i] in ("-", "_", ".", ":")):
        i += 1
    attrs_start = i
    brace_depth = 0
    while i < n:
        c = src[i]
        if c == "{":
            brace_depth += 1
        elif c == "}":
            if brace_depth > 0:
                brace_depth -= 1
        elif c in ('"', "'") and brace_depth == 0:
            end = src.find(c, i + 1)
            if end == -1:
                return None
            i = end
        elif c == ">" and brace_depth == 0:
            return (i + 1, src[attrs_start:i])
        i += 1
    return None


def scan_unlabeled_inputs(src: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for tag in ("input", "textarea"):
        pattern = re.compile(r"<" + tag + r"\b")
        for m in pattern.finditer(src):
            parsed = _find_opening_tag(src, m.start())
            if not parsed:
                continue
            _, attrs = parsed
        if "type=" in attrs and re.search(
            r'type\s*=\s*["\'](hidden|submit|button|checkbox|radio)["\']', attrs
        ):
            continue
        has_label_hint = any(
            a in attrs
            for a in (
                "aria-label",
                "aria-labelledby",
                "placeholder",
                "title=",
            )
        )
        if has_label_hint:
            continue
        # Element MAY be labeled by a sibling <label htmlFor=...>. We use a
        # coarse check: if the same file has a <label htmlFor="X"> AND this
        # element has id="X", call it labeled.
        id_m = re.search(r'\bid\s*=\s*["\']([^"\']+)["\']', attrs)
        if id_m:
            the_id = id_m.group(1)
            if re.search(
                r'<label\b[^>]*htmlFor\s*=\s*["\']' + re.escape(the_id) + r'["\']',
                src,
            ):
                continue
            # Also accept {id} template form
            if re.search(
                r'<label\b[^>]*htmlFor\s*=\s*\{[^}]*' + re.escape(the_id),
                src,
            ):
                continue
        line = src.count("\n", 0, m.start()) + 1
        out.append((line, f"<{tag}> has no accessible label"))
    return out


def scan_color_only_severity(src: str) -> list[tuple[int, str]]:
    """Flag elements whose className uses a severity color token but does NOT
    provide a non-color channel. This is WCAG 1.4.1."""
    out: list[tuple[int, str]] = []
    # Look at every className="..." or className={`...`} that contains a
    # severity color token.
    for m in re.finditer(
        r'className\s*=\s*(?:["\']([^"\'\n]+)["\']|\{`([^`]+)`\})',
        src,
    ):
        klass = m.group(1) or m.group(2) or ""
        if not any(tok in klass for tok in SEVERITY_COLOR_TOKENS):
            continue
        # Non-color signals we accept: border, underline, font-bold, ring-,
        # symbol character present in immediate neighborhood, aria-describedby.
        non_color_ok = any(
            marker in klass
            for marker in (
                "border-",
                "underline",
                "font-bold",
                "ring-",
                "italic",
                "line-through",
            )
        )
        # Walk ± 200 chars around the match for context signals.
        window_start = max(0, m.start() - 200)
        window_end = min(len(src), m.end() + 200)
        window = src[window_start:window_end]
        if not non_color_ok:
            if any(c in window for c in UNICODE_STATUS):
                non_color_ok = True
            elif "aria-describedby" in window:
                non_color_ok = True
            elif "role=\"alert\"" in window or "role='alert'" in window:
                non_color_ok = True
        if non_color_ok:
            continue
        line = src.count("\n", 0, m.start()) + 1
        out.append(
            (
                line,
                "severity color without non-color signal (border/underline/symbol/alert)",
            )
        )
    return out


def check_file(path: Path) -> int:
    src = strip_frontmatter(path.read_text(encoding="utf-8"))
    print(f"[{path.name}]")
    total = 0
    for label, fn in (
        ("A/1.4.1 unicode-status", scan_unicode_status),
        ("B/4.1.2 button-name", scan_buttons_without_name),
        ("C/3.3.2 input-label", scan_unlabeled_inputs),
        ("D/1.4.1 color-only-severity", scan_color_only_severity),
    ):
        findings = fn(src)
        total += len(findings)
        print(f"  {label:30s} {len(findings):3d}")
        for line, msg in findings[:10]:
            print(f"    !! L{line}: {msg}")
        if len(findings) > 10:
            print(f"    ... and {len(findings) - 10} more")
    return total


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: axe_lite_static.py <file.jsx> [...]", file=sys.stderr)
        return 2
    bad = 0
    for p in argv[1:]:
        bad += check_file(Path(p))
    print(f"\nTOTAL findings across {len(argv) - 1} file(s): {bad}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
