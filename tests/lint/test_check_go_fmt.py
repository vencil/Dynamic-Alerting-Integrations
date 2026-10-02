"""Tests for check_go_fmt.py and its pre-commit wiring — #2583.

Two layers:

- Logic, against a stub ``gofmt`` on a private PATH. Deterministic on any
  host, Go or no Go, so the red/green and fail-closed contracts are always
  exercised (a skip-when-no-go test would be vacuous on a runner without Go).
- Real gofmt, when the host has one: the stub is only as good as its model of
  ``gofmt -l``, so the same contracts are re-checked against the real binary.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_TOOLS_DIR = ROOT / "scripts" / "tools" / "lint"
sys.path.insert(0, str(_TOOLS_DIR))

import check_go_fmt as lint  # noqa: E402

SCRIPT = _TOOLS_DIR / "check_go_fmt.py"
UNFORMATTED = "package x\nfunc  F( ){ }\n"
FORMATTED = "package x\n\nfunc F() {}\n"
UNPARSABLE = "package x\nfunc F( {\n"
# gofmt-clean, but `gofmt -s` rewrites `[]T{T{A: 1}}` to `[]T{{A: 1}}`. CI's
# golangci-lint gofmt formatter defaults to simplify: true (#2583 F1).
SIMPLIFIABLE = "package x\n\ntype T struct{ A int }\n\nvar X = []T{T{A: 1}}\n"

# Stub gofmt, modelling the real one closely enough to tell argv shapes apart:
# - flags must be followed by `--`, else rc 3 (so dropping `--` is visible);
# - only FORMATTED is clean; SIMPLIFIABLE is listed only under `-s`;
# - UNPARSABLE goes to stderr with rc 2 while other files are still listed;
# - a missing path crashes it (rc 4): the script must pre-check instead.
_STUB_GOFMT = f"""#!/usr/bin/env python3
import os, sys
argv = sys.argv[1:]
if "--" not in argv:
    sys.stderr.write("stub: no -- before paths\\n"); sys.exit(3)
i = argv.index("--")
flags, paths = argv[:i], argv[i + 1:]
rc = 0
for p in paths:
    if not os.path.isfile(p):
        sys.stderr.write("stub: crashed on missing " + p + "\\n"); sys.exit(4)
    text = open(p, encoding="utf-8").read()
    if text == {UNPARSABLE!r}:
        sys.stderr.write(p + ":2:9: expected ')', found '{{'\\n"); rc = 2
    elif text == {SIMPLIFIABLE!r}:
        if "-s" in flags:
            print(p)
    elif text != {FORMATTED!r}:
        print(p)
sys.exit(rc)
"""


def _exe(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def stub_path(tmp_path, monkeypatch):
    """PATH holding only a stub gofmt (and the python the stub needs)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "gofmt", _STUB_GOFMT.replace(
        "#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    monkeypatch.setenv("PATH", str(bindir))
    return bindir


@pytest.fixture
def empty_path(tmp_path, monkeypatch):
    bindir = tmp_path / "nobin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    return bindir


def _go(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# --- red / green (stub) -------------------------------------------------------

def test_unformatted_file_fails_and_is_named(stub_path, tmp_path, capsys):
    bad = _go(tmp_path, "bad.go", UNFORMATTED)
    good = _go(tmp_path, "good.go", FORMATTED)
    assert lint.main([bad, good]) == lint.EXIT_VIOLATION
    out = capsys.readouterr().out
    assert bad in out
    assert good not in out


def test_formatted_files_pass(stub_path, tmp_path):
    good = _go(tmp_path, "good.go", FORMATTED)
    assert lint.main([good]) == lint.EXIT_OK


def test_gofmt_error_is_cannot_measure_not_pass(stub_path, tmp_path, capsys):
    # gofmt -l does not list a file it cannot parse; only the rc says so.
    broken = _go(tmp_path, "broken.go", UNPARSABLE)
    assert lint.main([broken]) == lint.EXIT_CALLER_ERROR
    assert "cannot measure" in capsys.readouterr().err


def test_missing_file_is_cannot_measure(stub_path, tmp_path, capsys):
    gone = str(tmp_path / "gone.go")
    good = _go(tmp_path, "good.go", FORMATTED)
    assert lint.main([good, gone]) == lint.EXIT_CALLER_ERROR
    err = capsys.readouterr().err
    # The pre-check names it; the stub never ran (it would say "crashed").
    assert f"no such file: {gone}" in err
    assert "crashed" not in err


def test_simplifiable_file_fails_like_ci(stub_path, tmp_path, capsys):
    # #2583 F1: plain `gofmt -l` passes this; CI (gofmt simplify) rejects it.
    simp = _go(tmp_path, "simp.go", SIMPLIFIABLE)
    assert lint.main([simp]) == lint.EXIT_VIOLATION
    assert simp in capsys.readouterr().out


def test_syntax_error_still_lists_other_unformatted_files(stub_path, tmp_path, capsys):
    # #2583 F2: gofmt rc 2 on one file must not hide the others it listed.
    bad = _go(tmp_path, "bad.go", UNFORMATTED)
    broken = _go(tmp_path, "broken.go", UNPARSABLE)
    good = _go(tmp_path, "good.go", FORMATTED)
    assert lint.main([bad, broken, good]) == lint.EXIT_CALLER_ERROR
    cap = capsys.readouterr()
    assert bad in cap.out
    assert good not in cap.out
    assert "cannot measure" in cap.err
    assert "expected ')'" in cap.err


def test_dash_leading_file_name_is_a_path(stub_path, tmp_path, monkeypatch, capsys):
    # pre-commit appends file names after the entry's `--`; gofmt gets `--` too.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "-x.go").write_text(UNFORMATTED, encoding="utf-8")
    assert lint.main(["--", "-x.go"]) == lint.EXIT_VIOLATION
    assert "-x.go" in capsys.readouterr().out


def test_no_files_is_ok_and_says_nothing_was_measured(stub_path, capsys):
    assert lint.main([]) == lint.EXIT_OK
    assert "nothing measured" in capsys.readouterr().err


def test_never_rewrites_the_file(stub_path, tmp_path):
    bad = _go(tmp_path, "bad.go", UNFORMATTED)
    lint.main([bad])
    assert Path(bad).read_text(encoding="utf-8") == UNFORMATTED


# --- missing toolchain: fail-closed -------------------------------------------

def test_no_gofmt_and_no_go_fails_closed(empty_path, tmp_path, capsys):
    good = _go(tmp_path, "good.go", FORMATTED)
    assert lint.locate_gofmt() is None
    assert lint.main([good]) == lint.EXIT_CALLER_ERROR
    err = capsys.readouterr().err
    assert "cannot measure" in err
    assert f"SKIP={lint.HOOK_ID}" in err


def test_falls_back_to_gofmt_under_go_env_goroot(tmp_path, monkeypatch):
    goroot = tmp_path / "goroot"
    (goroot / "bin").mkdir(parents=True)
    gofmt = _exe(goroot / "bin" / "gofmt", _STUB_GOFMT.replace(
        "#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    bindir = tmp_path / "gobin"
    bindir.mkdir()
    _exe(bindir / "go", f"#!/bin/sh\necho '{goroot}'\n")
    monkeypatch.setenv("PATH", str(bindir))
    assert lint.locate_gofmt() == str(gofmt)
    bad = _go(tmp_path, "bad.go", UNFORMATTED)
    assert lint.main([bad]) == lint.EXIT_VIOLATION


def test_go_whose_goroot_has_no_gofmt_fails_closed(tmp_path, monkeypatch):
    bindir = tmp_path / "gobin"
    bindir.mkdir()
    _exe(bindir / "go", f"#!/bin/sh\necho '{tmp_path / 'nowhere'}'\n")
    monkeypatch.setenv("PATH", str(bindir))
    good = _go(tmp_path, "good.go", FORMATTED)
    assert lint.main([good]) == lint.EXIT_CALLER_ERROR


# --- real gofmt ---------------------------------------------------------------

_REAL_GOFMT = lint.locate_gofmt()
_needs_gofmt = pytest.mark.skipif(
    _REAL_GOFMT is None, reason="no gofmt/go on PATH; stub tests above still ran")


@_needs_gofmt
def test_real_gofmt_red_then_green(tmp_path):
    bad = _go(tmp_path, "bad.go", UNFORMATTED)
    good = _go(tmp_path, "good.go", FORMATTED)
    assert lint.unformatted(_REAL_GOFMT, [bad, good]) == [bad]
    assert lint.unformatted(_REAL_GOFMT, [good]) == []


@_needs_gofmt
def test_real_gofmt_parse_error_raises_and_keeps_listed(tmp_path):
    bad = _go(tmp_path, "bad.go", UNFORMATTED)
    broken = _go(tmp_path, "broken.go", UNPARSABLE)
    with pytest.raises(lint.MeasureError) as exc:
        lint.unformatted(_REAL_GOFMT, [bad, broken])
    assert exc.value.listed == [bad]


@_needs_gofmt
def test_real_gofmt_simplify_is_on(tmp_path):
    simp = _go(tmp_path, "simp.go", SIMPLIFIABLE)
    assert lint.unformatted(_REAL_GOFMT, [simp]) == [simp]


@_needs_gofmt
def test_real_gofmt_dash_leading_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "-x.go").write_text(UNFORMATTED, encoding="utf-8")
    assert lint.unformatted(_REAL_GOFMT, ["-x.go"]) == ["-x.go"]


# --- pre-commit wiring --------------------------------------------------------

def _hook() -> dict:
    cfg = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [h for r in cfg["repos"] for h in r["hooks"] if h["id"] == lint.HOOK_ID]
    assert len(hooks) == 1, f"expected exactly one {lint.HOOK_ID!r} hook, got {len(hooks)}"
    return hooks[0]


def _go_modules() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", "*go.mod"], cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", timeout=60, check=True,
    ).stdout.split()
    return sorted(str(Path(p).parent) for p in out)


def test_hook_runs_this_script_on_staged_files_at_commit():
    hook = _hook()
    assert "scripts/tools/lint/check_go_fmt.py" in hook["entry"]
    # pre-commit appends file names after the entry: `--` keeps `-x.go` a path.
    assert hook["entry"].split()[-1] == "--"
    # Staged files must reach the script, and it must run at commit time.
    assert hook.get("pass_filenames", True) is True
    assert "manual" not in hook.get("stages", [])
    # Never rewrite (dev-rules): no gofmt -w / --fix style flag in the entry.
    assert not re.search(r"\s-w\b|--fix|--write", hook["entry"])


def test_hook_file_filter_covers_every_go_module():
    modules = _go_modules()
    assert "components/tenant-api" in modules, modules  # anti-vacuity anchor
    assert len(modules) >= 2, modules
    pattern = re.compile(_hook()["files"])
    for mod in modules:
        assert pattern.search(f"{mod}/x.go"), f"{mod} not covered"
        assert pattern.search(f"{mod}/sub/x_test.go"), f"{mod} subdir not covered"


@pytest.mark.parametrize("path", [
    "components/tenant-api/go.mod",
    "components/tenant-api/go.sum",
    "scripts/ops/golangci_floor_probe.go.txt",
    "scripts/tools/lint/check_go_fmt.py",
])
def test_hook_file_filter_skips_non_go(path):
    # A non-.go path would be passed to gofmt and fail the commit as a parse error.
    assert not re.search(_hook()["files"], path)


def test_cli_entrypoint_runs(tmp_path):
    """The script runs as pre-commit invokes it (python3 -X utf8 <script> files)."""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT)], capture_output=True,
        text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
