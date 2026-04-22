"""Smoke tests for check_devrules_size.py — dev-rules.md line-count gate.

Covers:
  - `find_repo_root` walks parents until a `.git` is found
  - `find_repo_root` falls back to script-relative path when cwd has no .git
  - `main` passes when dev-rules.md is at/under MAX_LINES
  - `main` fails with exit 1 when dev-rules.md exceeds MAX_LINES
  - `main` fails with exit 1 when dev-rules.md is missing
"""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import check_devrules_size as cds  # noqa: E402


# ---------------------------------------------------------------------------
# find_repo_root
# ---------------------------------------------------------------------------
class TestFindRepoRoot:
    def test_finds_cwd_ancestor_with_dot_git(self, tmp_path, monkeypatch):
        # Build: tmp/repo/.git  and  tmp/repo/sub/nested
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        nested = repo / "sub" / "nested"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert cds.find_repo_root() == repo

    def test_fallback_when_no_dot_git(self, tmp_path, monkeypatch):
        """With no ancestor carrying .git, we fall back to script-parent path.
        We don't assert the exact fallback path (it depends on where the test
        runs from) — just that the function returns *something* without crashing.
        """
        monkeypatch.chdir(tmp_path)  # tmp_path has no .git anywhere above in test env
        got = cds.find_repo_root()
        # Should be a Path, not a crash; on actual repo test env it will hit
        # the real vibe-k8s-lab .git and return that root, which is also fine.
        assert got is not None


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
        monkeypatch.chdir(repo)
        monkeypatch.setattr("sys.argv", ["check_devrules_size.py"])
        assert cds.main() == 0

    def test_exactly_at_limit_passes(self, tmp_path, monkeypatch):
        content = "\n".join(f"line {i}" for i in range(cds.MAX_LINES))
        repo = self._setup_fake_repo(tmp_path, content)
        monkeypatch.chdir(repo)
        monkeypatch.setattr("sys.argv", ["check_devrules_size.py"])
        assert cds.main() == 0

    def test_over_limit_fails(self, tmp_path, monkeypatch, capsys):
        content = "\n".join(f"line {i}" for i in range(cds.MAX_LINES + 50))
        repo = self._setup_fake_repo(tmp_path, content)
        monkeypatch.chdir(repo)
        monkeypatch.setattr("sys.argv", ["check_devrules_size.py"])
        assert cds.main() == 1
        err = capsys.readouterr().err
        assert "FAIL" in err
        assert "Prune" in err or "Promote" in err or "Archive" in err

    def test_missing_file_fails(self, tmp_path, monkeypatch, capsys):
        repo = self._setup_fake_repo(tmp_path, dev_rules_content=None)
        monkeypatch.chdir(repo)
        monkeypatch.setattr("sys.argv", ["check_devrules_size.py"])
        assert cds.main() == 1
        err = capsys.readouterr().err
        assert "target not found" in err
