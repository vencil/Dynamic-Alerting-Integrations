#!/usr/bin/env python3
"""check_playwright_coldstart_drift.py — Detect Playwright spec downstream-state testid assertions without preceding input establishment (S#97 Tier 1 mechanical net for testing-playbook §LL §11).

Why this exists
---------------
S#95 PR #185 first CI run failed 3 of 5 spec scenarios with
`simulate-preview-state-ready` / `-state-error` testids never visible
because the widget rendered `state-empty` indefinitely. Root cause:
spec assumed cold-start auto-fire on mount, but component default
state did not satisfy the canSimulate gate (`tenantId: ''` cold-start).

testing-playbook §LL §11 codifies the read-time discipline. This lint
is the Tier 1 commit-time mechanical safety net it identified — text
heuristic (no AST), targets the specific signal that precedes the
class of failure: ``expect(getByTestId(/-state-(ready|success|...))).
toBeVisible()`` appearing in a test block with no preceding input
establishment (fill/click/goto-with-params/route/etc.).

What it flags
-------------
For each ``test('...', async ({ page }) => { ... })`` block in
``tests/e2e/**/*.spec.ts``:

  1. Find every ``await expect(page.getByTestId('X')).toBeVisible()``
     where X matches the downstream-state pattern:
       ``*-state-(ready|success|loaded|error|fail)``
       ``*-(result|output|preview)-*``

  2. Walk lines BEFORE that assertion within the same test block.
     If NONE contain an input establishment call:
       - ``*.fill(``, ``*.click(``, ``*.dispatchEvent(``,
         ``*.selectOption(``, ``*.setInputFiles(``
       - ``page.goto(<url with query params>)``
       - ``page.route(``
     → flag as cold-start drift candidate.

Why those input establishments
------------------------------
- ``fill / click / dispatchEvent / selectOption / setInputFiles``: user
  input ⇒ component state mutates ⇒ canRender gate flips
- ``page.goto(?...=)``: URL query params often pre-fill state at mount
  (S#94 `?tenant_id=` deep-link pattern)
- ``page.route(``: mock fetch ⇒ component network response triggers
  state transition (the "input" is the network response, not user)

If the spec is genuinely testing a component whose cold-start state
already satisfies the render gate (e.g. alert-builder default
``groupName: 'my-alerts'``, simulate-preview default
``tenantId: 'example-tenant'``), use the auto-fire marker:

    test('auto-fires on mount with default inputs', async ({ page }) => {
      // playwright-coldstart: auto-fire — verified default tenantId triggers
      await loadPortalTool(page, 'simulate-preview');
      await expect(page.getByTestId('simulate-preview-state-ready')).toBeVisible();
    });

The marker is an assertion that the spec author has READ the component
source and verified the default state suffices — review checklist
catches the "just trusted my hunch" path.

Severity model
--------------
``--ci`` exit 1 on any finding.
Default mode prints findings but exits 0 (warn-only ship per S#96
pattern). Strict promotion candidate after 1 audit cycle.

Per-line escapes
----------------
- ``// playwright-coldstart: auto-fire`` — explicit assertion that
  cold-start satisfies render gate. Use sparingly; verify component
  source.
- ``// playwright-coldstart: ignore`` — general escape for residual
  edge cases (3-line lookback for JSDoc).

References
----------
- testing-playbook.md §v2.8.0 LL §11 (read-time SOT + 3-tier analysis)
- PR #185 first-CI-fail commit ``3beb127`` (real-world incident)
- PR #186 LL §11 codification + 3-tier feasibility analysis
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Markers — both at the end of a line, with 3-line JSDoc lookback.
_AUTO_FIRE_MARKER = "playwright-coldstart: auto-fire"
_IGNORE_MARKER = "playwright-coldstart: ignore"
_LOOKBACK_LINES = 3

# Downstream-state testid: matches getByTestId('X') where X ends in
# `-state-(ready|success|loaded|error|fail)`. We deliberately do NOT
# match `-result` / `-output` / `-preview` as suffixes — `preview` is
# an actual tool name in this repo (`simulate-preview-tenant-id` is an
# input testid, not a state testid), and the false-positive risk
# outweighs the rare case where a spec author uses those words as
# pure state markers. Empty / loading / initial states are NOT flagged
# — those legitimately render on mount. Extend later if a real
# component evolves a different state-marker convention.
_DOWNSTREAM_TESTID_RE = re.compile(
    r"""getByTestId\s*\(\s*['"]
        [^'"]*?                                  # any prefix
        -state-(?:ready|success|loaded|error|fail)
        ['"]
        \s*\)
    """,
    re.VERBOSE,
)

# Assertion line pattern — must wrap a getByTestId call we just matched.
_TO_BE_VISIBLE_RE = re.compile(r"\.toBeVisible\s*\(")

# Input establishment patterns (any one in the test block before the
# downstream-state assertion suppresses the warning).
_INPUT_PATTERNS = [
    re.compile(r"\.fill\s*\("),
    re.compile(r"\.click\s*\("),
    re.compile(r"\.dispatchEvent\s*\("),
    re.compile(r"\.selectOption\s*\("),
    re.compile(r"\.setInputFiles\s*\("),
    re.compile(r"\.check\s*\("),       # checkbox / radio
    re.compile(r"\.uncheck\s*\("),
    re.compile(r"\.press\s*\("),       # keyboard
    re.compile(r"\.type\s*\("),
    # page.goto with query params (the ?key=value form is the signal).
    re.compile(r"page\.goto\s*\(\s*['\"`][^'\"`]*\?\w+="),
    re.compile(r"page\.route\s*\("),
    # page.evaluate is unconventional but DOES count — it can push state
    # directly via window.postMessage / store dispatch. Conservative.
    re.compile(r"page\.evaluate\s*\("),
]

# Test-block boundary: the spec opens with `test(` (not `test.describe`).
# We treat each top-level `test(` as the start; the next `test(` line OR
# EOF closes it. Heuristic — nested test() (rare) collapses to the
# outer block; that only ADDS context (more lines for input
# establishment search), so no false-flag risk from nesting.
_TEST_BLOCK_START_RE = re.compile(r"^\s*(?:await\s+)?test(?:\.skip|\.only)?\s*\(")

# JSDoc body / line comments — skip from pattern matching to avoid
# flagging documentation that mentions getByTestId in a docstring.
_COMMENT_LINE_RE = re.compile(r"^\s*(?:\*|//)")


@dataclass
class Finding:
    """A single cold-start drift candidate."""

    path: Path
    line: int       # line of the .toBeVisible() assertion
    col: int        # col of getByTestId within that line
    testid: str     # the matched testid string
    test_block_start: int  # 1-based line of the test() call
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
            f"{rel}:{self.line}:{self.col} [coldstart-drift] "
            f"testid={self.testid!r} block@L{self.test_block_start} "
            f"{self.snippet[:100]}"
        )


@dataclass
class _TestBlock:
    """A test-block view used during scanning."""

    start_line: int                     # 1-based
    end_line: int                       # 1-based, inclusive
    has_input_establishment: bool = False
    auto_fire_marker_seen: bool = False


def _line_has_marker(lines: list[str], line_no: int, marker: str) -> bool:
    """True if line_no OR up to 3 lines above contain the marker."""
    for offset in range(_LOOKBACK_LINES + 1):
        candidate = line_no - offset
        if 1 <= candidate <= len(lines):
            if marker in lines[candidate - 1]:
                return True
    return False


def _is_comment_line(line: str) -> bool:
    """True if line is a TS line-comment or JSDoc body line."""
    return bool(_COMMENT_LINE_RE.match(line))


def _strip_inline_comment(line: str) -> str:
    """Drop trailing `// ...` / `/* ... */` to keep code-only payload."""
    line2 = re.sub(r"//.*$", "", line)
    line2 = re.sub(r"/\*.*?\*/", "", line2)
    return line2


def _identify_test_blocks(lines: list[str]) -> list[_TestBlock]:
    """Walk the file and split into test() blocks."""
    starts = [
        idx
        for idx, line in enumerate(lines, start=1)
        if _TEST_BLOCK_START_RE.match(line) and "test.describe" not in line
    ]
    blocks: list[_TestBlock] = []
    for i, start in enumerate(starts):
        end = (starts[i + 1] - 1) if i + 1 < len(starts) else len(lines)
        blocks.append(_TestBlock(start_line=start, end_line=end))
    return blocks


def _scan_block_for_inputs_and_marker(
    lines: list[str], block: _TestBlock
) -> None:
    """Mutate block flags based on lines [start_line, end_line]."""
    for ln in range(block.start_line, block.end_line + 1):
        line = lines[ln - 1]
        if _AUTO_FIRE_MARKER in line:
            block.auto_fire_marker_seen = True
        # Input establishment — strip inline comments so a doc string
        # mentioning `.fill(` doesn't count.
        if _is_comment_line(line):
            continue
        scan_target = _strip_inline_comment(line)
        for pat in _INPUT_PATTERNS:
            if pat.search(scan_target):
                block.has_input_establishment = True
                # Don't break — we still need to continue scanning for
                # the marker (might appear after the input).


def scan_source(path: Path, source: str) -> list[Finding]:
    """Walk a Playwright spec file and return cold-start drift findings.

    Algorithm:
      1. Identify all test() blocks (start lines).
      2. For each block, scan body to set has_input_establishment +
         auto_fire_marker_seen flags.
      3. For each line in the block carrying a downstream-state testid
         assertion AND a .toBeVisible() invocation, emit a finding
         UNLESS the block has input establishment OR auto-fire marker
         OR the assertion line has the ignore marker.
    """
    findings: list[Finding] = []
    lines = source.splitlines()
    blocks = _identify_test_blocks(lines)

    for block in blocks:
        _scan_block_for_inputs_and_marker(lines, block)

        # Block-level escape — input establishment OR auto-fire marker
        # exempt the entire block from cold-start drift checks.
        if block.has_input_establishment or block.auto_fire_marker_seen:
            continue

        # Walk block looking for the offending assertion.
        for ln in range(block.start_line, block.end_line + 1):
            line = lines[ln - 1]
            if _is_comment_line(line):
                continue
            if _line_has_marker(lines, ln, _IGNORE_MARKER):
                continue

            scan_target = _strip_inline_comment(line)
            testid_match = _DOWNSTREAM_TESTID_RE.search(scan_target)
            if not testid_match:
                continue
            if not _TO_BE_VISIBLE_RE.search(scan_target):
                continue

            # Extract the testid value for the user-friendly message.
            testid_str_match = re.search(
                r"getByTestId\s*\(\s*['\"]([^'\"]+)['\"]", scan_target
            )
            testid_str = (
                testid_str_match.group(1) if testid_str_match else "?"
            )
            col = testid_match.start() + 1

            findings.append(
                Finding(
                    path=path,
                    line=ln,
                    col=col,
                    testid=testid_str,
                    test_block_start=block.start_line,
                    snippet=line.strip(),
                )
            )

    return findings


def _resolve_target_paths(args: argparse.Namespace) -> list[Path]:
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
    """Severity matrix — same as PR #186 RTL drift lint."""
    if not ci:
        return 0
    return 1 if n_findings > 0 else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detect Playwright spec downstream-state testid assertions "
            "without preceding input establishment. Mechanical Tier 1 "
            "safety net for testing-playbook §LL §11 (PR #185 case)."
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
            print(
                f"OK no cold-start drift across {len(paths)} spec file(s)"
            )
        return 0

    print(
        f"WARN {len(all_findings)} cold-start drift finding(s) in "
        f"{len({f.path for f in all_findings})} file(s):",
        file=sys.stderr,
    )
    for f in all_findings:
        print(f"  {f.render()}", file=sys.stderr)
    print(
        "\nFix one of:\n"
        "  1. Add an input establishment BEFORE the assertion in the\n"
        "     same test block: `await page.getByTestId('X').fill(...)`\n"
        "     OR `await page.click(...)` OR navigate with query params\n"
        "     `page.goto('?...=v')` OR mock fetch `page.route(...)`.\n"
        "  2. If component cold-start state genuinely satisfies the\n"
        "     render gate (verified by reading component useState), add\n"
        "     `// playwright-coldstart: auto-fire` to the test body.\n"
        "  3. Per-line escape `// playwright-coldstart: ignore`\n"
        "     (3-line lookback for JSDoc) for residual edge cases.\n"
        "\nSee testing-playbook.md §v2.8.0 LL §11 for full rationale.",
        file=sys.stderr,
    )

    return _compute_exit_code(ci=args.ci, n_findings=len(all_findings))


if __name__ == "__main__":
    raise SystemExit(main())
