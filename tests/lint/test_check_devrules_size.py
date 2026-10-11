"""Smoke tests for check_devrules_size.py — dev-rules.md line-count gate.

Covers:
  - `find_repo_root` is this tool's own tree, whatever the cwd (#2852)
  - `main` passes when dev-rules.md is at/under MAX_LINES
  - `main` fails with exit 1 when dev-rules.md exceeds MAX_LINES
  - `main` returns caller-error (exit 2) when dev-rules.md is missing
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import check_devrules_size as cds  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402


# ---------------------------------------------------------------------------
# find_repo_root
# ---------------------------------------------------------------------------
class TestFindRepoRoot:
    """#2852: the root is this tool's own tree, never the cwd's `.git`."""

    def test_a_cwd_inside_another_repo_does_not_move_it(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        nested = repo / "sub" / "nested"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert cds.find_repo_root() == Path(cds.__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
class TestMain:
    def _setup_fake_repo(self, tmp_path, dev_rules_content):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "docs" / "internal").mkdir(parents=True)
        if dev_rules_content is not None:
            (repo / "docs" / "internal" / "dev-rules.md").write_text(
                dev_rules_content, encoding="utf-8"
            )
        return repo

    def test_under_limit_passes(self, tmp_path, monkeypatch):
        content = "\n".join(f"line {i}" for i in range(100))  # 100 lines
        repo = self._setup_fake_repo(tmp_path, content)
        monkeypatch.setattr(cds, "find_repo_root", lambda: repo)
        monkeypatch.setattr("sys.argv", ["check_devrules_size.py"])
        assert cds.main() == 0

    def test_exactly_at_limit_passes(self, tmp_path, monkeypatch):
        content = "\n".join(f"line {i}" for i in range(cds.MAX_LINES))
        repo = self._setup_fake_repo(tmp_path, content)
        monkeypatch.setattr(cds, "find_repo_root", lambda: repo)
        monkeypatch.setattr("sys.argv", ["check_devrules_size.py"])
        assert cds.main() == 0

    def test_over_limit_fails(self, tmp_path, monkeypatch, capsys):
        content = "\n".join(f"line {i}" for i in range(cds.MAX_LINES + 50))
        repo = self._setup_fake_repo(tmp_path, content)
        monkeypatch.setattr(cds, "find_repo_root", lambda: repo)
        monkeypatch.setattr("sys.argv", ["check_devrules_size.py"])
        assert cds.main() == 1
        err = capsys.readouterr().err
        assert "FAIL" in err
        assert "Prune" in err or "Promote" in err or "Archive" in err

    def test_missing_file_is_caller_error(self, tmp_path, monkeypatch, capsys):
        repo = self._setup_fake_repo(tmp_path, dev_rules_content=None)
        monkeypatch.setattr(cds, "find_repo_root", lambda: repo)
        monkeypatch.setattr("sys.argv", ["check_devrules_size.py"])
        # Missing target file is a caller/environment error (#452).
        assert cds.main() == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "target not found" in err
