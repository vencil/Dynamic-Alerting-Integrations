"""A failed ``git diff`` must never read as "no offenders" (#1987).

Three diff-only lints listed changed files with ``git diff --name-only`` and
turned a git failure or timeout into an empty list, so the tool printed OK and
exited 0 even with a violation staged. An explicit ``--diff-base`` skips the
``rev-parse --verify`` check in ``resolve_diff_base``, so a bad ref reached git
directly. They now go through ``_lint_helpers.diff_changed_paths``, which
raises ``DiffScanError``; each ``main()`` turns that into exit 2.

Every tool is exercised in a throwaway git repo with real git, in three
directions:

* bad base + staged violation -> 2, and no OK line (the missed-detection side);
* good base + legal input     -> 0 (the over-rejection side);
* good base + staged violation -> 1 (the helper's success path still feeds the
  tool the diff -- a helper that always returned [] would pass the first two).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import _lint_helpers  # noqa: E402
import check_ad_hoc_git_scripts as adhoc  # noqa: E402
import check_bat_ascii_purity as bat  # noqa: E402
import check_repo_name as crn  # noqa: E402
from _lint_helpers import DiffScanError, diff_changed_paths  # noqa: E402

BAD_BASE = "zz-no-such-ref"
WRONG_URL = "github.com/vencil/" + "vibe-k8s-lab"  # split so this file is not itself a hit


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True,
                   text=True, encoding="utf-8", timeout=30)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "README.md").write_text("base\n", encoding="utf-8", newline="\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _stage(repo: Path, rel: str, data: bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _git(repo, "add", "-f", rel)


def _run(monkeypatch, capsys, module, argv: list[str]) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", [module.__name__, *argv])
    rc = module.main()
    out, err = capsys.readouterr()
    return rc, out, err


# ---------------------------------------------------------------------------
# Per-tool cases: (module, violating (rel, bytes), legal (rel, bytes), extra argv)
# ---------------------------------------------------------------------------

CASES = {
    "ad_hoc_git_scripts": (
        adhoc,
        ("_probe_commit.bat", b"@echo off\r\ngit commit\r\n"),
        ("scripts/ops/ok.bat", b"@echo off\r\nrem ok\r\n"),
        [],
    ),
    "bat_ascii_purity": (
        bat,
        ("scripts/ops/_probe.bat", "@echo off\r\nrem café\r\n".encode("utf-8")),
        ("scripts/ops/ok.bat", b"@echo off\r\nrem ok\r\n"),
        [],
    ),
    "repo_name": (
        crn,
        ("docs/probe.md", f"see https://{WRONG_URL}/x\n".encode("utf-8")),
        ("docs/probe.md", b"see https://github.com/vencil/Dynamic-Alerting-Integrations\n"),
        ["--ci"],
    ),
}


@pytest.fixture
def in_repo(repo: Path, monkeypatch) -> Path:
    """Point every tool at the throwaway repo.

    ad_hoc and bat find the repo from the cwd; repo_name (and the helper's
    per-line diff) use a module-level REPO_ROOT.
    """
    monkeypatch.chdir(repo)
    monkeypatch.setattr(crn, "REPO_ROOT", repo)
    monkeypatch.setattr(_lint_helpers, "REPO_ROOT", repo)
    monkeypatch.delenv("PR_BODY", raising=False)
    return repo


@pytest.mark.parametrize("name", sorted(CASES))
def test_unresolvable_base_with_staged_violation_exits_2(name, in_repo, monkeypatch, capsys):
    module, (rel, data), _legal, extra = CASES[name]
    _stage(in_repo, rel, data)
    rc, out, err = _run(monkeypatch, capsys, module, ["--diff-base", BAD_BASE, *extra])
    assert rc == 2, f"{name}: rc={rc}\nstdout={out}\nstderr={err}"
    assert "OK" not in out, f"{name} reported OK on a failed git diff:\n{out}"
    assert "git diff" in err and BAD_BASE in err, err


@pytest.mark.parametrize("name", sorted(CASES))
def test_resolvable_base_with_legal_input_exits_0(name, in_repo, monkeypatch, capsys):
    module, _viol, (rel, data), extra = CASES[name]
    _stage(in_repo, rel, data)
    rc, out, err = _run(monkeypatch, capsys, module, ["--diff-base", "HEAD", *extra])
    assert rc == 0, f"{name}: rc={rc}\nstdout={out}\nstderr={err}"


@pytest.mark.parametrize("name", sorted(CASES))
def test_resolvable_base_with_staged_violation_exits_1(name, in_repo, monkeypatch, capsys):
    module, (rel, data), _legal, extra = CASES[name]
    _stage(in_repo, rel, data)
    rc, out, err = _run(monkeypatch, capsys, module, ["--diff-base", "HEAD", *extra])
    assert rc == 1, f"{name}: rc={rc}\nstdout={out}\nstderr={err}"


# Same cases with a non-ASCII path. Without `git diff -z`, git C-quotes such a
# path ("_\346\270\254.bat"), the tools' suffix / is_file() checks miss it, and
# a staged violation reads as clean -- the same silent-OK shape as a failed diff.
NON_ASCII = {
    "ad_hoc_git_scripts": ("_測試.bat", "scripts/ops/測試.bat"),
    "bat_ascii_purity": ("scripts/ops/_測試.bat", "scripts/ops/測試.bat"),
    "repo_name": ("docs/測試.md", "docs/測試.md"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_non_ascii_named_violation_is_still_seen(name, in_repo, monkeypatch, capsys):
    module, (_rel, data), _legal, extra = CASES[name]
    _stage(in_repo, NON_ASCII[name][0], data)
    rc, out, err = _run(monkeypatch, capsys, module, ["--diff-base", "HEAD", *extra])
    assert rc == 1, f"{name}: rc={rc}\nstdout={out}\nstderr={err}"


@pytest.mark.parametrize("name", sorted(CASES))
def test_non_ascii_named_legal_input_exits_0(name, in_repo, monkeypatch, capsys):
    module, _viol, (_rel, data), extra = CASES[name]
    _stage(in_repo, NON_ASCII[name][1], data)
    rc, out, err = _run(monkeypatch, capsys, module, ["--diff-base", "HEAD", *extra])
    assert rc == 0, f"{name}: rc={rc}\nstdout={out}\nstderr={err}"


# A move that git detects as a rename has status R, and `--diff-filter=AM`
# dropped it: `git mv` into a violating place -- with or without an edit on the
# way -- read as clean (#2025). Each case: (committed path, committed bytes,
# destination, bytes after the move or None for a pure rename). The files are
# long enough that the edit keeps git's similarity above the rename threshold;
# `_assert_git_sees_a_rename` checks that, or the case would test an A.
_FILLER = "".join(f"rem line {i}\r\n" for i in range(40))
_BAD_BAT = ("@echo off\r\ngit commit\r\n" + _FILLER).encode()
_ASCII_BAT = ("@echo off\r\n" + _FILLER).encode()
_NON_ASCII_BAT = ("@echo off\r\nrem café\r\n" + _FILLER).encode("utf-8")
_WRONG_MD = (f"see https://{WRONG_URL}/x\n" + _FILLER.replace("\r", "")).encode()

RENAME_INTO_VIOLATION = {
    ("ad_hoc_git_scripts", "pure"): ("scripts/ops/tool.bat", _BAD_BAT, "tool.bat", None),
    ("ad_hoc_git_scripts", "edit"): ("scripts/ops/tool.bat", _BAD_BAT, "tool.bat",
                                     _BAD_BAT + b"rem edited\r\n"),
    ("bat_ascii_purity", "pure"): ("scripts/ops/a.bat", _NON_ASCII_BAT, "scripts/ops/b.bat", None),
    ("bat_ascii_purity", "edit"): ("scripts/ops/a.bat", _ASCII_BAT, "scripts/ops/b.bat",
                                   _NON_ASCII_BAT),
    ("repo_name", "pure"): ("tests/x.md", _WRONG_MD, "docs/x.md", None),
    ("repo_name", "edit"): ("tests/x.md", _WRONG_MD, "docs/x.md", _WRONG_MD + b"edited\n"),
}

# The other direction: a move into a place the tool accepts stays clean.
RENAME_INTO_ACCEPTED = {
    "ad_hoc_git_scripts": ("tools/x.bat", _BAD_BAT, "scripts/ops/x.bat"),
    "bat_ascii_purity": ("scripts/ops/a.bat", _ASCII_BAT, "scripts/ops/b.bat"),
    "repo_name": ("docs/x.md", _WRONG_MD, "tests/x.md"),
}


def _commit_then_move(repo: Path, src: str, data: bytes, dst: str, after) -> None:
    _stage(repo, src, data)
    _git(repo, "commit", "-q", "-m", "second")
    (repo / dst).parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", src, dst)
    if after is not None:
        _stage(repo, dst, after)


def _assert_git_sees_a_rename(repo: Path, src: str, dst: str) -> None:
    out = subprocess.run(["git", "diff", "--name-status", "-z", "HEAD"], cwd=str(repo),
                         check=True, capture_output=True, timeout=30).stdout
    fields = out.decode("utf-8").split("\0")
    assert fields[0].startswith("R") and fields[1:3] == [src, dst], fields


@pytest.mark.parametrize("name,shape", sorted(RENAME_INTO_VIOLATION))
def test_rename_into_violation_is_seen(name, shape, in_repo, monkeypatch, capsys):
    module, *_rest, extra = CASES[name]
    src, data, dst, after = RENAME_INTO_VIOLATION[(name, shape)]
    _commit_then_move(in_repo, src, data, dst, after)
    _assert_git_sees_a_rename(in_repo, src, dst)
    rc, out, err = _run(monkeypatch, capsys, module, ["--diff-base", "HEAD", *extra])
    assert rc == 1, f"{name}/{shape}: rc={rc}\nstdout={out}\nstderr={err}"


@pytest.mark.parametrize("name", sorted(RENAME_INTO_ACCEPTED))
def test_rename_into_accepted_place_exits_0(name, in_repo, monkeypatch, capsys):
    module, *_rest, extra = CASES[name]
    src, data, dst = RENAME_INTO_ACCEPTED[name]
    _commit_then_move(in_repo, src, data, dst, None)
    _assert_git_sees_a_rename(in_repo, src, dst)
    rc, out, err = _run(monkeypatch, capsys, module, ["--diff-base", "HEAD", *extra])
    assert rc == 0, f"{name}: rc={rc}\nstdout={out}\nstderr={err}"


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------

def test_helper_raises_on_git_failure(repo):
    with pytest.raises(DiffScanError, match=BAD_BASE):
        diff_changed_paths(BAD_BASE, repo)


def test_helper_raises_on_timeout(repo, monkeypatch):
    def _timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
    monkeypatch.setattr(_lint_helpers.subprocess, "run", _timeout)
    with pytest.raises(DiffScanError, match="timed out"):
        diff_changed_paths("HEAD", repo)


def test_helper_lists_added_and_modified_but_not_deleted(repo):
    _stage(repo, "gone.txt", b"y\n")
    _git(repo, "commit", "-q", "-m", "second")
    (repo / "README.md").write_text("changed\n", encoding="utf-8", newline="\n")  # M
    _stage(repo, "a/new.txt", b"x\n")                                            # A
    _git(repo, "rm", "-q", "gone.txt")                                           # D
    assert sorted(diff_changed_paths("HEAD", repo)) == ["README.md", "a/new.txt"]


def test_helper_returns_non_ascii_paths_verbatim(repo):
    _stage(repo, "d/測試 x.bat", b"@echo off\r\n")
    assert diff_changed_paths("HEAD", repo) == ["d/測試 x.bat"]


def test_helper_lists_a_copy_under_copy_detection_config(repo):
    """``diff.renames=copies`` is the user's config; it must not hide a copy (#2025).

    This is the case that separates ``--no-renames`` from adding ``R`` to the
    filter: git reports the copy as ``C``, which ``AMR`` drops as well.
    """
    _git(repo, "config", "diff.renames", "copies")
    body = "".join(f"line {i}\n" for i in range(30)).encode()
    _stage(repo, "src.txt", body)
    _git(repo, "commit", "-q", "-m", "second")
    _stage(repo, "src.txt", body + b"more\n")   # copy detection needs a modified source
    _stage(repo, "copy.txt", body)
    assert sorted(diff_changed_paths("HEAD", repo)) == ["copy.txt", "src.txt"]


def test_helper_pathspec_limits_the_listing(repo):
    _stage(repo, "scripts/ops/x.bat", b"@echo off\r\n")
    _stage(repo, "other/y.bat", b"@echo off\r\n")
    assert diff_changed_paths("HEAD", repo, ("scripts/ops/*.bat",)) == ["scripts/ops/x.bat"]
