"""changelog_rebase_check.py 的測試。

oracle 的定義與它答不出來的事只寫在工具自己的 docstring，這裡不複製。
每個情境都在 tmp_path 建一個真的 git repo：base 之上 main 與 mine 各自分岔，
「rebase 後的結果」寫在 working tree（工具的預設讀法）。
"""
from __future__ import annotations

import importlib.util as _ilu
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL = _REPO_ROOT / "scripts" / "tools" / "dx" / "changelog_rebase_check.py"

_spec = _ilu.spec_from_file_location("changelog_rebase_check", _TOOL)
crc = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(crc)

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

HEADER = "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, env=_ENV, timeout=60)


def _commit(repo: Path, body: str, msg: str) -> None:
    (repo / "CHANGELOG.md").write_text(HEADER + body, encoding="utf-8",
                                       newline="\n")
    _git(repo, "add", "CHANGELOG.md")
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """base: A, B ─┬─ main: A, B2, C（上游改寫 B、新增 C）
                   └─ mine: A, B, M（我新增 M）"""
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, "- A\n- B\n", "base")
    _git(tmp_path, "checkout", "-q", "-b", "mine")
    _commit(tmp_path, "- A\n- B\n- M\n", "mine")
    _git(tmp_path, "checkout", "-q", "main")
    _commit(tmp_path, "- A\n- B2\n- C\n", "main")
    return tmp_path


def _after(repo: Path, body: str) -> dict:
    (repo / "CHANGELOG.md").write_text(HEADER + body, encoding="utf-8",
                                       newline="\n")
    return crc.check(repo, "CHANGELOG.md", "mine", "main", None, None)


def test_a_correct_rebase_matches_the_oracle(repo):
    r = _after(repo, "- M\n- A\n- B2\n- C\n")
    assert r["ok"] and r["missing"] == [] and r["extra"] == []


def test_an_upstream_bullet_dropped_by_the_rebase_is_missing(repo):
    r = _after(repo, "- M\n- A\n- B2\n")
    assert r["missing"] == ["- C"] and r["extra"] == []


def test_my_bullet_duplicated_is_extra_even_though_the_set_is_equal(repo):
    r = _after(repo, "- M\n- A\n- B2\n- C\n- M\n")
    assert r["missing"] == [] and r["extra"] == ["- M"]


def test_a_union_resolution_that_keeps_the_rewritten_bullet_twice_is_extra(repo):
    r = _after(repo, "- M\n- A\n- B\n- B2\n- C\n")
    assert r["extra"] == ["- B"]


def test_mine_union_main_would_have_called_the_rewrite_missing(repo):
    """反事實：`mine ∪ main` 會把上游改寫掉的 B 報成遺失；本 oracle 不會。"""
    r = _after(repo, "- M\n- A\n- B2\n- C\n")
    assert "- B" not in r["missing"]


def test_a_bullet_i_deleted_is_expected_to_stay_deleted(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, "- A\n- B\n", "base")
    _git(tmp_path, "checkout", "-q", "-b", "mine")
    _commit(tmp_path, "- B\n", "mine drops A")
    _git(tmp_path, "checkout", "-q", "main")
    _commit(tmp_path, "- A\n- B\n- C\n", "main")
    assert _after(tmp_path, "- B\n- C\n")["ok"]
    assert _after(tmp_path, "- A\n- B\n- C\n")["extra"] == ["- A"]


def test_main_exit_codes(repo, capsys):
    _after(repo, "- M\n- A\n- B2\n- C\n")
    assert crc.main(["--repo", str(repo), "--mine", "mine", "--main", "main"]) == 0
    _after(repo, "- M\n- A\n- B2\n")
    assert crc.main(["--repo", str(repo), "--mine", "mine", "--main", "main"]) == 1
    assert "MISSING (expected, absent): 1" in capsys.readouterr().out
    assert crc.main(["--repo", str(repo), "--mine", "no-such-ref",
                     "--main", "main"]) == 2


def test_after_can_be_a_ref(repo):
    _git(repo, "checkout", "-q", "-b", "rebased", "main")
    _commit(repo, "- M\n- A\n- B2\n- C\n", "rebased")
    r = crc.check(repo, "CHANGELOG.md", "mine", "main", None, "rebased")
    assert r["ok"] and r["refs"]["after"] != "working tree"


def test_cli_runs_as_a_script(repo):
    _after(repo, "- M\n- A\n- B2\n- C\n")
    proc = subprocess.run(
        [sys.executable, str(_TOOL), "--repo", str(repo), "--mine", "mine",
         "--main", "main"],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.rstrip().startswith("CHANGELOG.md: base=")
    assert proc.stdout.rstrip().splitlines()[-1].startswith("OK — ")
