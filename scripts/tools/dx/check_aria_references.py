#!/usr/bin/env python3
"""
Static JSX ARIA reference closure validator (Phase .a0 Day 5 verification).

Purpose
-------
Complete rewrites of wizard JSX files (e.g. cicd-setup-wizard Day 4) introduce
many aria-* attributes. `@babel/parser` parse-check only verifies syntax — it
cannot tell whether:

  * `aria-labelledby={`pack-cat-${cat}`}` has a matching element that renders
    `id={`pack-cat-${cat}`}`
  * `htmlFor={inputId}` has a matching `id={inputId}`
  * `aria-controls={someId}` points at a real element id

This script flags references whose target id is not present anywhere in the
same JSX file. It handles three id shapes:

  1. Static string     id="foo"            → reference "foo"
  2. JSX string expr   id={"foo"}          → reference "foo"
  3. Template literal  id={`prefix-${x}`}  → reference the literal prefix
                                              "prefix-" (heuristic: we treat
                                              any aria ref with the same
                                              literal prefix as matched)

Usage
-----
    python scripts/tools/dx/check_aria_references.py \\
        docs/interactive/tools/cicd-setup-wizard.jsx \\
        docs/interactive/tools/rbac-setup-wizard.jsx \\
        docs/interactive/tools/threshold-heatmap.jsx

Exit code 0 = no dangling references, 1 = found unresolved references.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REF_ATTRS = (
    "aria-labelledby",
    "aria-describedby",
    "aria-controls",
    "aria-owns",
    "htmlFor",
)

_ATTR_NAME_RE = re.compile(r"\b(id|aria-[a-z]+|htmlFor)\s*=")


def strip_frontmatter(src: str) -> str:
    if src.startswith("---"):
        end = src.find("\n---", 3)
        if end != -1:
            return src[end + 4 :]
    return src


def read_jsx_value(src: str, pos: int) -> tuple[str, int] | None:
    """Read a JSX attribute value starting at `pos` (which is just past `=`).

    Returns (raw_value_string, next_pos) or None if unreadable.
    """
    # Skip whitespace
    while pos < len(src) and src[pos].isspace():
        pos += 1
    if pos >= len(src):
        return None
    ch = src[pos]
    if ch == '"' or ch == "'":
        end = src.find(ch, pos + 1)
        if end == -1:
            return None
        return (src[pos : end + 1], end + 1)
    if ch == "{":
        # Balanced brace scan respecting template literals with ${} interpolation.
        # Stack tracks contexts: 'brace' = regular JSX/JS braces,
        # 'template' = inside `...`, 'interp' = inside ${...} of a template.
        stack: list[str] = []
        i = pos
        n = len(src)
        while i < n:
            c = src[i]
            ctx = stack[-1] if stack else None
            if ctx == "template":
                if c == "`":
                    stack.pop()
                elif c == "$" and i + 1 < n and src[i + 1] == "{":
                    stack.append("interp")
                    i += 2
                    continue
                i += 1
                continue
            # brace / interp / start contexts behave similarly
            if c == "`":
                stack.append("template")
            elif c == "{":
                stack.append("brace" if stack else "brace")
            elif c == "}":
                if stack:
                    stack.pop()
                if not stack:
                    return (src[pos : i + 1], i + 1)
            elif c == '"':
                end = src.find('"', i + 1)
                if end == -1:
                    return None
                i = end
            elif c == "'":
                end = src.find("'", i + 1)
                if end == -1:
                    return None
                i = end
            i += 1
        return None
    return None


def classify_attr_value(raw: str) -> tuple[str, str] | None:
    """Return (kind, value) tuple for a raw JSX attribute value.

    kind ∈ {'literal', 'template-prefix', 'dynamic'}.
    """
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return ("literal", raw[1:-1])
    if raw.startswith("'") and raw.endswith("'"):
        return ("literal", raw[1:-1])
    if raw.startswith("{") and raw.endswith("}"):
        inner = raw[1:-1].strip()
        if (inner.startswith('"') and inner.endswith('"')) or (
            inner.startswith("'") and inner.endswith("'")
        ):
            return ("literal", inner[1:-1])
        if inner.startswith("`") and inner.endswith("`"):
            body = inner[1:-1]
            idx = body.find("${")
            if idx == -1:
                return ("literal", body)
            prefix = body[:idx]
            if not prefix:
                return ("dynamic", body)
            return ("template-prefix", prefix)
        return ("dynamic", inner)
    return None


def collect(path: Path):
    src = strip_frontmatter(path.read_text(encoding="utf-8"))
    ids: list[tuple[str, str, int]] = []
    refs: list[tuple[str, str, str, int]] = []
    for m in _ATTR_NAME_RE.finditer(src):
        attr = m.group(1)
        value_start = m.end()
        pair = read_jsx_value(src, value_start)
        if pair is None:
            continue
        raw, _ = pair
        cls = classify_attr_value(raw)
        if cls is None:
            continue
        kind, value = cls
        line = src.count("\n", 0, m.start()) + 1
        if attr == "id":
            ids.append((kind, value, line))
        elif attr in REF_ATTRS:
            refs.append((attr, kind, value, line))
    return ids, refs


def matches_any(ref_kind: str, ref_val: str, ids) -> bool:
    for id_kind, id_val, _ in ids:
        if ref_kind == "literal" and id_kind == "literal":
            if ref_val == id_val:
                return True
        elif ref_kind == "template-prefix":
            if id_kind == "literal" and id_val.startswith(ref_val):
                return True
            if id_kind == "template-prefix" and id_val == ref_val:
                return True
        elif ref_kind == "literal" and id_kind == "template-prefix":
            if ref_val.startswith(id_val):
                return True
    return False


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print("usage: check_aria_references.py <file.jsx> [...]")
        print("\nStatic ARIA id/reference cross-checker for JSX portal tools.")
        print("Exit 0 = clean; 1 = dangling refs found; 2 = usage error.")
        return 0 if len(argv) >= 2 else 2
    overall_ok = True
    for p in argv[1:]:
        path = Path(p)
        ids, refs = collect(path)
        dangling = []
        dynamic_count = 0
        for attr, kind, val, line in refs:
            if kind == "dynamic":
                dynamic_count += 1
                continue
            if not matches_any(kind, val, ids):
                dangling.append((attr, kind, val, line))
        print(f"[{path.name}]")
        print(f"  ids found:              {len(ids)}")
        print(f"  aria refs found:        {len(refs)}")
        print(f"  dynamic (unverifiable): {dynamic_count}")
        print(f"  dangling refs:          {len(dangling)}")
        for attr, kind, val, line in dangling:
            print(f"    !! L{line} {attr} ({kind}) -> {val!r}")
        if dangling:
            overall_ok = False
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
