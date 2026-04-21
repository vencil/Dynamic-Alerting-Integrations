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
# Primary MCP-caller wrappers with goto/label structure + documented caller
# pattern. These go through the full structural suite below.
BAT_FILES = [
    REPO_ROOT / "scripts" / "ops" / "win_git_escape.bat",
    REPO_ROOT / "scripts" / "ops" / "win_gh.bat",
]
# All .bat under scripts/ops/ that can be invoked by Desktop Commander /
# Windows-MCP start_process. These get the narrower ASCII/CRLF/BOM gate
# (pitfall #45 + pitfall row #2) but not the goto/label + caller-pattern
# checks that only apply to the two escape-hatch wrappers above.
ALL_OPS_BAT_FILES = sorted(
    (REPO_ROOT / "scripts" / "ops").glob("*.bat")
)

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
    # Accept either the C# form `Process.Start` or the PowerShell form
    # `[Diagnostics.Process]::Start` — both describe the same API.
    assert re.search(r"Process[\]\.:]+Start", header), (
        f"{bat_path.name}: MCP caller pattern not documented in header. "
        "Add a Process.Start / [Diagnostics.Process]::Start example to the REM block."
    )
    assert "WaitForExit" in header, (
        f"{bat_path.name}: WaitForExit not mentioned in header. "
        "The caller pattern is incomplete without it — MCP hangs otherwise."
    )
    # CreateNoWindow and /s /c are the two non-obvious pieces we dogfooded
    # (PR #44 C5 close-loop). Without CreateNoWindow MCP still inherits the
    # child console handle. Without /s the quoted-args dance is fragile.
    assert "CreateNoWindow" in header, (
        f"{bat_path.name}: header must mention CreateNoWindow=$true. "
        "Without it the MCP transport still inherits the child console "
        "and WaitForExit silently times out."
    )
    assert "/s /c" in header, (
        f"{bat_path.name}: header must show the `cmd.exe /s /c` invocation. "
        "The /s flag is what makes cmd.exe strip the outer quotes cleanly."
    )


def test_bat_files_exist() -> None:
    """Sanity — fail loudly if someone renames/moves the .bat files."""
    for p in BAT_FILES:
        assert p.exists(), f"expected .bat file missing: {p}"


# ---------------------------------------------------------------------------
# Pitfall #45 — Desktop Commander start_process mangles .bat with CJK bytes.
#
# cmd.exe's batch parser reads byte-by-byte and does NOT normalize UTF-8
# multi-byte sequences. When Desktop Commander's start_process launches a
# .bat, it spawns a child cmd.exe that inherits the parent OEM codepage
# (typically cp950 / cp437 on zh-TW Windows) — NOT cp65001. Any byte ≥ 0x80
# in a REM comment or string can land on what the parser treats as a
# shell metacharacter (0x80–0xBF covers several cp1252 punctuation bytes)
# and corrupts parser state on downstream lines.
#
# The symptom is that @echo off / setlocal / goto appear to "not exist"
# on lines that came AFTER the CJK one — the corruption leaks downstream.
# `cmd /c` indirection doesn't help: the child cmd still inherits the
# parent codepage, and chcp 65001 inside the .bat is too late (the parser
# has already read the preamble with the wrong codepage).
#
# PowerShell-invoked .bat does NOT hit this, because PowerShell runtime
# decodes the file to UTF-16 before handing the command line to cmd —
# the byte-level collision happens one level earlier.
#
# These three tests below enforce the ASCII-only contract at CI time, so
# the rule cannot silently decay between PRs (which it did — all three
# wrappers accumulated CJK REM lines between commit e55d9af and PR #45).
# ---------------------------------------------------------------------------


def _find_non_ascii(data: bytes) -> list[tuple[int, int, int, str]]:
    """Return list of (line_no, col, byte_value, line_preview) for bytes ≥ 0x80.

    Line numbers are 1-indexed. Preview is the UTF-8-decoded line truncated
    to 80 chars for readable assertion failure messages.
    """
    hits: list[tuple[int, int, int, str]] = []
    lines = data.split(b"\r\n") if b"\r\n" in data else data.split(b"\n")
    for i, line in enumerate(lines, 1):
        for j, b in enumerate(line):
            if b >= 0x80:
                try:
                    preview = line.decode("utf-8", errors="replace")[:80]
                except Exception:
                    preview = repr(line[:80])
                hits.append((i, j, b, preview))
                break  # one hit per line is enough for the report
    return hits


@pytest.mark.parametrize("bat_path", ALL_OPS_BAT_FILES, ids=lambda p: p.name)
def test_bat_files_are_ascii_pure(bat_path: pathlib.Path) -> None:
    """Pitfall #45 — .bat under scripts/ops/ must be ASCII-only (no byte ≥ 0x80).

    Desktop Commander start_process reads the .bat through a child cmd.exe
    that inherits OEM codepage (cp950 on zh-TW, cp437 on en-US). cmd's
    batch parser is byte-oriented; a CJK byte in a REM comment corrupts
    parser state and silently breaks @echo off / setlocal on DOWNSTREAM
    lines. See playbook §MCP Shell Pitfalls + pitfall #45 for byte-level
    root cause.

    Enforcement rationale: commit e55d9af originally purged CJK from
    win_git_escape.bat, but between that commit and PR #45, all three
    wrappers accumulated CJK back in REM link-back comments. Without
    CI gating the rule silently decays.
    """
    data = bat_path.read_bytes()
    hits = _find_non_ascii(data)
    if hits:
        lines = [
            f"{bat_path.name}: {len(hits)} line(s) contain byte(s) ≥ 0x80 — "
            "pitfall #45 forbids non-ASCII in .bat under scripts/ops/."
        ]
        for ln, col, b, preview in hits[:5]:
            lines.append(f"  L{ln} col{col}: byte=0x{b:02x}  |  {preview}")
        if len(hits) > 5:
            lines.append(f"  ... and {len(hits) - 5} more")
        lines.append(
            "  Fix: translate CJK/em-dash to ASCII. Link-back prose can use "
            '"see: <section-name>" phrasing instead of "§<cjk-anchor>".'
        )
        pytest.fail("\n".join(lines))


@pytest.mark.parametrize("bat_path", ALL_OPS_BAT_FILES, ids=lambda p: p.name)
def test_bat_files_are_crlf(bat_path: pathlib.Path) -> None:
    """Pitfall row #2 — LF-only .bat makes cmd.exe treat every line as a command.

    cmd.exe expects CRLF. With bare LF, the parser sees `REM\\n@echo off`
    as `REM@echo` (one token) and reports `'REM@echo' is not recognized`.
    Write/Edit tools on Linux default to LF — this test catches that.
    """
    data = bat_path.read_bytes()
    # Count bare LFs (LF not preceded by CR).
    bare_lf_lines: list[int] = []
    line_no = 1
    for i, b in enumerate(data):
        if b == 0x0A:
            if i == 0 or data[i - 1] != 0x0D:
                bare_lf_lines.append(line_no)
            line_no += 1
    assert not bare_lf_lines, (
        f"{bat_path.name}: {len(bare_lf_lines)} bare LF line-ending(s) — "
        f".bat files must be CRLF. First offending line(s): "
        f"{bare_lf_lines[:5]}. Re-save via Write tool on Windows side, or "
        f"run `unix2dos` equivalent."
    )


@pytest.mark.parametrize("bat_path", ALL_OPS_BAT_FILES, ids=lambda p: p.name)
def test_bat_files_have_no_utf8_bom(bat_path: pathlib.Path) -> None:
    """Pitfall row #2 extension — UTF-8 BOM at file start breaks cmd.exe.

    A UTF-8 BOM (`EF BB BF`) before `@echo off` makes cmd.exe read the
    first command as `\ufeff@echo off`, which it reports as
    `'<bom>@echo' is not recognized`. Write tool on some platforms can
    inject a BOM when the file is declared as UTF-8. Keep the wrapper
    byte-prefix clean.
    """
    data = bat_path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), (
        f"{bat_path.name}: file starts with UTF-8 BOM (EF BB BF). "
        f"Remove the BOM — cmd.exe cannot parse the BOM bytes as a command "
        f"prefix and will fail on the first line."
    )
