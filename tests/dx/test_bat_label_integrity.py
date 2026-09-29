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

import os
import pathlib
import re
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
# Primary MCP-caller wrappers with goto/label structure + documented caller
# pattern. These go through the full structural suite below.
BAT_FILES = [
    REPO_ROOT / "scripts" / "ops" / "win_git_escape.bat",
    REPO_ROOT / "scripts" / "ops" / "win_gh.bat",
]
# All .bat under scripts/ops/ (any depth) that can be invoked by Desktop Commander /
# Windows-MCP start_process. These get the narrower ASCII/CRLF/BOM gate
# (pitfall #45 + pitfall row #2) but not the goto/label + caller-pattern
# checks that only apply to the two escape-hatch wrappers above.
# ⛔ Not ``glob("*.bat")``: case-sensitive on POSIX, and cmd.exe runs
# ``FOO.BAT`` exactly like ``foo.bat`` (#2230). Any depth, not the top level
# only: pre-commit ``files:`` (what CI runs) reaches subdirectories (#2240).
# ⛔ The filesystem, not ``tests/_tree.repo_files()``: ``git ls-files
# --exclude-standard`` drops a gitignored .bat that start_process can still
# run. The walk stays inside scripts/ops/, so the worktree copies that rule
# guards against are never visited.


def _ops_bat_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in (root / "scripts" / "ops").rglob("*")
                  if p.is_file() and p.name.lower().endswith(".bat"))


ALL_OPS_BAT_FILES = _ops_bat_files(REPO_ROOT)

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


def _wrapper_rc(
    tmp_path: pathlib.Path, wrapper: str, tool_rc: int, env: dict[str, str] | None = None
) -> int:
    """Run `<wrapper> pr-preflight` against a stub pr_preflight.py; return its rc.

    ⛔ Behavioural on purpose (#1472): the syntax-scan version of this check was
    walked through five different ways while the wrapper still returned 0.
    Both wrappers resolve the repo root from their own location; the .bat also
    refuses to run outside that tree's git work tree, hence the `git init`.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True, timeout=60)
    (tmp_path / "scripts" / "ops").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts" / "tools" / "dx").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "scripts" / "ops" / wrapper, tmp_path / "scripts" / "ops" / wrapper)
    (tmp_path / "scripts" / "tools" / "dx" / "pr_preflight.py").write_text(
        f"import sys\nprint('stub report')\nsys.exit({tool_rc})\n", encoding="utf-8"
    )
    target = tmp_path / "scripts" / "ops" / wrapper
    if wrapper.endswith(".bat"):
        cmd = ["cmd", "/c", str(target), "pr-preflight"]
    else:
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(target), "pr-preflight"]
    # The .bat writes to fixed %TEMP% paths (#2275): parallel tests must not share them.
    env = {**(env or os.environ), "TEMP": str(tmp_path), "TMP": str(tmp_path)}
    return subprocess.run(cmd, cwd=tmp_path, capture_output=True, timeout=120, env=env).returncode


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatches")
@pytest.mark.parametrize("wrapper", ["win_git_escape.bat", "win_git_escape.ps1"])
def test_wrapper_pr_preflight_propagates_a_failing_rc(tmp_path, wrapper) -> None:
    """#1472 — a BLOCKED report must not reach the caller as success."""
    assert _wrapper_rc(tmp_path, wrapper, tool_rc=1) != 0, (
        f"{wrapper} swallows a non-zero rc from pr_preflight.py (#1472)"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatches")
@pytest.mark.parametrize("wrapper", ["win_git_escape.bat", "win_git_escape.ps1"])
def test_wrapper_pr_preflight_stays_zero_when_the_tool_passes(tmp_path, wrapper) -> None:
    """Must-ring control: `exit 1` everywhere would satisfy the check above."""
    assert _wrapper_rc(tmp_path, wrapper, tool_rc=0) == 0, (
        f"{wrapper} reports failure for a passing preflight"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
@pytest.mark.skipif(shutil.which("py") is None, reason="needs the py launcher to stand in for a real python")
def test_bat_pr_preflight_does_not_trust_a_python_that_runs_nothing(tmp_path) -> None:
    """#1918 — a `python` that exits 0 without running (the Store stub) is a false green.

    The `python` stub goes first on PATH; the tool itself says BLOCKED.
    """
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    (stub_dir / "python.bat").write_text("@exit /b 0\r\n", encoding="ascii")
    env = {**os.environ, "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}"}
    assert _wrapper_rc(tmp_path, "win_git_escape.bat", tool_rc=1, env=env) != 0, (
        "win_git_escape.bat pr-preflight ran the `python` on PATH instead of PY_CMD"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
def test_bat_pr_preflight_forwards_the_pr_number(tmp_path) -> None:
    """`pr-preflight 123` must reach the tool as `--pr 123`, run from the tree root.

    Called from a subdirectory: the other subcommands run in the caller's
    directory (#1919), this one is the exception.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True, timeout=60)
    (tmp_path / "scripts" / "ops").mkdir(parents=True)
    (tmp_path / "scripts" / "tools" / "dx").mkdir(parents=True)
    (tmp_path / "sub").mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "ops" / "win_git_escape.bat", tmp_path / "scripts" / "ops")
    log = tmp_path / "argv.txt"
    (tmp_path / "scripts" / "tools" / "dx" / "pr_preflight.py").write_text(
        f"import os, sys\nopen({str(log)!r}, 'w').write(os.getcwd() + '|' + ' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["cmd", "/c", str(tmp_path / "scripts" / "ops" / "win_git_escape.bat"), "pr-preflight", "123"],
        cwd=tmp_path / "sub",
        capture_output=True,
        timeout=120,
        env={**os.environ, "TEMP": str(tmp_path), "TMP": str(tmp_path)},  # fixed output paths, #2275
    )
    assert proc.returncode == 0, proc.stdout
    cwd, _, argv = log.read_text().partition("|")
    assert argv == "--skip-hooks --pr 123"
    assert pathlib.Path(cwd) == tmp_path, f"the tool ran in {cwd}"


def test_ps1_pr_preflight_case_runs_the_tool() -> None:
    """Cross-platform smoke — the rc predicate above is Windows-only.

    ⛔ Asserts invocation, never rc: rc is behavioural (see `_wrapper_rc`).
    """
    src = (REPO_ROOT / "scripts" / "ops" / "win_git_escape.ps1").read_text(encoding="utf-8")
    body = "\n".join(
        ln for ln in src.splitlines() if not re.match(r"^\s*#", ln)
    )
    assert "pr_preflight.py" in body, (
        "win_git_escape.ps1 no longer runs pr_preflight.py"
    )


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


# ---------------------------------------------------------------------------
# #1487 — `push` must leave the pre-push guards on the path.
#
# `--no-verify` is all-or-nothing: it also disarms protect_main_push.sh, the
# one guard with no flag of its own (dev-rules #12). The wrapper now bypasses
# the other two by name instead. ⛔ The predicate here is "did the hook run and
# what did the wrapper do with its verdict" — a syntax scan answers neither.
#
# ⛔ `main` is a parameter, not decoration: the guard this buys back only ever
# judges main, so a push path that treats main specially is exactly the shape
# that must not slip through. Measured: routing main to its own label with
# `--no-verify` left every check here green until this became a parameter.
# ---------------------------------------------------------------------------

_PUSH_BRANCHES = ("feat/escape-hatch", "main")


def _git(work: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=work, check=True, capture_output=True, timeout=60)


def _wrapper_repo(
    tmp_path: pathlib.Path, branch: str, ops_files: tuple[str, ...] = ("win_git_escape.bat",)
) -> tuple[pathlib.Path, pathlib.Path]:
    """A repo with the wrapper (and optionally the guards) beside it, plus a bare remote."""
    work = tmp_path / "work"
    (work / "scripts" / "ops").mkdir(parents=True, exist_ok=True)
    for name in ops_files:
        shutil.copy2(REPO_ROOT / "scripts" / "ops" / name, work / "scripts" / "ops" / name)
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "fixture@example.invalid")
    _git(work, "config", "user.name", "fixture")
    # ⛔ Pin hooksPath: a global core.hooksPath would otherwise point git at a
    # directory this fixture never writes, and the hook would "not run" for a
    # reason that has nothing to do with the wrapper.
    _git(work, "config", "core.hooksPath", str(work / ".git" / "hooks"))
    _git(work, "checkout", "-q", "-b", branch)
    (work / "a.txt").write_text("fixture\n", encoding="utf-8")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "test: fixture commit")
    _git(work, "remote", "add", "origin", str(bare))
    return work, bare


def _push_through_wrapper(
    tmp_path: pathlib.Path, hook_rc: int, branch: str, explicit_args: bool
) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
    """Push a real commit via `win_git_escape.bat push` against a stub pre-push hook.

    The stub records the two per-guard bypass flags it was handed and exits
    `hook_rc`, so one fixture answers both questions: whether git ran the hook
    at all, and whether a rejecting hook reaches the caller. Returns the
    wrapper's process and the recorded environment ({} when the hook never ran).
    """
    work, _bare = _wrapper_repo(tmp_path, branch)

    log = tmp_path / "hook-ran.txt"
    sh_log = str(log).replace("\\", "/")
    hook = work / ".git" / "hooks" / "pre-push"
    hook.write_text(
        "#!/bin/sh\n"
        f'echo "MKDOCS_STRICT_BYPASS=${{MKDOCS_STRICT_BYPASS:-unset}}" > "{sh_log}"\n'
        f'echo "GIT_PREFLIGHT_BYPASS=${{GIT_PREFLIGHT_BYPASS:-unset}}" >> "{sh_log}"\n'
        f"exit {hook_rc}\n",
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)

    # ⛔ Both shapes. `make win-commit` runs `win_git_escape.bat push` with NO
    # arguments, so the remote/branch auto-detect branch is the ONE the
    # production caller takes — and a bypass parked there is invisible to a
    # fixture that always passes them (measured).
    argv = ["push", "origin", branch] if explicit_args else ["push"]
    proc = subprocess.run(
        ["cmd", "/c", str(work / "scripts" / "ops" / "win_git_escape.bat"), *argv],
        cwd=work,
        capture_output=True,
        timeout=180,
        # ⛔ The wrapper writes %TEMP%\vibe-git-out.txt / -err.txt at a FIXED
        # path, so a suite run on a Windows host would otherwise overwrite the
        # output an operator is reading from their own escape-hatch run.
        env={**os.environ, "TEMP": str(tmp_path), "TMP": str(tmp_path)},
    )
    recorded: dict[str, str] = {}
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                name, _, value = line.partition("=")
                recorded[name] = value.rstrip("\r")
    return proc, recorded


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
@pytest.mark.parametrize("branch", _PUSH_BRANCHES)
@pytest.mark.parametrize("explicit_args", [True, False], ids=["argv", "auto-detect"])
def test_push_runs_the_pre_push_hook_and_names_the_two_bypasses(
    tmp_path, branch, explicit_args
) -> None:
    """#1487 — the guards must still see the push, and be bypassed by name.

    Also the must-ring control for the rc test below: an accepting hook has to
    come back as success, or `exit /b 1` everywhere would satisfy it.
    """
    proc, recorded = _push_through_wrapper(
        tmp_path, hook_rc=0, branch=branch, explicit_args=explicit_args
    )
    assert recorded, (
        f"the pre-push hook never ran for {branch} — `push` is skipping hooks "
        "again (#1487):\n" + proc.stdout.decode("utf-8", "replace")
    )
    # ⛔ Exact values, not `"NAME=1" in text`: the guards compare with
    # `[ "${NAME:-0}" = "1" ]`, so `1 ` (a quote-less `set` with a trailing
    # space) reads as unset to them while a substring check stays green.
    assert recorded.get("MKDOCS_STRICT_BYPASS") == "1", recorded
    assert recorded.get("GIT_PREFLIGHT_BYPASS") == "1", recorded
    assert proc.returncode == 0, (
        "an accepted push is reported as failure:\n"
        + proc.stdout.decode("utf-8", "replace")
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
@pytest.mark.parametrize("branch", _PUSH_BRANCHES)
@pytest.mark.parametrize("explicit_args", [True, False], ids=["argv", "auto-detect"])
def test_push_propagates_a_rejecting_pre_push_guard(
    tmp_path, branch, explicit_args
) -> None:
    """A guard that says no must reach the caller — `goto :done` is `exit /b 0`."""
    proc, recorded = _push_through_wrapper(
        tmp_path, hook_rc=1, branch=branch, explicit_args=explicit_args
    )
    assert recorded, f"fixture did not exercise the hook at all for {branch}"
    assert proc.returncode != 0, (
        f"win_git_escape.bat push swallows a rejecting pre-push guard ({branch}):\n"
        + proc.stdout.decode("utf-8", "replace")
    )


_COMMENT_RE = re.compile(r"^\s*(?:REM\b|::)", re.I)
# A git invocation, however the interpreter is named: `"%GIT_CMD%" push`,
# `%Git_Cmd% push`, `git push`, `git.exe -c x=y push`. ⛔ Case-insensitive:
# cmd.exe variable names are, so `%Git_Cmd%` and `%GIT_CMD%` are one thing.
_PUSH_SITE_RE = re.compile(r'(?:%\w+%|\bgit(?:\.exe)?\b)"?\s+(?:-c\s+\S+\s+)*push\b', re.I)
# Tokens that turn a guard off wholesale. Neither has a legitimate use in this
# wrapper, so the predicate is "absent from everything cmd.exe executes" —
# not "absent from the line I happened to match".
_HOOK_DISABLING_TOKENS = ("--no-verify", "core.hookspath")


def _executable_lines(lines: list[str]) -> list[str]:
    """What cmd.exe actually runs: comments dropped, `^` continuations joined.

    ⛔ Both normalisations are load-bearing, and both were found by walking the
    previous version of this check: commenting out a `goto :done_err` left it
    green while the rc was swallowed, and moving `--no-verify` onto a
    continuation line hid it from a per-line scan.
    """
    out: list[str] = []
    pending: str | None = None
    for raw in lines:
        if _COMMENT_RE.match(raw):
            continue
        pending = raw if pending is None else f"{pending} {raw.strip()}"
        if pending.rstrip().endswith("^"):
            pending = pending.rstrip()[:-1]
            continue
        out.append(pending)
        pending = None
    if pending is not None:
        out.append(pending)
    return out


def test_the_wrapper_never_turns_a_pre_push_guard_off() -> None:
    """Cross-platform net — the behavioural predicates above are Windows-only.

    ⛔ And they are the ONLY ones that run them: every CI runner in this repo is
    ubuntu, so on a PR this test is the whole net. ⚠️ It reads source, so it can
    only answer "is the off-switch written down"; whether the guards actually
    run is `_push_through_wrapper`'s question, and a bypass assembled at runtime
    would still need that test to be seen.
    """
    lines = _read_normalized(REPO_ROOT / "scripts" / "ops" / "win_git_escape.bat")
    body = _executable_lines(lines)
    lowered = "\n".join(body).lower()
    for token in _HOOK_DISABLING_TOKENS:
        assert token not in lowered, (
            f"`{token}` is back in executable text: it turns off every pre-push "
            "guard, including the direct-push-to-main gate that has no flag of "
            f"its own (#1487):\n"
            + "\n".join(ln for ln in body if token in ln.lower())
        )
    sites = [(i, ln) for i, ln in enumerate(body) if _PUSH_SITE_RE.search(ln)]
    assert sites, "no `git push` call site found — the scan lost its subject"
    for i, ln in sites:
        # The failure branch must reach :done_err — `goto :done` is `exit /b 0`,
        # which is how a rejecting guard used to be reported as success (#1472).
        end = next(
            (j for j in range(i + 1, len(body)) if body[j].strip().lower() == "goto :done"),
            len(body),
        )
        assert any("goto :done_err" in l.lower() for l in body[i:end]), (
            f"this push has no failure path to :done_err:\n" + "\n".join(body[i:end])
        )
    for flag in ('set "mkdocs_strict_bypass=1"', 'set "git_preflight_bypass=1"'):
        assert flag in lowered, f"{flag} is no longer set before the push"


# ---------------------------------------------------------------------------
# The composition, once: wrapper → git → shipped installer → real guards.
#
# The stub-hook tests above answer "does git run the hook and does its verdict
# reach the caller". They deliberately return the same verdict for every
# branch, so they cannot show that main is what gets refused — which is the
# whole point of dropping --no-verify. This one installs the real thing.
# ---------------------------------------------------------------------------

# ⛔ Git's own bash, by path. `bash` on PATH here is WSL, and WSL git cannot
# read a Windows-path worktree — the installer then reports "not inside a git
# work tree" while you plainly are (measured).
_GIT_BASH = pathlib.Path(r"C:\Program Files\Git\bin\bash.exe")

_GUARD_FILES = (
    "win_git_escape.bat",
    "install_prepush_hook.sh",
    "prepush_dispatch.sh",
    "protect_main_push.sh",
    "require_preflight_pass.sh",
    "pre_push_mkdocs_strict.sh",
    "_prepush_refs.sh",
)


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
@pytest.mark.skipif(not _GIT_BASH.exists(), reason="Git for Windows bash not installed")
@pytest.mark.parametrize(
    ("branch", "blocked"), [("feat/escape-hatch", False), ("main", True)]
)
def test_the_shipped_guards_refuse_only_a_direct_main_push(tmp_path, branch, blocked) -> None:
    """#1487 — the whole point: main is refused, everything else still goes.

    The feature-branch leg is the must-ring control; without it, a wrapper that
    failed every push would satisfy the main leg.
    """
    work, _bare = _wrapper_repo(tmp_path, branch, ops_files=_GUARD_FILES)
    install = subprocess.run(
        [str(_GIT_BASH), "scripts/ops/install_prepush_hook.sh"],
        cwd=work,
        capture_output=True,
        timeout=120,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    assert (work / ".git" / "hooks" / "pre-push").exists(), "installer wrote no hook"

    proc = subprocess.run(
        ["cmd", "/c", str(work / "scripts" / "ops" / "win_git_escape.bat"), "push"],
        cwd=work,
        capture_output=True,
        timeout=300,
        env={**os.environ, "TEMP": str(tmp_path), "TMP": str(tmp_path)},
    )
    out = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
    if blocked:
        assert proc.returncode != 0, f"a direct push to {branch} was allowed:\n{out}"
        # ASCII slice of the guard's banner — the CJK around it travels through
        # cmd's codepage, this does not.
        assert "dev-rules #12" in out, f"blocked, but not by that guard:\n{out}"
    else:
        assert proc.returncode == 0, f"{branch} was refused:\n{out}"
        assert "dev-rules #12" not in out, f"the main guard fired on {branch}:\n{out}"


# ---------------------------------------------------------------------------
# #2230 — `.gitattributes` must put EVERY spelling of the Windows shell
# extensions on CRLF.
#
# Attribute patterns are case-sensitive, so a plain `*.bat` left `FOO.BAT` on
# the repo-wide LF rule: checked out LF, mis-parsed by cmd.exe, and — under
# scripts/ops/ — contradicting the CRLF rule the tests above enforce.
# `core.ignorecase=false` is pinned on every call: a Windows/macOS checkout
# defaults it to true, which makes git match attributes case-insensitively
# and would turn the control below green-for-the-wrong-reason there.

_CRLF_SPELLINGS = [
    "tools/x.bat", "tools/x.BAT", "scripts/ops/x.Bat",
    "x.cmd", "x.CMD", "x.cMd",
    "x.ps1", "x.PS1", "x.Ps1",
]
# Share a prefix or suffix with the rule but are not that extension.
_LOOKALIKES = ["x.batx", "x.bat.sh", "x.xbat", "x.cmdline", "x.ps12", "x.ps"]


def _eol_of(cwd: pathlib.Path, paths: list[str]) -> dict[str, str]:
    """`eol` attribute per path; an unanswered path is an error, not a pass.

    `-z`: the default output C-quotes non-ASCII paths and joins fields with
    ": ", so a key would stop matching the path it answers for.
    """
    proc = subprocess.run(
        ["git", "-c", "core.ignorecase=false", "check-attr", "-z", "eol", "--", *paths],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, f"git check-attr failed (unmeasured):\n{proc.stderr}"
    fields = proc.stdout.split("\0")[:-1]
    assert len(fields) % 3 == 0, f"unexpected check-attr -z output: {proc.stdout!r}"
    out: dict[str, str] = {}
    for path, _attr, value in zip(fields[0::3], fields[1::3], fields[2::3]):
        out[path] = value
    assert set(out) == set(paths), (
        f"git check-attr did not answer for every path (unmeasured): "
        f"asked {sorted(paths)}, got {sorted(out)}")
    return out


def test_every_spelling_of_a_windows_script_is_crlf() -> None:
    eol = _eol_of(REPO_ROOT, _CRLF_SPELLINGS)
    wrong = {p: v for p, v in eol.items() if v != "crlf"}
    assert not wrong, (
        f".gitattributes leaves these Windows scripts off CRLF: {wrong}. "
        "Spell the extension as a bracket class, e.g. `*.[bB][aA][tT]` (#2230).")


def test_a_lookalike_extension_is_not_forced_to_crlf() -> None:
    eol = _eol_of(REPO_ROOT, _LOOKALIKES)
    wrong = {p: v for p, v in eol.items() if v == "crlf"}
    assert not wrong, f".gitattributes over-matches: {wrong}"


def test_the_plain_pattern_is_what_misses_the_upper_case(tmp_path: pathlib.Path) -> None:
    """Paired control: the pre-#2230 spelling really does miss `FOO.BAT`.

    Without it, the two tests above would also pass if `check-attr` matched
    case-insensitively on this host — that is, if they measured nothing.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True, timeout=60)
    (tmp_path / ".gitattributes").write_text(
        "* text=auto eol=lf\n*.bat text eol=crlf\n", encoding="utf-8", newline="\n")
    eol = _eol_of(tmp_path, ["x.bat", "x.BAT"])
    assert eol == {"x.bat": "crlf", "x.BAT": "lf"}, eol


# ---------------------------------------------------------------------------
# #2240 — the pytest layer covers scripts/ops/ at any depth, like CI does.

def test_ops_bat_files_reaches_subdirectories(tmp_path: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True, timeout=60)
    ops = tmp_path / "scripts" / "ops"
    ignored = ops / "sub" / "ignored.bat"
    (tmp_path / ".gitignore").write_text("ignored.bat\n", encoding="utf-8")
    inside = [ops / "x.bat", ops / "sub" / "y.BAT", ops / "a" / "b" / "z.Bat", ignored]
    outside = [tmp_path / "scripts" / "opsx" / "x.bat",
               tmp_path / "other" / "scripts" / "ops" / "x.bat",
               tmp_path / "scripts" / "x.bat", ops / "sub" / "x.batx"]
    for p in inside + outside:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"@echo off\r\n")
    assert _ops_bat_files(tmp_path) == sorted(inside)


def test_ops_bat_files_sees_the_real_wrappers() -> None:
    """Tripwire: an empty ALL_OPS_BAT_FILES would skip the three gates above."""
    missing = [p for p in BAT_FILES if p not in ALL_OPS_BAT_FILES]
    assert not missing, f"ALL_OPS_BAT_FILES lost {missing}"


# ---------------------------------------------------------------------------
# #1918 — every other subcommand's git failure must reach the caller too.
#
# One injection for all of them, `_break_git`: the repo stays discoverable, so
# the wrapper's own tree check passes, but the git call inside each subcommand
# fails. The control runs the same subcommand in a healthy repo and checks that
# its effect landed, so `exit /b 1` everywhere cannot pass and neither can a
# success that prints FAILED (the old `branch <existing>` shape).
# ---------------------------------------------------------------------------


def _git_out(work: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=work, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
    ).stdout.strip()


def _stage_b(work: pathlib.Path) -> None:
    (work / "b.txt").write_text("b\n", encoding="utf-8")
    _git(work, "add", "b.txt")


# name -> (argv, prep in the healthy repo, effect that must have landed)
_SUBCOMMANDS = {
    "status": (("status",), None, None),
    "add": (
        ("add", "b.txt"),
        lambda w: (w / "b.txt").write_text("b\n", encoding="utf-8"),
        lambda w: _git_out(w, "diff", "--cached", "--name-only") == "b.txt",
    ),
    "commit": (("commit", "test: second"), _stage_b, lambda w: _git_out(w, "rev-list", "--count", "HEAD") == "2"),
    "commit-file": (
        ("commit-file", "msg.txt"),
        lambda w: (_stage_b(w), (w / "msg.txt").write_text("test: from a file\n", encoding="utf-8")),
        lambda w: _git_out(w, "log", "-1", "--format=%s") == "test: from a file",
    ),
    "tag": (("tag", "t-new"), None, lambda w: _git_out(w, "tag", "--list", "t-new") == "t-new"),
    "branch-create": (
        ("branch", "feat/new"), None, lambda w: _git_out(w, "branch", "--show-current") == "feat/new"
    ),
    # The second `checkout` used to be judged by the first one's rc.
    "branch-existing": (
        ("branch", "feat/exists"),
        lambda w: _git(w, "branch", "feat/exists"),
        lambda w: _git_out(w, "branch", "--show-current") == "feat/exists",
    ),
    "branch-list": (("branch",), None, None),
    "log": (("log",), None, None),
    "diff": (("diff",), None, None),
    "preflight": (("preflight",), None, None),
}


def _break_git(work: pathlib.Path) -> None:
    """`rev-parse --show-toplevel` still answers; the subcommands' own calls fail.

    A corrupt index and an unreadable packed-refs; neither alone breaks every
    subcommand. GIT_DIR can't be used: the wrapper clears it (#1919).
    """
    (work / ".git" / "index").write_bytes(b"not an index")
    (work / ".git" / "packed-refs").write_text("not a packed-refs line\n", encoding="ascii")


def _run_subcommand(
    tmp_path: pathlib.Path, name: str, broken: bool
) -> tuple[pathlib.Path, subprocess.CompletedProcess]:
    argv, prep, _effect = _SUBCOMMANDS[name]
    work, _bare = _wrapper_repo(
        tmp_path, "feat/escape-hatch", ops_files=("win_git_escape.bat", "commit_helper.py")
    )
    if prep:
        prep(work)
    if broken:
        _break_git(work)
    # ⛔ No inherited GIT_*: a pytest run from inside a git hook carries
    # GIT_DIR / GIT_INDEX_FILE, which would point the fixture's git at this repo.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(TEMP=str(tmp_path), TMP=str(tmp_path))
    proc = subprocess.run(
        ["cmd", "/c", str(work / "scripts" / "ops" / "win_git_escape.bat"), *argv],
        cwd=work,
        capture_output=True,
        timeout=120,
        env=env,
    )
    return work, proc


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
@pytest.mark.parametrize("name", sorted(_SUBCOMMANDS))
def test_a_failing_git_call_reaches_the_caller(tmp_path, name) -> None:
    """#1918 — `FAILED:` followed by `goto :done` is `exit /b 0`."""
    _work, proc = _run_subcommand(tmp_path, name, broken=True)
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode != 0, f"`{name}` swallowed a git failure:\n{out}"
    assert "FAILED" in out, f"`{name}` failed without saying so:\n{out}"
    # git's own reason, not just the wrapper's verdict: git writes lower-case
    # `fatal:` / `error:`, the wrapper's own messages are upper-case. `preflight`
    # lets git write to the console, so look at both streams.
    assert re.search(r"\b(fatal|error):", out + proc.stderr.decode("utf-8", "replace")), (
        f"`{name}` failed without git's reason:\n{out}"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
@pytest.mark.parametrize("name", sorted(_SUBCOMMANDS))
def test_a_working_git_call_is_reported_as_success(tmp_path, name) -> None:
    """Must-ring control for the test above."""
    work, proc = _run_subcommand(tmp_path, name, broken=False)
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 0, f"`{name}` reported a working git call as failure:\n{out}"
    assert "FAILED" not in out, f"`{name}` printed FAILED on success:\n{out}"
    effect = _SUBCOMMANDS[name][2]
    assert effect is None or effect(work), f"`{name}` returned 0 but its effect did not land:\n{out}"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
def test_branch_never_treats_the_name_as_a_path(tmp_path) -> None:
    """`branch .` is not a branch; a `checkout .` fallback discarded every change."""
    work, _bare = _wrapper_repo(tmp_path, "feat/escape-hatch", ops_files=("win_git_escape.bat",))
    (work / "a.txt").write_text("uncommitted\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(TEMP=str(tmp_path), TMP=str(tmp_path))
    proc = subprocess.run(
        ["cmd", "/c", str(work / "scripts" / "ops" / "win_git_escape.bat"), "branch", "."],
        cwd=work,
        capture_output=True,
        timeout=120,
        env=env,
    )
    out = proc.stdout.decode("utf-8", "replace")
    assert (work / "a.txt").read_text(encoding="utf-8") == "uncommitted\n", f"changes discarded:\n{out}"
    assert proc.returncode != 0, f"`branch .` reported success:\n{out}"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
def test_branch_reports_a_refused_switch_to_an_existing_branch(tmp_path) -> None:
    """GIT_DIR injection stops at `show-ref`; this failure is past it."""
    work, _bare = _wrapper_repo(tmp_path, "feat/escape-hatch", ops_files=("win_git_escape.bat",))
    _git(work, "checkout", "-q", "-b", "feat/exists")
    (work / "a.txt").write_text("theirs\n", encoding="utf-8")
    _git(work, "commit", "-q", "-am", "test: diverge")
    _git(work, "checkout", "-q", "feat/escape-hatch")
    (work / "a.txt").write_text("mine\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(TEMP=str(tmp_path), TMP=str(tmp_path))
    proc = subprocess.run(
        ["cmd", "/c", str(work / "scripts" / "ops" / "win_git_escape.bat"), "branch", "feat/exists"],
        cwd=work,
        capture_output=True,
        timeout=120,
        env=env,
    )
    out = proc.stdout.decode("utf-8", "replace")
    assert _git_out(work, "branch", "--show-current") == "feat/escape-hatch", out
    assert proc.returncode != 0, f"a refused switch reported success:\n{out}"
    assert "FAILED" in out, out


# ---------------------------------------------------------------------------
# #1919 — which tree the wrapper acts on, and which locks it touches.
#
# The wrapper refuses to run outside the tree its copy lives in, and runs in
# the caller's directory, so relative arguments keep git's meaning. It deletes
# no lock; preflight lists every one, wherever git keeps it (in a linked
# worktree `.git` is a file and the locks live under the common git dir).
# ---------------------------------------------------------------------------


def _bat(tree: pathlib.Path, tmp_path: pathlib.Path, *argv: str,
         cwd: pathlib.Path | None = None, env_extra: dict[str, str] | None = None):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(TEMP=str(tmp_path), TMP=str(tmp_path), **(env_extra or {}))
    return subprocess.run(
        ["cmd", "/c", str(tree / "scripts" / "ops" / "win_git_escape.bat"), *argv],
        cwd=cwd or tree,
        capture_output=True,
        timeout=120,
        env=env,
    )


def _main_and_worktree(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    """Main repo + one linked worktree, each with its own copy of the wrapper,
    on different commits (refs are shared, so the commit tells them apart)."""
    ops = ("win_git_escape.bat", "commit_helper.py")
    work, bare = _wrapper_repo(tmp_path, "feat/escape-hatch", ops_files=ops)
    wt = tmp_path / "linked"
    _git(work, "worktree", "add", "-q", "-b", "feat/linked", str(wt))
    (wt / "scripts" / "ops").mkdir(parents=True)
    for name in ops:
        shutil.copy2(REPO_ROOT / "scripts" / "ops" / name, wt / "scripts" / "ops")
    _git(wt, "commit", "-q", "--allow-empty", "-m", "test: linked only")
    return {"main": work, "linked": wt, "bare": bare}


def _git_path(tree: pathlib.Path, name: str) -> pathlib.Path:
    p = pathlib.Path(_git_out(tree, "rev-parse", "--git-path", name))
    return p if p.is_absolute() else tree / p


def _state(trees: dict[str, pathlib.Path]) -> tuple[str, ...]:
    """Every ref (both trees share them), both indexes, and the remote."""
    return (
        _git_out(trees["main"], "for-each-ref"),
        _git_out(trees["main"], "diff", "--cached", "--name-only"),
        _git_out(trees["linked"], "diff", "--cached", "--name-only"),
        _git_out(trees["main"], "--git-dir", str(trees["bare"]), "for-each-ref"),
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
@pytest.mark.parametrize(
    "argv",
    [("status",), ("add", "a.txt"), ("commit", "test: x"), ("commit-file", "a.txt"),
     ("tag", "t-x"), ("branch", "feat/x"), ("push",), ("preflight",)],
    ids=lambda a: a[0],
)
def test_the_wrapper_refuses_a_caller_outside_its_tree(tmp_path, argv) -> None:
    """Called from the main repo, the linked tree's copy must not act on either."""
    trees = _main_and_worktree(tmp_path)
    (trees["main"] / "a.txt").write_text("changed\n", encoding="utf-8")
    _git(trees["main"], "add", "a.txt")
    before = _state(trees)
    proc = _bat(trees["linked"], tmp_path, *argv, cwd=trees["main"])
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode != 0, f"`{argv[0]}` ran from another tree:\n{out}"
    assert "FAILED" in out, out
    assert _state(trees) == before, f"`{argv[0]}` changed something:\n{out}"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
def test_relative_arguments_resolve_against_the_callers_directory(tmp_path) -> None:
    """Must-not-ring control for the refusal above: a subdirectory is inside."""
    trees = _main_and_worktree(tmp_path)
    tree = trees["linked"]
    docs = tree / "docs"
    docs.mkdir()
    for d in (tree, docs):  # same names at the root, to catch root-relative resolution
        (d / "README.md").write_text(f"{d.name}\n", encoding="utf-8")
        (d / "msg.txt").write_text(f"test: message from {d.name}\n", encoding="utf-8")
    proc = _bat(tree, tmp_path, "add", "README.md", cwd=docs)
    assert proc.returncode == 0, proc.stdout
    assert _git_out(tree, "diff", "--cached", "--name-only") == "docs/README.md"
    proc = _bat(tree, tmp_path, "commit-file", "msg.txt", cwd=docs)
    assert proc.returncode == 0, proc.stdout
    assert _git_out(tree, "log", "-1", "--format=%s") == "test: message from docs"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
def test_an_inherited_git_dir_does_not_redirect_the_wrapper(tmp_path) -> None:
    """A git hook exports GIT_DIR; the wrapper must still act on its own tree."""
    trees = _main_and_worktree(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True, timeout=60)
    _git(other, "commit", "-q", "--allow-empty", "-m", "test: other")
    proc = _bat(trees["linked"], tmp_path, "tag", "t-env", env_extra={"GIT_DIR": str(other / ".git")})
    assert proc.returncode == 0, proc.stdout
    assert _git_out(trees["linked"], "rev-parse", "t-env") == _git_out(trees["linked"], "rev-parse", "HEAD")
    assert _git_out(other, "tag", "--list") == "", "the tag went to the repo GIT_DIR named"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
def test_a_copy_outside_any_work_tree_refuses_instead_of_climbing(tmp_path) -> None:
    """A tree whose .git is gone, nested in another repo: git would climb to it."""
    work, _bare = _wrapper_repo(tmp_path, "feat/escape-hatch")
    inner = work / "inner"
    (inner / "scripts" / "ops").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "ops" / "win_git_escape.bat", inner / "scripts" / "ops")
    proc = _bat(inner, tmp_path, "tag", "t-climb")
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode != 0, out
    assert _git_out(work, "tag", "--list") == "", f"the tag landed in the outer repo:\n{out}"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
def test_a_copy_outside_any_repo_says_why(tmp_path) -> None:
    """Not a repo at all: refused with git's own reason, not an empty tree name."""
    tree = tmp_path / "loose"
    (tree / "scripts" / "ops").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "ops" / "win_git_escape.bat", tree / "scripts" / "ops")
    proc = _bat(tree, tmp_path, "status", env_extra={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode != 0, out
    assert "not a git repository" in out.lower(), out


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
def test_an_inherited_work_tree_cannot_satisfy_the_tree_check(tmp_path) -> None:
    """GIT_WORK_TREE naming the wrapper's tree, cwd in another repo: git would take
    that repo's git dir while `--show-toplevel` answers with the wrapper's tree."""
    trees = _main_and_worktree(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True, timeout=60)
    _git(other, "commit", "-q", "--allow-empty", "-m", "test: other")
    proc = _bat(trees["linked"], tmp_path, "tag", "t-wt", cwd=other,
                env_extra={"GIT_WORK_TREE": str(trees["linked"])})
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode != 0, out
    assert _git_out(other, "tag", "--list") == "", f"the tag landed in the caller's repo:\n{out}"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
def test_an_inherited_index_file_does_not_redirect_the_wrapper(tmp_path) -> None:
    """A pre-commit hook exports GIT_INDEX_FILE: staging must land in this tree's index."""
    trees = _main_and_worktree(tmp_path)
    tree = trees["linked"]
    (tree / "new.txt").write_text("new\n", encoding="utf-8")
    foreign = _git_path(trees["main"], "index")
    before = foreign.read_bytes()
    proc = _bat(tree, tmp_path, "add", "new.txt", env_extra={"GIT_INDEX_FILE": str(foreign)})
    assert proc.returncode == 0, proc.stdout
    assert _git_out(tree, "diff", "--cached", "--name-only") == "new.txt"
    assert foreign.read_bytes() == before, "staged into the index GIT_INDEX_FILE named"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
@pytest.mark.parametrize("bat_in", ["w!x!", "w"])
def test_a_bang_in_either_path_fails_closed(tmp_path, bat_in) -> None:
    """Delayed expansion turns `w!x!` into `w`, a tree that exists: whichever
    side carries the `!`, the rewrite must not make the two trees look equal."""
    trees = {}
    for name in ("w!x!", "w"):
        trees[name] = tmp_path / name
        subprocess.run(["git", "init", "-q", str(trees[name])], check=True, capture_output=True, timeout=60)
        _git(trees[name], "commit", "-q", "--allow-empty", "-m", "test: base")
        (trees[name] / "scripts" / "ops").mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "scripts" / "ops" / "win_git_escape.bat", trees[name] / "scripts" / "ops")
    caller = "w" if bat_in == "w!x!" else "w!x!"
    proc = _bat(trees[bat_in], tmp_path, "tag", "t-bang", cwd=trees[caller])
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode != 0, out
    assert [n for n in trees if _git_out(trees[n], "tag", "--list")] == [], f"a tag landed:\n{out}"
    if bat_in == "w!x!":
        assert 'contains "!"' in out, f"refused without saying why:\n{out}"


def _plant_locks(trees: dict[str, pathlib.Path], layout: str) -> list[pathlib.Path]:
    tree, other = trees[layout], trees["linked" if layout == "main" else "main"]
    locks = [
        _git_path(tree, "index.lock"),                         # this tree's own git dir
        _git_path(tree, "HEAD.lock"),
        _git_path(other, "index.lock"),                        # the other tree's
        _git_path(tree, "refs/heads") / "feat" / "held.lock",  # common dir, nested
        _git_path(tree, "packed-refs.lock"),                   # common dir
    ]
    for lock in locks:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("", encoding="utf-8")
    return locks


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
@pytest.mark.parametrize("layout", ["main", "linked"])
@pytest.mark.parametrize("sub", ["status", "preflight"])
def test_no_lock_is_ever_deleted(tmp_path, layout, sub) -> None:
    """A lock a crashed git left and one a running git holds look the same."""
    trees = _main_and_worktree(tmp_path)
    locks = _plant_locks(trees, layout)
    out = _bat(trees[layout], tmp_path, sub).stdout.decode("utf-8", "replace")
    assert [str(p) for p in locks if not p.exists()] == [], f"`{sub}` deleted a lock:\n{out}"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
@pytest.mark.parametrize("layout", ["main", "linked"])
def test_preflight_lists_every_lock_git_may_be_holding(tmp_path, layout) -> None:
    trees = _main_and_worktree(tmp_path)
    locks = _plant_locks(trees, layout)
    out = _bat(trees[layout], tmp_path, "preflight").stdout.decode("utf-8", "replace")
    missed = [str(p) for p in locks if str(p).lower() not in out.lower()]
    assert missed == [], f"preflight missed locks in {layout}:\n{out}"


# --- #1920: what the docs say a wrapper can do, the wrapper must dispatch -----
# The playbook told readers to run `win_git_escape.bat raw git ...`; the .bat had
# no `raw`, fell through to `:usage`, and returned 0. Two halves, both static so
# Linux CI sees them: every documented `<wrapper>.bat <sub>` is dispatched, and
# the fall-through is not a success.
DISPATCH_RE = re.compile(r'^\s*if\s+/i\s+"%CMD%"=="([a-z][a-z-]*)"\s+goto\s+:', re.IGNORECASE)
# Only inside code (inline `...` or a fenced block): prose like "the
# win_git_escape.bat header" names the file, not an invocation.
DOC_CALL_RE = re.compile(r"\bwin_(git_escape|gh)\.bat[ \t]+([a-z][a-z-]*)")
FENCE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
INLINE_RE = re.compile(r"`[^`\n]+`")


def _dispatched(bat: pathlib.Path) -> set[str]:
    return {m.group(1).lower() for ln in _read_normalized(bat) if (m := DISPATCH_RE.match(ln))}


def _documented_calls() -> list[tuple[str, str, str]]:
    """(doc, wrapper, subcommand) for every wrapper call written as code in a .md."""
    from _tree import REPO_ROOT as TREE_ROOT, repo_files  # noqa: PLC0415

    calls = []
    for md in repo_files(".md"):
        text = md.read_text(encoding="utf-8", errors="replace")
        code = FENCE_RE.findall(text) + INLINE_RE.findall(FENCE_RE.sub("", text))
        for chunk in code:
            for m in DOC_CALL_RE.finditer(chunk):
                calls.append((md.relative_to(TREE_ROOT).as_posix(), m.group(1), m.group(2)))
    return calls


def test_every_wrapper_dispatches_something() -> None:
    """Control: an empty set would make the doc check below pass vacuously."""
    for bat in BAT_FILES:
        assert len(_dispatched(bat)) >= 5, f"{bat.name}: dispatch pattern no longer matches"


def test_documented_subcommands_exist() -> None:
    calls = _documented_calls()
    assert calls, "no documented wrapper call found — the scan no longer matches"
    known = {b.stem.removeprefix("win_"): _dispatched(b) for b in BAT_FILES}
    missing = sorted({f"{doc}: win_{w}.bat {sub}" for doc, w, sub in calls
                      if sub.lower() not in known[w]})
    assert missing == [], (
        "docs call a subcommand the wrapper does not dispatch (it falls to "
        ":usage instead):\n" + "\n".join(missing))


@pytest.mark.parametrize("bat_path", BAT_FILES, ids=lambda p: p.name)
def test_usage_is_not_a_success(bat_path: pathlib.Path) -> None:
    """An unknown subcommand lands in `:usage`; rc 0 there reads as success."""
    lines = _read_normalized(bat_path)
    start = next(i for i, ln in enumerate(lines) if LABEL_RE.match(ln) and ln.strip().lower() == ":usage")
    block = []
    for ln in lines[start + 1:]:
        if LABEL_RE.match(ln):
            break
        block.append(ln)
    gotos = [m.group(1).lower() for ln in block
             if not ln.strip().upper().startswith("REM ") for m in GOTO_RE.finditer(ln)]
    assert gotos and gotos[-1] == "done_err", f"{bat_path.name} :usage ends in {gotos[-1:]}"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only escape hatch")
def test_an_unknown_subcommand_is_reported_as_failure(tmp_path) -> None:
    """The behavioural twin of the check above."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True, timeout=60)
    (tmp_path / "scripts" / "ops").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "ops" / "win_git_escape.bat", tmp_path / "scripts" / "ops")
    assert _bat(tmp_path, tmp_path, "raw", "git", "status").returncode != 0
