"""A flag that names a file replaced whole refuses ``-`` at parse time (#3107 ③a).

``_lib_io.write_repo_artifact`` replaces its file atomically, so the file is
never a stream: ``-`` (standard output for an operator output flag) names
nothing it can write. ``_lib_validation.artifact_file_type`` is the argparse
``type=`` that refuses it — rc 2, argparse's ``argument <flag>: ...`` line.
Devices, FIFOs and fd 1's file are not decidable from the string; the write
(``check_repo_artifact``) refuses those.

⚠️ The list is the flags that declare it, written out (as in
``test_output_dir_flag``). Every one must be an ``artifact`` file row of the
write gate; the other artifact rows (``sync_glossary_abbr --output``,
``configmap_assemble --output``, ``state-reconcile --manifest-path``) do not
declare it yet, and this file holds them to nothing.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_validation
from test_output_path_write_failure import ROWS, TOOLS_DIR, Ctx

#: (tool, flag) — the same strings as the rows of the write gate.
ARTIFACT_FILE_FLAGS = [
    ("dx/generate_tenant_metadata.py", "--output"),  # #3982, owner 2026-11-18
    ("ops/generate_alertmanager_routes.py", "-o/--output"),
]

REFUSAL = "'-' is standard output, and this flag names a file that is replaced whole"


def test_dash_is_refused_and_nothing_else_is_judged():
    kind = _lib_validation.artifact_file_type()
    with pytest.raises(argparse.ArgumentTypeError) as ei:
        kind("-")
    assert REFUSAL in str(ei.value)
    for value in ("out.yml", "./-", "--", " ", "/dev/null", "/dev/stdout", "a/-"):
        assert kind(value) == value


def test_an_empty_value_is_refused():
    """An unset variable (`-o "$OUT"`): it names no file, and generate-routes
    reads a falsy -o as "not given" — it printed to stdout and exited 0."""
    with pytest.raises(argparse.ArgumentTypeError, match="an empty value names no file"):
        _lib_validation.artifact_file_type()("")


def test_the_tools_own_type_runs_after_it():
    kind = _lib_validation.artifact_file_type(then=Path)
    assert kind("x.yml") == Path("x.yml")
    with pytest.raises(argparse.ArgumentTypeError) as ei:
        kind("-")
    assert REFUSAL in str(ei.value)


def test_argparse_names_the_flag_and_exits_2(capsys):
    parser = argparse.ArgumentParser(prog="t")
    parser.add_argument("-o", "--output", type=_lib_validation.artifact_file_type())
    assert parser.parse_args([]).output is None
    assert parser.parse_args(["-o", "x"]).output == "x"
    with pytest.raises(SystemExit) as ei:
        parser.parse_args(["-o", "-"])
    assert ei.value.code == 2
    assert f"t: error: argument -o/--output: {REFUSAL}" in capsys.readouterr().err


def test_every_listed_flag_is_an_artifact_file_row_of_the_write_gate():
    rows = {(r.tool, r.flag): r for r in ROWS}
    for key in ARTIFACT_FILE_FLAGS:
        assert key in rows and rows[key].kind == "file" and rows[key].cls == "artifact", key


@pytest.mark.parametrize("tool,flag", ARTIFACT_FILE_FLAGS,
                         ids=[Path(t).stem for t, _f in ARTIFACT_FILE_FLAGS])
def test_an_artifact_flag_given_dash_is_refused_at_parse_time(tool, flag, tmp_path):
    row = next(r for r in ROWS if (r.tool, r.flag) == (tool, flag))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    argv = row.build(Ctx(tmp_path, "http://127.0.0.1:1"), Path("-"))
    assert "-" in argv, argv
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / tool), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=cwd,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 2, proc.stderr[-800:]
    assert f"error: argument {flag}: {REFUSAL}" in proc.stderr, proc.stderr[-800:]
    assert "Traceback" not in proc.stderr
    assert proc.stdout == ""
    assert list(cwd.iterdir()) == [], "the run made something in the working directory"


@pytest.mark.parametrize("tool,flag", ARTIFACT_FILE_FLAGS,
                         ids=[Path(t).stem for t, _f in ARTIFACT_FILE_FLAGS])
def test_an_artifact_flag_given_an_empty_value_is_refused_at_parse_time(tool, flag, tmp_path):
    """`--output "$UNSET"`: a tool that reads a falsy value as "not given"
    printed to stdout at rc 0 and left the file a pipeline meant to update."""
    row = next(r for r in ROWS if (r.tool, r.flag) == (tool, flag))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    marker = tmp_path / "@OUT@"
    argv = ["" if a == str(marker) else a
            for a in row.build(Ctx(tmp_path, "http://127.0.0.1:1"), marker)]
    assert "" in argv, argv
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / tool), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=cwd,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 2, proc.stderr[-800:]
    assert f"error: argument {flag}: an empty value names no file" in proc.stderr, \
        proc.stderr[-800:]
    assert proc.stdout == ""
    assert list(cwd.iterdir()) == []
