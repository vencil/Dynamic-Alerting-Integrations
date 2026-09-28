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
import json
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


# ---------------------------------------------------------------------------
# subprocess text mode (#1374)
# ---------------------------------------------------------------------------

def _sp(tmp_path: Path, body: str) -> list[tuple[int, str, str]]:
    f = tmp_path / "probe.py"
    f.write_text(body, encoding="utf-8")
    return mod.scan_subprocess(f)


class TestSubprocessVerdict:
    @pytest.mark.parametrize("body", [
        "import subprocess\nsubprocess.run(c, capture_output=True, text=True)\n",
        "import subprocess\nsubprocess.run(c, capture_output=True, universal_newlines=True)\n",
        "import subprocess\nsubprocess.run(c, capture_output=True, errors='replace')\n",
        "import subprocess\nsubprocess.check_output(c, text=True)\n",
        "import subprocess\nsubprocess.Popen(c, stdout=subprocess.PIPE, text=True)\n",
        "import subprocess as sp\nsp.run(c, text=True)\n",
        "from subprocess import run as r\nr(c, text=True)\n",
        "import subprocess\nsubprocess.getoutput('git log')\n",
    ], ids=["text", "universal_newlines", "errors-only", "check_output", "Popen",
            "module-alias", "from-import-alias", "getoutput"])
    def test_text_mode_without_encoding_is_flagged(self, tmp_path, body):
        assert [line for line, _r, _s in _sp(tmp_path, body)] == [2]

    @pytest.mark.parametrize("body", [
        "import subprocess\nsubprocess.run(c, capture_output=True, text=True, encoding='utf-8')\n",
        "import subprocess\nsubprocess.run(c, capture_output=True, text=True, encoding='locale')\n",
        "import subprocess\nsubprocess.run(c, capture_output=True)\n",
        "import subprocess\nsubprocess.run(c, capture_output=True, text=False)\n",
        "import asyncio\nfrom somewhere import run\nrun(c, text=True)\n",
        "import subprocess\nsubprocess.run(\n    c,\n    text=True,  # open-encoding: ignore\n)\n",
    ], ids=["utf-8", "locale", "bytes", "text-False", "run-not-from-subprocess",
            "ignore-on-later-line"])
    def test_stated_encoding_bytes_or_foreign_call_passes(self, tmp_path, body):
        assert _sp(tmp_path, body) == []

    @pytest.mark.parametrize("body", [
        "import subprocess\nsubprocess.run(c, **kw)\n",
        "import subprocess\nsubprocess.run(c, capture_output=True, text=flag)\n",
        "import subprocess\nsubprocess.Popen(c, -1)\n",
    ], ids=["kwargs", "text-variable", "extra-positional"])
    def test_unreadable_call_is_flagged_as_unmeasurable(self, tmp_path, body):
        found = _sp(tmp_path, body)
        assert len(found) == 1 and found[0][1].startswith("unmeasurable")


def _tree(tmp_path: Path, sites: int) -> Path:
    root = tmp_path / "pkg"
    root.mkdir(exist_ok=True)
    body = "import subprocess\n" + "".join(
        f"subprocess.run(c{i}, capture_output=True, text=True)\n" for i in range(sites))
    (root / "a.py").write_text(body, encoding="utf-8")
    return root


def _ledger(tmp_path: Path, rows: dict[str, int] | None) -> Path:
    path = tmp_path / "ledger.json"
    if rows is not None:
        path.write_text(json.dumps({"files": rows}), encoding="utf-8")
    return path


def _key(root: Path) -> str:
    return mod.ledger_key(root / "a.py")


class TestSubprocessLedger:
    def _strict(self, monkeypatch, root: Path, ledger: Path) -> int:
        return _run(monkeypatch, "--ci", "--strict-open-encoding",
                    "--subprocess-baseline", str(ledger), str(root))

    def test_count_equal_to_ledger_passes(self, monkeypatch, tmp_path):
        root = _tree(tmp_path, 2)
        assert self._strict(monkeypatch, root, _ledger(tmp_path, {_key(root): 2})) == 0

    def test_one_more_than_ledger_fails_and_names_the_file(self, monkeypatch, capsys, tmp_path):
        root = _tree(tmp_path, 3)
        rc = self._strict(monkeypatch, root, _ledger(tmp_path, {_key(root): 2}))
        captured = capsys.readouterr()
        assert rc == 1
        assert "1 are new" in captured.err
        assert "a.py:4:" in captured.out

    def test_file_missing_from_ledger_fails(self, monkeypatch, tmp_path):
        root = _tree(tmp_path, 1)
        assert self._strict(monkeypatch, root, _ledger(tmp_path, {})) == 1

    def test_fewer_than_ledger_is_a_stale_row(self, monkeypatch, capsys, tmp_path):
        root = _tree(tmp_path, 1)
        rc = self._strict(monkeypatch, root, _ledger(tmp_path, {_key(root): 2}))
        assert rc == 1
        assert "stale ledger row" in capsys.readouterr().err

    def test_row_for_a_deleted_file_inside_the_roots_is_stale(self, monkeypatch, tmp_path):
        root = _tree(tmp_path, 1)
        gone = mod.ledger_key(root / "gone.py")
        rows = {_key(root): 1, gone: 1}
        assert self._strict(monkeypatch, root, _ledger(tmp_path, rows)) == 1

    def test_rows_outside_the_scanned_roots_are_not_compared(self, monkeypatch, tmp_path):
        root = _tree(tmp_path, 1)
        rows = {_key(root): 1, "scripts/elsewhere.py": 5}
        assert self._strict(monkeypatch, root, _ledger(tmp_path, rows)) == 0

    def test_missing_ledger_fails_strict(self, monkeypatch, tmp_path):
        root = _tree(tmp_path, 1)
        assert self._strict(monkeypatch, root, _ledger(tmp_path, None)) == 1

    def test_malformed_count_fails_strict(self, monkeypatch, tmp_path):
        root = _tree(tmp_path, 1)
        assert self._strict(monkeypatch, root, _ledger(tmp_path, {_key(root): 0})) == 1

    def test_without_strict_a_surplus_only_warns(self, monkeypatch, tmp_path):
        root = _tree(tmp_path, 3)
        rc = _run(monkeypatch, "--ci", "--subprocess-baseline",
                  str(_ledger(tmp_path, {})), str(root))
        assert rc == 0


class TestWriteSubprocessLedger:
    def _write(self, monkeypatch, root: Path, ledger: Path) -> int:
        return _run(monkeypatch, "--write-subprocess-baseline",
                    "--subprocess-baseline", str(ledger), str(root))

    def _rows(self, ledger: Path) -> dict[str, int]:
        return json.loads(ledger.read_text(encoding="utf-8"))["files"]

    def test_bootstrap_writes_every_count(self, monkeypatch, tmp_path):
        root = _tree(tmp_path, 2)
        ledger = _ledger(tmp_path, None)
        assert self._write(monkeypatch, root, ledger) == 0
        assert self._rows(ledger) == {_key(root): 2}

    def test_lowers_a_count(self, monkeypatch, tmp_path):
        root = _tree(tmp_path, 1)
        ledger = _ledger(tmp_path, {_key(root): 3})
        assert self._write(monkeypatch, root, ledger) == 0
        assert self._rows(ledger) == {_key(root): 1}

    def test_drops_a_row_at_zero_and_keeps_rows_outside_the_roots(self, monkeypatch, tmp_path):
        root = _tree(tmp_path, 0)
        ledger = _ledger(tmp_path, {_key(root): 2, "scripts/elsewhere.py": 5})
        assert self._write(monkeypatch, root, ledger) == 0
        assert self._rows(ledger) == {"scripts/elsewhere.py": 5}

    def test_refuses_to_raise_a_count(self, monkeypatch, capsys, tmp_path):
        root = _tree(tmp_path, 3)
        ledger = _ledger(tmp_path, {_key(root): 2})
        assert self._write(monkeypatch, root, ledger) == 1
        assert self._rows(ledger) == {_key(root): 2}
        assert "REFUSED" in capsys.readouterr().err

    def test_refuses_to_add_a_file(self, monkeypatch, tmp_path):
        root = _tree(tmp_path, 1)
        ledger = _ledger(tmp_path, {})
        assert self._write(monkeypatch, root, ledger) == 1
        assert self._rows(ledger) == {}
