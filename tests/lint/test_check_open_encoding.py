"""Tests for scripts/tools/lint/check_open_encoding.py — scan-root handling (#1984).

The pre-commit hook passes explicit scan roots together with
``--strict-open-encoding``. A root that does not exist used to be skipped
silently: the tool scanned zero files and exited 0, which reads exactly
like a clean tree. These tests pin both directions of the fix:

  - a missing root is a caller error (exit 2), even when another root is
    fine and clean — it must never collapse into exit 0;
  - an existing root is scanned for real: a bare ``open()`` under it
    exits 1 in strict mode, and a clean one exits 0 (an existing root is
    not mistaken for a missing one).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_open_encoding.py"

_spec = importlib.util.spec_from_file_location("check_open_encoding", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_open_encoding"] = mod
_spec.loader.exec_module(mod)

_BARE = 'def f(p):\n    return open(p).read()\n'
_CLEAN = 'def f(p):\n    return open(p, encoding="utf-8").read()\n'


def _run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["check_open_encoding.py", *argv])
    return mod.main()


@pytest.fixture
def clean_root(tmp_path: Path) -> Path:
    root = tmp_path / "clean"
    root.mkdir()
    (root / "ok.py").write_text(_CLEAN, encoding="utf-8")
    return root


class TestMissingScanRoot:
    def test_missing_root_alone_is_caller_error(self, monkeypatch, capsys, tmp_path):
        missing = tmp_path / "no-such-dir"
        rc = _run(monkeypatch, "--ci", "--strict-open-encoding", str(missing))
        assert rc == 2
        assert str(missing) in capsys.readouterr().err

    def test_missing_root_next_to_a_clean_root_is_still_caller_error(
        self, monkeypatch, capsys, tmp_path, clean_root
    ):
        missing = tmp_path / "renamed-away"
        rc = _run(monkeypatch, "--ci", "--strict-open-encoding",
                  str(clean_root), str(missing))
        assert rc == 2
        assert str(missing) in capsys.readouterr().err

    def test_missing_root_is_caller_error_without_strict_too(self, monkeypatch, tmp_path):
        rc = _run(monkeypatch, "--ci", str(tmp_path / "no-such-dir"))
        assert rc == 2


class TestExistingScanRoot:
    def test_bare_open_under_existing_root_fails_strict(self, monkeypatch, capsys, tmp_path):
        root = tmp_path / "pkg" / "sub"
        root.mkdir(parents=True)
        (root / "bad.py").write_text(_BARE, encoding="utf-8")
        rc = _run(monkeypatch, "--ci", "--strict-open-encoding", str(tmp_path / "pkg"))
        assert rc == 1
        assert "bad.py:2" in capsys.readouterr().out

    def test_clean_existing_root_passes_strict(self, monkeypatch, capsys, clean_root):
        rc = _run(monkeypatch, "--ci", "--strict-open-encoding", str(clean_root))
        assert rc == 0
        assert capsys.readouterr().err == ""

    def test_existing_file_root_is_scanned(self, monkeypatch, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text(_BARE, encoding="utf-8")
        rc = _run(monkeypatch, "--ci", "--strict-open-encoding", str(bad))
        assert rc == 1
