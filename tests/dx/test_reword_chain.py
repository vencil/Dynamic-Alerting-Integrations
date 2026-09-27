"""reword_chain must not drop the commits after the last mapping entry (#2014).

The rewritten chain replaces the target branch up to its tip. A mapping that
stopped short of the tip used to exit 0 with every later commit gone from the
branch (and its files gone from the working tree). The tool now refuses with
exit 2 before writing anything, ``--dry-run`` included.

Both directions run against a throwaway repo with real git:

* mapping stops short of the tip -> 2, branch ref, HEAD and tags untouched;
* mapping reaches the tip        -> 0, subjects rewritten, trees and
  author/committer dates unchanged (the check must not reject the legal use);
* the check follows ``--branch``: a mapping that reaches the tip of the named
  branch is accepted even when the checked-out branch is longer, and
  ``--branch ''`` (print only, no ref update) drops nothing so it stays legal.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx"))

import reword_chain  # noqa: E402

_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True,
                          text=True, encoding="utf-8", timeout=30,
                          env={**os.environ, **_GIT_ENV}).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """Three commits c1..c3 on `main`, cwd inside the repo."""
    for k, v in _GIT_ENV.items():
        monkeypatch.setenv(k, v)
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    for i in (1, 2, 3):
        (r / f"f{i}").write_text(f"{i}\n", encoding="utf-8")
        _git(r, "add", f"f{i}")
        _git(r, "commit", "-q", "-m", f"c{i} old")
    monkeypatch.chdir(r)
    return r


def _mapping(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "map.tsv"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _run(argv: list[str]) -> int:
    try:
        return reword_chain.main(argv)
    except SystemExit as exc:
        return exc.code


def _state(repo: Path) -> tuple[str, str, str]:
    return (_git(repo, "rev-parse", "refs/heads/main"), _git(repo, "rev-parse", "HEAD"),
            _git(repo, "tag", "--list"))


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry-run"])
def test_mapping_short_of_the_tip_is_refused(repo, tmp_path, capsys, dry_run):
    c2 = _git(repo, "rev-parse", "HEAD~1")
    before = _state(repo)
    argv = [str(_mapping(tmp_path, [f"{c2}\tc2 NEW subject"]))] + (["--dry-run"] if dry_run else [])
    rc = _run(argv)
    err = capsys.readouterr().err
    assert rc == 2, err
    assert "1 later commit(s) would be dropped" in err, err
    assert _state(repo) == before
    assert (repo / "f3").is_file()


def test_mapping_reaching_the_tip_rewrites_and_preserves(repo, tmp_path):
    c2, c3 = _git(repo, "rev-parse", "HEAD~1"), _git(repo, "rev-parse", "HEAD")
    fmt = "%T %aI %cI %an %cn"
    old = _git(repo, "log", f"--format={fmt}", "-3")
    rc = _run([str(_mapping(tmp_path, [f"{c2}\tc2 NEW subject", f"{c3}\t-"]))])
    assert rc == 0
    assert _git(repo, "log", "--format=%s", "-3").splitlines() == ["c3 old", "c2 NEW subject", "c1 old"]
    assert _git(repo, "log", f"--format={fmt}", "-3") == old
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "refs/heads/main") != c3


def test_named_branch_tip_is_what_counts(repo, tmp_path):
    """`--branch feature` whose tip is c2: listing up to c2 drops nothing there."""
    c2 = _git(repo, "rev-parse", "HEAD~1")
    _git(repo, "branch", "feature", c2)
    main_before = _git(repo, "rev-parse", "refs/heads/main")
    rc = _run([str(_mapping(tmp_path, [f"{c2}\tc2 NEW subject"])), "--branch", "feature"])
    assert rc == 0
    assert _git(repo, "log", "--format=%s", "-1", "feature") == "c2 NEW subject"
    assert _git(repo, "rev-parse", "refs/heads/main") == main_before


def test_skip_ref_update_drops_nothing_and_stays_legal(repo, tmp_path):
    c2 = _git(repo, "rev-parse", "HEAD~1")
    before = _state(repo)[:2]
    rc = _run([str(_mapping(tmp_path, [f"{c2}\tc2 NEW subject"])), "--branch", ""])
    assert rc == 0
    assert _state(repo)[:2] == before
