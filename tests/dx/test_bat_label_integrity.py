"""test_bat_label_integrity.py — structural check for our Windows .bat escape hatches.

Why this test exists
--------------------
PR #44 C5 caught a silent bug in `scripts/ops/win_git_escape.bat`: every
successful command returned rc=1 because the `:done` and `:done_err` labels
were missing from the file. cmd.exe reports `goto :done` against a nonexistent
label as errorlevel=1 but does not echo a fatal error — every caller saw
"FAILED" with no reason. We can't run cmd.exe on CI (Linux), but the bug is
structural: it's always true that if a `goto :X` exists, a `:X` label must
exist in the same file.

What this test does
-------------------
For each Windows .bat file in scripts/ops/:
  1. Collect every label defined (`^:name` at line start).
  2. Collect every `goto :<name>` target.
  3. Assert every goto target has a matching label.

Also asserts that the two escape-hatch files both define `:done` and
`:done_err`, since that's the agreed exit-handling contract.
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BAT_FILES = [
    REPO_ROOT / "scripts" / "ops" / "win_git_escape.bat",
    REPO_ROOT / "scripts" / "ops" / "win_gh.bat",
]

LABEL_RE = re.compile(r"^:([A-Za-z_][A-Za-z0-9_]*)\s*$")
# cmd.exe label dispatch — match `goto :name` (optionally with extra tokens
# before, e.g. inside a parenthetical `if errorlevel 1 goto :done`).
GOTO_RE = re.compile(r"\bgoto\s+:([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def _read_normalized(path: pathlib.Path) -> list[str]:
    """Read a CRLF-authored .bat file as a list of newline-stripped lines."""
    text = path.read_bytes().decode("utf-8", errors="replace")
    # Normalize line endings — .bat files are CRLF but tests run on Linux.
    return text.replace("\r\n", "\n").split("\n")


def _collect_labels(lines: list[str]) -> set[str]:
    labels: set[str] = set()
    for line in lines:
        m = LABEL_RE.match(line)
        if m:
            labels.add(m.group(1).lower())
    return labels


def _collect_goto_targets(lines: list[str]) -> set[str]:
    targets: set[str] = set()
    for line in lines:
        # Skip comment-only lines — REM / :: comments can mention labels
        # in prose without implying a real goto.
        stripped = line.strip()
        if stripped.upper().startswith("REM ") or stripped.startswith("::"):
            continue
        for m in GOTO_RE.finditer(line):
            targets.add(m.group(1).lower())
    return targets


@pytest.mark.parametrize("bat_path", BAT_FILES, ids=lambda p: p.name)
def test_every_goto_has_a_matching_label(bat_path: pathlib.Path) -> None:
    """Every `goto :X` must have a corresponding `:X` label in the same file.

    A missing label causes cmd.exe to silently return errorlevel=1, which
    makes successful commands appear to fail. This was the C5 bug.
    """
    assert bat_path.exists(), f"fixture file missing: {bat_path}"
    lines = _read_normalized(bat_path)
    labels = _collect_labels(lines)
    targets = _collect_goto_targets(lines)
    orphans = sorted(targets - labels)
    assert not orphans, (
        f"{bat_path.name}: goto targets with no matching label: {orphans}. "
        f"Defined labels: {sorted(labels)}."
    )


@pytest.mark.parametrize("bat_path", BAT_FILES, ids=lambda p: p.name)
def test_defines_done_and_done_err(bat_path: pathlib.Path) -> None:
    """Escape-hatch .bat files must define `:done` (success) and `:done_err` (failure).

    This is the agreed contract: every `:do_*` handler ends with either
    `goto :done` (rc=0) or `goto :done_err` (rc=1). If either label is
    missing, the handler's exit code is wrong.
    """
    lines = _read_normalized(bat_path)
    labels = _collect_labels(lines)
    assert "done" in labels, f"{bat_path.name} missing :done label"
    assert "done_err" in labels, f"{bat_path.name} missing :done_err label"


@pytest.mark.parametrize("bat_path", BAT_FILES, ids=lambda p: p.name)
def test_mcp_caller_pattern_documented(bat_path: pathlib.Path) -> None:
    """Header must document the MCP PowerShell cmd-redirect caller pattern.

    Windows-MCP PowerShell callers hang if they invoke the .bat directly via
    the transport's pipe chain. The documented workaround is cmd.exe /c with
    output redirected to a tempfile + Process.Start + WaitForExit(ms). That
    pattern must be discoverable in-tree — require a header comment so
    future maintainers don't reinvent a broken caller each session.
    """
    text = bat_path.read_text(encoding="utf-8", errors="replace")
    # Look in the first ~80 lines for the pattern — it belongs in the header.
    header = "\n".join(text.splitlines()[:80])
    assert "Process.Start" in header, (
        f"{bat_path.name}: MCP caller pattern not documented in header. "
        "Add a Process.Start + WaitForExit example to the REM block."
    )
    assert "WaitForExit" in header, (
        f"{bat_path.name}: WaitForExit not mentioned in header. "
        "The caller pattern is incomplete without it — MCP hangs otherwise."
    )


def test_bat_files_exist() -> None:
    """Sanity — fail loudly if someone renames/moves the .bat files."""
    for p in BAT_FILES:
        assert p.exists(), f"expected .bat file missing: {p}"
