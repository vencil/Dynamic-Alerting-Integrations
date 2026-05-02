#!/usr/bin/env python3
"""check_playwright_rtl_drift.py — Detect React Testing Library API names in Playwright specs (S#96, mechanical safety net for testing-playbook §LL §10).

Why this exists
---------------
S#94 PR #184 first CI run failed 3 of 4 deep-link spec scenarios with
``TypeError: page.getByDisplayValue is not a function``. Spec author
imported ``getByDisplayValue`` from React Testing Library habit, but
Playwright does NOT have that method. The local ``npm run lint``
(eslint + typescript-eslint + eslint-plugin-playwright) and pre-commit
hooks all passed because TypeScript's strict mode does not exhaustive-
check method-not-found on Playwright's overloaded ``Page`` signatures
— the failure surfaces only at runtime.

testing-playbook §LL §10 codifies the lesson at the read-time layer.
This lint is the commit-time mechanical safety net it deferred.

What it flags
-------------
Four RTL-only ``getBy*`` API names appearing as method calls on a
``page.`` / ``locator.`` / ``frameLocator.`` / ``component.`` receiver
in Playwright spec files (``tests/e2e/**/*.spec.ts``):

    page.getByDisplayValue(...)         # RTL only
    page.getByLabelText(...)            # RTL — Playwright is getByLabel
    page.getByPlaceholderText(...)      # RTL — Playwright is getByPlaceholder
    page.getByAltText(...)              # NOTE: actually allowed, see below

Note on getByAltText: Playwright DOES have ``getByAltText`` (added in
1.27). RTL also has it. Same name, same intent, both work — we do NOT
flag it. The lint targets only the three RTL methods that don't exist
on Playwright.

Severity model
--------------
``--ci`` exit 1 on any finding. Default mode prints findings but
exits 0 (warn-only) — useful for `git ls-files` style audits.

Per-line ignore
---------------
Append ``// playwright-rtl-drift: ignore`` to a line, OR add it on a
line up to 3 lines above (covers multi-line comment blocks). Used for:

    // playwright-rtl-drift: ignore — discussing the RTL API in docstring
    /**
     * Why we don't use page.getByDisplayValue: ...
     */

References
----------
- testing-playbook.md §v2.8.0 LL §10 (read-time SOT)
- PR #184 first-CI-fail commit ``912cf2b`` (real-world incident)
- PR #185 LL §10 codification
- Playwright docs: https://playwright.dev/docs/locators
- React Testing Library docs: https://testing-library.com/docs/queries/about/
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Per-line ignore marker — use a TS-comment-friendly form so a spec
# author can drop it inline without breaking syntax. Lookback covers
# multi-line rationale blocks like JSDoc.
_IGNORE_MARKER = "playwright-rtl-drift: ignore"
_IGNORE_LOOKBACK_LINES = 3

# RTL-only getBy* names that do NOT exist on Playwright's Page /
# Locator / FrameLocator / Component objects. NOT including getByAltText
# because Playwright has the same-named method (since 1.27).
_RTL_ONLY_METHODS = (
    "getByDisplayValue",
    "getByLabelText",
    "getByPlaceholderText",
)

# Receiver-prefix regex: matches the dot-notation invocation
# ``<receiver>.<method>(`` where receiver is a typical Playwright
# locator handle. We don't try to enumerate every receiver name —
# anchoring on ``.<method>(`` with any preceding word works for the
# patterns spec authors actually write. Compiled per-method below.
_INVOCATION_RE_TEMPLATE = r"\b\w+\.{method}\s*\("

_INVOCATION_PATTERNS = {
    method: re.compile(_INVOCATION_RE_TEMPLATE.format(method=method))
    for method in _RTL_ONLY_METHODS
}


@dataclass
class Finding:
    """A single RTL-method invocation flagged by this lint."""

    path: Path
    line: int
    col: int
    method: str
    snippet: str

    def render(self) -> str:
        if self.path.is_absolute():
            try:
                rel = self.path.relative_to(PROJECT_ROOT)
            except ValueError:
                rel = self.path
        else:
            rel = self.path
        return (
            f"{rel}:{self.line}:{self.col} [{self.method}] {self.snippet[:120]}"
        )


def _line_has_ignore(source_lines: list[str], line_no: int) -> bool:
    """True if line_no OR up to 3 lines above contain the ignore marker."""
    for offset in range(_IGNORE_LOOKBACK_LINES + 1):
        candidate = line_no - offset
        if 1 <= candidate <= len(source_lines):
            if _IGNORE_MARKER in source_lines[candidate - 1]:
                return True
    return False


# JSDoc body lines start with optional whitespace + `*` followed by
# space or end-of-line. Single-line `//` comments also count.
_COMMENT_LINE_RE = re.compile(r"^\s*(?:\*|//)")


def _is_comment_line(line: str) -> bool:
    """True if line is a TS line-comment (`// ...`) or JSDoc body (` * ...`).

    We detect comment lines because spec files frequently document the
    very RTL APIs we want to flag — `page.getByDisplayValue` appears in
    docstrings discussing why we don't use it. Flagging those is a
    false positive that wastes reviewer time. A `// playwright-rtl-drift:
    ignore` marker is the explicit escape hatch; comment-line skipping
    is the implicit default for the common case.
    """
    return bool(_COMMENT_LINE_RE.match(line))


def _strip_inline_comment(line: str) -> str:
    """Strip trailing `// ...` and `/* ... */` from a code line.

    Keeps the leading code (which may legitimately call a banned method)
    and discards the trailing comment (which may discuss the API). For
    block-comment opens without a close on the same line we conservatively
    return the line as-is — multi-line block comments are rare in spec
    files outside JSDoc, which `_is_comment_line` already handles.
    """
    # Inline `// comment` — only the first occurrence outside a string.
    # We don't try to perfectly parse TS string literals; spec files
    # rarely have `//` inside backticks.
    line_no_inline = re.sub(r"//.*$", "", line)
    # `/* ... */` on a single line.
    line_no_inline = re.sub(r"/\*.*?\*/", "", line_no_inline)
    return line_no_inline


def _is_in_backtick_span(stripped_line: str, col: int) -> bool:
    """True if 1-based column is inside an odd number of backticks.

    Used to skip ``page.getByDisplayValue(...)`` literals inside
    code-span backticks — those are documentation, not calls.
    """
    pre = stripped_line[: col - 1]
    return pre.count("`") % 2 == 1


def scan_source(path: Path, source: str) -> list[Finding]:
    """Walk a Playwright spec file and return all RTL-drift findings.

    Three layers of false-positive suppression:
      1. Skip lines (and 3-line lookback) carrying the ignore marker
      2. Skip TS line-comments (`// ...`) and JSDoc body (` * ...`)
      3. Skip matches inside inline backtick code spans

    Within a code line, also strip the trailing `// comment` portion
    before pattern-matching, so a real call followed by a comment
    discussing the RTL alternative still flags only the real call.
    """
    findings: list[Finding] = []
    lines = source.splitlines()

    for idx, line in enumerate(lines, start=1):
        if _line_has_ignore(lines, idx):
            continue
        if _is_comment_line(line):
            continue

        # Strip trailing inline comment to avoid flagging discussion
        # text that follows a real call.
        scan_target = _strip_inline_comment(line)

        for method, pattern in _INVOCATION_PATTERNS.items():
            for match in pattern.finditer(scan_target):
                # Column points at the method name, not the receiver,
                # so the user's eye lands on the offending token.
                method_offset_in_match = match.group(0).find(method)
                col = match.start() + method_offset_in_match + 1
                # Skip backtick code-spans — documentation, not calls.
                if _is_in_backtick_span(scan_target, col):
                    continue
                findings.append(
                    Finding(
                        path=path,
                        line=idx,
                        col=col,
                        method=method,
                        snippet=line.strip(),
                    )
                )
    return findings


def _resolve_target_paths(args: argparse.Namespace) -> list[Path]:
    """Resolve --paths args, falling back to all Playwright spec files."""
    if args.paths:
        return [
            Path(p) if Path(p).is_absolute() else PROJECT_ROOT / p
            for p in args.paths
        ]
    spec_dir = PROJECT_ROOT / "tests" / "e2e"
    if not spec_dir.is_dir():
        return []
    return sorted(spec_dir.rglob("*.spec.ts"))


def _compute_exit_code(*, ci: bool, n_findings: int) -> int:
    """Pure helper for severity routing.

    | --ci  | n_findings | exit |
    |-------|------------|------|
    | False | *          | 0    |
    | True  | 0          | 0    |
    | True  | >0         | 1    |
    """
    if not ci:
        return 0
    return 1 if n_findings > 0 else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detect React Testing Library API names "
            "(getByDisplayValue / getByLabelText / getByPlaceholderText) "
            "in Playwright specs. Mechanical safety net for "
            "testing-playbook §LL §10 (PR #184 case)."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help=(
            "Files to scan. Defaults to all tests/e2e/**/*.spec.ts when "
            "omitted (full repo audit)."
        ),
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Exit 1 on findings (default: report only).",
    )
    args = parser.parse_args(argv)

    paths = _resolve_target_paths(args)
    if not paths:
        if args.ci:
            print("OK no Playwright spec files matched scan target")
        return 0

    all_findings: list[Finding] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"WARN cannot read {path}: {exc}", file=sys.stderr)
            continue
        all_findings.extend(scan_source(path, source))

    if not all_findings:
        if args.ci:
            print(f"OK no RTL-drift across {len(paths)} spec file(s)")
        return 0

    print(
        f"FAIL {len(all_findings)} RTL-drift finding(s) in "
        f"{len({f.path for f in all_findings})} file(s):",
        file=sys.stderr,
    )
    for f in all_findings:
        print(f"  {f.render()}", file=sys.stderr)
    print(
        "\nFix: replace with the Playwright-native equivalent.\n"
        "  getByDisplayValue(x)    -> page.evaluate(...) over input.value\n"
        "                             OR await locator.inputValue() / toHaveValue(x)\n"
        "  getByLabelText(x)       -> page.getByLabel(x)\n"
        "  getByPlaceholderText(x) -> page.getByPlaceholder(x)\n"
        "\nSee testing-playbook.md §v2.8.0 LL §10 for the canonical evaluate pattern.\n"
        "Per-line escape: append `// playwright-rtl-drift: ignore` "
        "(3-line lookback for JSDoc).",
        file=sys.stderr,
    )

    return _compute_exit_code(ci=args.ci, n_findings=len(all_findings))


if __name__ == "__main__":
    raise SystemExit(main())
