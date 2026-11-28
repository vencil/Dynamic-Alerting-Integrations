"""An interactive answer is read through `_lib_prompt.prompt_line` (#3822).

WHAT IT GUARDS. A bare ``input()`` on a stdin that has no line — CI, cron,
``< /dev/null``, a closed fd 0 — raised EOFError, which the tools reported
as a crash: ``unexpected internal error (EOFError)`` plus a traceback, and
nothing that names the flag that would have unblocked the caller.
``prompt_line`` turns that into a caller error (rc 2, one ``ERROR:`` line
naming the tool's non-interactive way).

TWO HALVES.

* The ratchet (AST): under ``scripts/``, the builtin ``input`` may be
  referenced only in ``scripts/tools/_lib_prompt.py``. A reference counts,
  not only a call — ``f = input`` and ``builtins.input`` /
  ``from builtins import input`` / ``getattr(builtins, "input")`` too.
* The behaviour: the tools that ask (scaffold-tenant, init) run with stdin
  ``/dev/null`` end at rc 2, not the test crash code, with no traceback and
  the flag named. (migrate-rule asked under ``--interactive`` until #4226
  removed the flag: it no longer guesses anything to confirm.)

⚠️ WHAT THE RATCHET CANNOT CATCH (it reads names, not values): a name built
at run time (``getattr(builtins, "in" + "put")``), ``builtins.__dict__``,
``sys.stdin.readline()`` and other direct reads of stdin, and a module that
binds a name ``input`` of its own (it is reported anyway: rename it).
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from _tree import repo_files  # noqa: E402

REPO = HERE.parents[1]
TOOLS = REPO / "scripts" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import _lib_exitcodes  # noqa: E402
import _lib_prompt  # noqa: E402

#: The one module allowed to read stdin with the builtin ``input``.
HOME = "scripts/tools/_lib_prompt.py"


def input_refs(source: str) -> "list[int]":
    """Line numbers of every reference to the builtin ``input`` in *source*."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id == "input":
            lines.append(node.lineno)
        elif (isinstance(node, ast.Attribute) and node.attr == "input"
              and isinstance(node.value, ast.Name)
              and node.value.id in ("builtins", "__builtins__")):
            lines.append(node.lineno)
        elif (isinstance(node, ast.ImportFrom) and node.module == "builtins"
              and any(a.name in ("input", "*") for a in node.names)):
            lines.append(node.lineno)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "getattr" and len(node.args) >= 2
              and isinstance(node.args[1], ast.Constant)
              and node.args[1].value == "input"):
            lines.append(node.lineno)
    return lines


def _scripts() -> "dict[str, str]":
    return {p.relative_to(REPO).as_posix(): p.read_text(encoding="utf-8")
            for p in repo_files(".py") if p.relative_to(REPO).parts[0] == "scripts"}


# ── the ratchet ──────────────────────────────────────────────────────

def test_input_is_read_only_through_prompt_line() -> None:
    found = {path: refs for path, src in _scripts().items()
             if path != HOME and (refs := input_refs(src))}
    assert not found, (
        "a bare `input` under scripts/: an unreadable stdin (CI, cron, "
        "`< /dev/null`) ends it at a crash with a traceback. Read the answer "
        "with `_lib_prompt.prompt_line(prompt, remedy=<the tool's "
        f"non-interactive flags>)` instead (#3822): {found}")


def test_the_home_still_reads_with_input() -> None:
    """Anti-vacuity: the scan sees the one reference that is allowed, so a
    scan that finds nothing anywhere is not mistaken for a clean tree."""
    assert input_refs((REPO / HOME).read_text(encoding="utf-8")), HOME


@pytest.mark.parametrize("snippet", [
    "x = input('? ')",
    "f = input",
    "import builtins\nbuiltins.input('? ')",
    "from builtins import input as ask",
    "import builtins\ngetattr(builtins, 'input')('? ')",
])
def test_every_spelling_is_seen(snippet: str) -> None:
    assert input_refs(snippet), snippet


def test_other_names_are_not_seen() -> None:
    assert not input_refs("import _lib_input\n_lib_input.add_input(p, '--x')\n"
                          "ns_input = prompt_line('? ', remedy='r')\n")


# ── prompt_line itself ───────────────────────────────────────────────

@pytest.mark.parametrize("exc", [EOFError("EOF when reading a line"),
                                 RuntimeError("input(): lost sys.stdin"),
                                 OSError(9, "Bad file descriptor")],
                         ids=["eof", "lost-stdin", "bad-fd"])
def test_an_unreadable_stdin_is_a_caller_error(monkeypatch, exc) -> None:
    def boom(_prompt=""):
        raise exc
    monkeypatch.setattr("builtins.input", boom)
    with pytest.raises(_lib_exitcodes.CallerError) as got:
        _lib_prompt.prompt_line("? ", remedy="pass --flag")
    line = got.value.error_line
    assert line.startswith("ERROR: ") and "--flag" in line, line
    assert type(exc).__name__ in line, line
    assert got.value.reason == "stdin_unreadable"


def test_a_readable_answer_is_returned_as_given(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _p="": "  yes ")
    assert _lib_prompt.prompt_line("? ", remedy="r") == "  yes "


def test_ctrl_c_is_not_turned_into_a_caller_error(monkeypatch) -> None:
    def interrupt(_prompt=""):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _lib_prompt.prompt_line("? ", remedy="r")


# ── the tools, with stdin /dev/null ──────────────────────────────────

def _run(argv: "list[str]", *, env: "dict[str, str] | None" = None):
    with open(os.devnull, "rb") as null:
        return subprocess.run([sys.executable, *argv], stdin=null,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300,
                              env={**os.environ, "DA_LANG": "en", **(env or {})})


def _assert_named_caller_error(proc, flag: str) -> None:
    out = proc.stdout + proc.stderr
    assert proc.returncode == _lib_exitcodes.EXIT_CALLER_ERROR, (
        f"rc={proc.returncode} (the test crash code is "
        f"{os.environ.get(_lib_exitcodes.TEST_CRASH_EXIT_CODE_ENV)})\n{out[-1500:]}")
    assert "Traceback" not in proc.stderr, proc.stderr[-1500:]
    assert _lib_exitcodes.CRASH_LINE_PREFIX not in proc.stderr, proc.stderr[-1500:]
    assert flag in proc.stderr, proc.stderr[-1500:]


def test_scaffold_tenant_names_non_interactive(tmp_path) -> None:
    proc = _run([str(TOOLS / "ops" / "scaffold_tenant.py"), "-o", str(tmp_path / "out")])
    _assert_named_caller_error(proc, "--non-interactive")
    assert not (tmp_path / "out").exists()


def test_init_names_non_interactive(tmp_path) -> None:
    proc = _run([str(TOOLS / "ops" / "init_project.py"), "-o", str(tmp_path / "repo")])
    _assert_named_caller_error(proc, "--non-interactive")
    assert not (tmp_path / "repo").exists()
