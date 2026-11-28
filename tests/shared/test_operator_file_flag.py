"""A flag that names ONE operator output file, declared with
``_lib_validation.operator_file_type`` (#3107 ③b).

The operator entry (``_lib_io.write_operator_output``) writes ``-`` to
standard output. Owner ruling 2026-11-15: where the tool's standard output
already has an owner (its report, ``--json``), ``-`` is refused at parse
time (rc 2, argparse's ``argument <flag>: ...`` line) and the message says
where to get the document instead; otherwise ``-`` IS standard output. An
empty value (an unset variable) is refused everywhere: tools read a falsy
value as "not given" and would write nothing at rc 0.

⚠️ The list is written out, not derived (as in ``test_output_dir_flag``):
nothing in the tree says which tool owns its stdout. Every entry must be an
``operator`` file row of the write gate. A flag not listed is held to
nothing here.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_validation
from _platform_fs import symlink_or_skip
from test_output_path_write_failure import ROWS, TOOLS_DIR, Ctx, stub_url  # noqa: F401

#: (tool, flag) → whether `-` is refused (the tool's stdout has another owner).
OPERATOR_FILE_FLAGS: dict[tuple[str, str], bool] = {
    ("dx/describe_tenant.py", "-o/--output"): False,
    ("dx/generate_changelog.py", "-o/--output"): False,
    ("dx/migrate_conf_d.py", "-o/--output-plan"): True,
    ("dx/pair_bench_ratio.py", "--out"): True,
    ("dx/paired_trend_watch.py", "--summary-file"): True,
    ("dx/render_soak_diff.py", "--output"): False,
    ("dx/scan_component_health.py", "--output"): True,
    ("dx/waveform_score.py", "--out"): True,
    ("ops/analyze_rule_pack_gaps.py", "-o/--output"): True,
    ("ops/backtest_threshold.py", "-o/--output"): True,
    ("ops/backtest_threshold.py", "--markdown-output"): True,
    ("ops/batch_diagnose.py", "-o/--output"): True,
    ("ops/validate_migration.py", "--convergence-output"): True,
}

DASH_REFUSAL = "'-' is standard output, and "
EMPTY_REFUSAL = "an empty value names no file"


# ── the type ───────────────────────────────────────────────────────────────
def test_dash_is_refused_only_where_stdout_has_another_owner():
    owned = _lib_validation.operator_file_type(stdout="the report is printed there")
    with pytest.raises(argparse.ArgumentTypeError) as ei:
        owned("-")
    assert str(ei.value).startswith(DASH_REFUSAL + "the report is printed there")
    assert _lib_validation.operator_file_type()("-") == "-"
    for kind in (owned, _lib_validation.operator_file_type()):
        for value in ("out.json", "./-", "--", " ", "/dev/null", "a/-"):
            assert kind(value) == value


def test_an_empty_value_is_refused_with_or_without_an_owner():
    for kind in (_lib_validation.operator_file_type(),
                 _lib_validation.operator_file_type(stdout="x")):
        with pytest.raises(argparse.ArgumentTypeError, match=EMPTY_REFUSAL):
            kind("")


def test_the_tools_own_type_runs_after_it():
    kind = _lib_validation.operator_file_type(then=Path)
    assert kind("r.json") == Path("r.json")


@pytest.mark.parametrize("stdout", [None, "x"])
def test_a_then_that_collapses_dot_dash_to_dash_still_names_the_file(stdout):
    """`Path("./-")` is `Path("-")`, which the operator entry writes to
    standard output (#3982): the file the operator named is kept by an
    absolute name. `-` itself is untouched."""
    kind = _lib_validation.operator_file_type(stdout=stdout, then=Path)
    assert kind("./-") == Path(os.path.abspath("-"))
    if stdout is None:
        assert kind("-") == Path("-")


def test_argparse_names_the_flag_and_exits_2(capsys):
    parser = argparse.ArgumentParser(prog="t")
    parser.add_argument("-o", "--output",
                        type=_lib_validation.operator_file_type(stdout="--json prints it"))
    assert parser.parse_args(["-o", "x"]).output == "x"
    with pytest.raises(SystemExit) as ei:
        parser.parse_args(["-o", "-"])
    assert ei.value.code == 2
    assert f"t: error: argument -o/--output: {DASH_REFUSAL}--json prints it" in capsys.readouterr().err


# ── the tools ──────────────────────────────────────────────────────────────
def test_every_listed_flag_is_an_operator_file_row_of_the_write_gate():
    rows = {(r.tool, r.flag): r for r in ROWS}
    for key in OPERATOR_FILE_FLAGS:
        assert key in rows and rows[key].kind == "file" and rows[key].cls == "operator", key


def _refused_by_name(stderr: str, flag: str, why: str) -> bool:
    """argparse names the flag by its option strings in declaration order
    (`-o/--output` or `--output/-o`)."""
    names = {flag, "/".join(reversed(flag.split("/")))}
    return any(f"error: argument {n}: {why}" in stderr for n in names)


def _run(tool: str, flag: str, value: str, tmp_path: Path, stub: str):
    row = next(r for r in ROWS if (r.tool, r.flag) == (tool, flag))
    ctx = Ctx(tmp_path, stub)
    # The row's own working directory when it has one (generate_changelog
    # runs in a git tree), else an empty one.
    if row.cwd is not None:
        cwd = row.cwd(ctx)
    else:
        cwd = tmp_path / "cwd"
        cwd.mkdir()
    # The row's own argv with its output path swapped for *value* (a Path
    # cannot carry "" — it reads it as ".").
    marker = tmp_path / "@OUT@"
    argv = [value if a == str(marker) else a for a in row.build(ctx, marker)]
    assert value in argv, argv
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if row.env is not None:
        env.update(row.env(ctx))
    before = set(os.listdir(cwd))
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / tool), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, cwd=cwd, env=env)
    return proc, _Made(cwd, before)


class _Made:
    """What a run added to its working directory (a row's own cwd is not
    empty to begin with)."""

    def __init__(self, cwd: Path, before: set[str]) -> None:
        self.cwd, self.before = cwd, before

    def iterdir(self):
        return [self.cwd / n for n in sorted(set(os.listdir(self.cwd)) - self.before)]


_IDS = [f"{Path(t).stem}[{f}]" for t, f in OPERATOR_FILE_FLAGS]


@pytest.mark.parametrize("tool,flag", list(OPERATOR_FILE_FLAGS), ids=_IDS)
def test_an_empty_value_is_refused_at_parse_time(tool, flag, tmp_path, stub_url):
    proc, cwd = _run(tool, flag, "", tmp_path, stub_url)
    assert proc.returncode == 2, proc.stderr[-800:]
    assert _refused_by_name(proc.stderr, flag, EMPTY_REFUSAL), proc.stderr[-800:]
    assert proc.stdout == ""
    assert list(cwd.iterdir()) == []


@pytest.mark.parametrize("tool,flag", [k for k, v in OPERATOR_FILE_FLAGS.items() if v],
                         ids=[i for i, v in zip(_IDS, OPERATOR_FILE_FLAGS.values()) if v])
def test_dash_is_refused_where_stdout_is_taken(tool, flag, tmp_path, stub_url):
    proc, cwd = _run(tool, flag, "-", tmp_path, stub_url)
    assert proc.returncode == 2, proc.stderr[-800:]
    assert _refused_by_name(proc.stderr, flag, DASH_REFUSAL), proc.stderr[-800:]
    assert "Traceback" not in proc.stderr
    assert proc.stdout == ""
    assert list(cwd.iterdir()) == [], "the run made something in the working directory"


@pytest.mark.parametrize("tool,flag", [k for k, v in OPERATOR_FILE_FLAGS.items() if not v],
                         ids=[i for i, v in zip(_IDS, OPERATOR_FILE_FLAGS.values()) if not v])
def test_dash_is_standard_output_where_nothing_else_writes_it(tool, flag, tmp_path, stub_url):
    """Before #3107 ③b this wrote a file called `-`."""
    proc, cwd = _run(tool, flag, "-", tmp_path, stub_url)
    assert proc.returncode == 0, proc.stderr[-800:]
    assert proc.stdout.strip(), "the document did not reach stdout"
    # The tool's receipts stay on stderr: stdout is the document alone.
    assert "Written to" not in proc.stdout and "Generated " not in proc.stdout, proc.stdout[-400:]
    assert list(cwd.iterdir()) == [], "a file called - was made"


@pytest.mark.parametrize("tool,flag", list(OPERATOR_FILE_FLAGS), ids=_IDS)
def test_dot_dash_is_a_file_called_dash(tool, flag, tmp_path, stub_url):
    """The way out both refusals name: `./-` writes a file called `-`,
    never standard output (a `then=Path` collapsed it to `-`, #3982)."""
    proc, cwd = _run(tool, flag, "./-", tmp_path, stub_url)
    assert proc.returncode in (0, 1), proc.stderr[-800:]
    made = [p.name for p in cwd.iterdir()]
    assert "-" in made, (made, proc.stderr[-400:])
    assert (cwd.cwd / "-").stat().st_size > 0


@pytest.mark.skipif(os.name != "posix", reason="fd 1 redirected to a file")
@pytest.mark.parametrize("tool,flag", [k for k, v in OPERATOR_FILE_FLAGS.items() if v],
                         ids=[i for i, v in zip(_IDS, OPERATOR_FILE_FLAGS.values()) if v])
def test_another_name_of_stdout_is_refused_where_stdout_is_taken(tool, flag, tmp_path, stub_url):
    """`-o F > F` (and `/dev/stdout` on a pipe) is `-` to the operator
    entry: written through stdout, beside the report or the --json document.
    Refused at parse time like `-`, the file left as it was."""
    row = next(r for r in ROWS if (r.tool, r.flag) == (tool, flag))
    ctx = Ctx(tmp_path, stub_url)
    target = tmp_path / "taken.out"
    target.write_text("kept\n", encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if row.env is not None:
        env.update(row.env(ctx))
    cmd = [sys.executable, str(TOOLS_DIR / tool), *row.build(ctx, target)]
    with open(target, "ab") as fd1:
        proc = subprocess.run(cmd, stdout=fd1, stderr=subprocess.PIPE, timeout=180,
                              env=env, cwd=tmp_path)
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 2, err[-800:]
    assert _refused_by_name(err, flag, f"{str(target)!r} is this run's standard output"), err[-800:]
    assert target.read_text(encoding="utf-8") == "kept\n"
    proc = subprocess.run([sys.executable, str(TOOLS_DIR / tool),
                           *row.build(ctx, Path("/dev/stdout"))],
                          capture_output=True, timeout=180, env=env, cwd=tmp_path)
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 2, err[-800:]
    assert _refused_by_name(err, flag, "'/dev/stdout' is this run's standard output"), err[-800:]
    assert proc.stdout == b""


#: Flags whose file is the JSON document `--json` prints.
JSON_DOCUMENT_FLAGS = [
    ("dx/waveform_score.py", "--out"),
    ("ops/analyze_rule_pack_gaps.py", "-o/--output"),
    ("ops/backtest_threshold.py", "-o/--output"),
    ("ops/batch_diagnose.py", "-o/--output"),
]


@pytest.mark.parametrize("tool,flag", JSON_DOCUMENT_FLAGS,
                         ids=[f"{Path(t).stem}[{f}]" for t, f in JSON_DOCUMENT_FLAGS])
def test_the_file_is_byte_for_byte_what_json_prints(tool, flag, tmp_path, stub_url):
    """#3107 ③b: one encoder (`json_file_text`) for the file and for
    --json's stdout — same bytes, trailing newline included; and under
    --json the file is still written."""
    row = next(r for r in ROWS if (r.tool, r.flag) == (tool, flag))
    ctx = Ctx(tmp_path, stub_url)
    target = tmp_path / "doc.json"
    argv = row.build(ctx, target)
    if "--json" not in argv:
        argv = [*argv, "--json"]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if row.env is not None:
        env.update(row.env(ctx))
    proc = subprocess.run([sys.executable, str(TOOLS_DIR / tool), *argv],
                          capture_output=True, timeout=180, env=env, cwd=tmp_path)
    assert proc.returncode in (0, 1), proc.stderr.decode("utf-8", "replace")[-800:]
    assert proc.stdout.strip(), "no --json document"
    assert target.read_bytes() == proc.stdout


def test_a_dangling_link_gets_its_targets_directory_made(tmp_path, stub_url):
    """`scan_component_health --output` made the RESOLVED parent before
    #3982: a link to a file in a directory not made yet still works."""
    row = next(r for r in ROWS if (r.tool, r.flag) == ("dx/scan_component_health.py", "--output"))
    ctx = Ctx(tmp_path, stub_url)
    target = tmp_path / "tdir" / "sub" / "h.json"
    link = tmp_path / "link.json"
    symlink_or_skip(target, link)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if row.env is not None:
        env.update(row.env(ctx))
    proc = subprocess.run([sys.executable, str(TOOLS_DIR / row.tool), *row.build(ctx, link)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=180, env=env, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr[-800:]
    assert link.is_symlink() and target.stat().st_size > 0


@pytest.mark.parametrize("value", ["-", "/dev/stdout"])
def test_the_step_summary_variable_is_held_to_the_flags_type(value, tmp_path, stub_url):
    """`$GITHUB_STEP_SUMMARY` fills `--summary-file`'s slot: `-` (or another
    name of stdout) there would print the body twice, so it is refused at
    parse time the same way, the variable named."""
    row = next(r for r in ROWS if (r.tool, r.flag) == ("dx/paired_trend_watch.py", "--summary-file"))
    ctx = Ctx(tmp_path, stub_url)
    marker = tmp_path / "@OUT@"
    argv = row.build(ctx, marker)
    i = argv.index(str(marker))
    assert argv[i - 1] == "--summary-file", argv
    argv = argv[:i - 1] + argv[i + 1:]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "GITHUB_STEP_SUMMARY": value}
    if row.env is not None:
        env.update(row.env(ctx))
    proc = subprocess.run([sys.executable, str(TOOLS_DIR / row.tool), *argv],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=180, env=env, cwd=tmp_path)
    assert proc.returncode == 2, proc.stderr[-800:]
    assert "error: $GITHUB_STEP_SUMMARY: " in proc.stderr, proc.stderr[-800:]
    assert proc.stdout == ""
    assert not (tmp_path / "-").exists()
