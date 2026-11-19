"""A flag that names an output DIRECTORY refuses ``-`` at parse time (#3107).

``-`` is standard output for a single-file output flag
(``_lib_io.open_operator_output``). For a directory flag it names nothing:
the tools used to make a directory called ``-`` in the working directory and
exit 0. ``_lib_validation.output_dir_type`` is the argparse ``type=`` that
refuses it — rc 2, argparse's ``argument <flag>: ...`` line — and the tools
below declare it on their directory flag.

⚠️ The list is the tools #3107's second batch moved to the operator entry,
written out. It is not derived: nothing in the tree says "this flag is a
directory" except ``Row.kind`` in ``test_output_path_write_failure``, and
the other ``kind="dir"`` rows are tools that batch did not touch. A tool
that is not listed here is not held to anything by this file.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_python
import _lib_validation
from test_output_path_write_failure import ROWS, TOOLS_DIR, Ctx

#: (tool, flag) — the same strings as the rows of the write gate.
DIR_FLAGS = [
    ("dx/generate_tenant_fixture.py", "-o/--output"),
    ("dx/run_chaos_soak.py", "--output-dir"),
    ("ops/assemble_config_dir.py", "--output"),
    ("ops/baseline_discovery.py", "-o/--output-dir"),
    ("dx/waveform_compile.py", "--out"),
    ("ops/generate_rule_pack_split.py", "--output-dir"),
    ("ops/init_project.py", "-o/--output-dir"),
    ("ops/migrate_rule.py", "-o/--output-dir"),
    ("ops/migrate_to_operator.py", "--output-dir"),
    ("ops/onboard_platform.py", "-o/--output-dir"),
    ("ops/operator_generate.py", "--output-dir"),
    ("ops/scaffold_tenant.py", "-o/--output-dir"),
    ("ops/validate_migration.py", "-o/--output-dir"),
]

REFUSAL = "'-' is standard output, and this flag takes a directory"


# ── the type ───────────────────────────────────────────────────────────────
def test_dash_is_refused_and_nothing_else_is_judged():
    kind = _lib_validation.output_dir_type()
    with pytest.raises(argparse.ArgumentTypeError) as ei:
        kind("-")
    assert REFUSAL in str(ei.value)
    # Only the one value: what the directory is, and whether it can be made,
    # is the write's to say (rc 2 there too, naming the path).
    # #3107 ④: `-` with white space is another name, not `-` (a `.strip()`
    # in the comparison survived the ② mutations).
    for value in ("out", "./-", "-/", "--", "", " ", "/dev/null", "a/-", " -", "- ", "\t-"):
        assert kind(value) == value


def test_the_tools_own_type_runs_after_it():
    def own(value: str) -> Path:
        if not value.strip():
            raise argparse.ArgumentTypeError("must not be empty")
        return Path(value)
    kind = _lib_validation.output_dir_type(then=own)
    assert kind("out") == Path("out")
    with pytest.raises(argparse.ArgumentTypeError, match="must not be empty"):
        kind("")
    with pytest.raises(argparse.ArgumentTypeError) as ei:
        kind("-")
    assert REFUSAL in str(ei.value)


def test_argparse_names_the_flag_and_exits_2(capsys):
    parser = argparse.ArgumentParser(prog="t")
    parser.add_argument("-o", "--output-dir", default="out",
                        type=_lib_validation.output_dir_type())
    assert parser.parse_args([]).output_dir == "out"
    assert parser.parse_args(["-o", "x"]).output_dir == "x"
    with pytest.raises(SystemExit) as ei:
        parser.parse_args(["-o", "-"])
    assert ei.value.code == 2
    assert f"t: error: argument -o/--output-dir: {REFUSAL}" in capsys.readouterr().err


def test_the_facade_exports_it():
    assert _lib_python.output_dir_type is _lib_validation.output_dir_type
    assert _lib_python.OutputDirArg is _lib_validation.OutputDirArg


# ── the tools ──────────────────────────────────────────────────────────────
def test_every_listed_flag_is_a_directory_row_of_the_write_gate():
    rows = {(r.tool, r.flag): r for r in ROWS}
    for key in DIR_FLAGS:
        assert key in rows and rows[key].kind == "dir", key


@pytest.mark.parametrize("tool,flag", DIR_FLAGS, ids=[Path(t).stem for t, _f in DIR_FLAGS])
def test_a_directory_flag_given_dash_is_refused_at_parse_time(tool, flag, tmp_path):
    row = next(r for r in ROWS if (r.tool, r.flag) == (tool, flag))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # The row's own argv, with `-` as the directory. The stub URL is never
    # dialled: the refusal is argparse's, before the tool does anything.
    argv = row.build(Ctx(tmp_path, "http://127.0.0.1:1"), Path("-"))
    assert "-" in argv, argv
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / tool), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=cwd,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 2, proc.stderr[-800:]
    # argparse names the flag by its option strings in declaration order.
    names = {flag, "/".join(reversed(flag.split("/")))}
    assert any(f"error: argument {n}: {REFUSAL}" in proc.stderr for n in names), \
        proc.stderr[-800:]
    assert "Traceback" not in proc.stderr
    assert proc.stdout == ""
    assert list(cwd.iterdir()) == [], "the run made something in the working directory"
